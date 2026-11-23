from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Annotated

import typer

from .config import Settings
from .report import escaped, render_run
from .sessions import Daemon, Sessions
from .skills import Skills
from .storage import Store, digest
from .umami import UmamiBackend

session_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
skill_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)


def emit(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def local_store():
    return Store(Settings.load("offline").state_dir)


def pilot():
    return UmamiBackend(Path(os.getenv("OPS_PILOT_SOCKET", "/run/ops-pilot/broker.sock")))


@session_app.command("serve")
def serve(mode: str | None = None, single: bool = False, drain: bool = False):
    """Process queued turns and reconcile interrupted operations."""
    Daemon(Settings.load(mode), dual=not single).run(drain=drain)


@session_app.command("new")
def session_new(
    message: str,
    backend: str = "umami",
    mode: str = "offline",
    dataset: Path | None = None,
    constraint: Annotated[list[str] | None, typer.Option()] = None,
):
    """Start an incident and queue the first turn."""
    store = local_store()
    if mode not in {"offline", "online"}:
        raise typer.BadParameter("mode must be offline or online")
    if backend == "umami" and dataset is None:
        spec = pilot().spec
    elif backend == "lab" and dataset is None:
        spec = {"kind": "lab", "directory": str(store.path.parent / "lab")}
    elif backend == "dataset" and dataset is not None:
        spec = {"kind": "dataset", "directory": str(dataset.resolve())}
    else:
        raise typer.BadParameter("Use umami, lab, or dataset with --dataset")
    sessions = Sessions(store)
    session_id = sessions.create(spec, mode, message, constraint)
    emit(sessions.history(session_id, limit=1))


@session_app.command("tell")
def tell(session_id: str, message: str, request_key: str | None = None):
    """Queue a follow-up in an existing incident."""
    key = request_key or uuid.uuid4().hex
    emit({"run_id": Sessions(local_store()).tell(session_id, message, key), "request_key": key})


@session_app.command("chat")
def chat(session_id: str, wait_seconds: Annotated[int, typer.Option(min=1, max=900)] = 300):
    """Attach to a durable incident and continue the conversation."""
    import time

    sessions = Sessions(local_store())
    emit(sessions.history(session_id, limit=2))
    while True:
        try:
            line = input("incident> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if line == "/exit":
            return
        if not line:
            continue
        run_id = sessions.tell(session_id, line, uuid.uuid4().hex)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            turns = sessions.history(session_id, limit=30)["turns"]
            current = next((turn for turn in turns if turn["run_id"] == run_id), None)
            if current and current["status"] in {"done", "failed", "paused"}:
                emit(current)
                break
            time.sleep(0.5)
        else:
            emit(
                {
                    "run_id": run_id,
                    "status": "wait expired",
                    "message": "Use session show to inspect the persistent job",
                }
            )


@session_app.command("list")
def session_list():
    emit(Sessions(local_store()).list())


@session_app.command("show")
def session_show(session_id: str, before: int | None = None):
    emit(Sessions(local_store()).history(session_id, before=before))


@session_app.command("report")
def session_report(session_id: str, output: Path | None = None):
    store = local_store()
    data = Sessions(store).history(session_id)
    path = output or Path("reports") / ("session-" + data["id"]) / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    ids = sorted({row["run_id"] for row in data["turns"]})
    for run_id in ids:
        render_run(store, run_id, path.parent / f"{run_id}.html")
    links = "".join(f'<li><a href="{run_id}.html">{run_id}</a></li>' for run_id in ids)
    path.write_text(
        f"<!doctype html><meta charset=utf-8><title>Incident session</title><h1>Incident session</h1>"
        f"<ul>{links}</ul><pre style='white-space:pre-wrap'>{escaped(data)}</pre>",
        encoding="utf-8",
    )
    typer.echo(str(path.resolve()))


@session_app.command("approve")
def session_approve(session_id: str, run_id: str, proposal_digest: Annotated[str, typer.Option("--digest")]):
    emit({"queued": Sessions(local_store()).request_repair(session_id, run_id, proposal_digest)})


@session_app.command("reconcile")
def reconcile(session_id: str, run_id: str):
    emit({"queued": Sessions(local_store()).request_repair(session_id, run_id, None, reconcile=True)})


@session_app.command("observe")
def observe():
    backend = pilot()
    health = backend.health()
    if health["sample"] is None:
        emit({"status": "no completed probe sample"})
        return
    emit(Sessions(local_store()).watch(backend.spec, "offline", key=digest(backend.spec), **health))


@session_app.command("benchmark")
def benchmark(mode: str = "offline", count: int = 10, dual: bool = False):
    """Run independent RCAEval sessions through the shared scheduler and model gate."""
    from .session_evaluation import run_public_concurrency

    emit(run_public_concurrency(Settings.load(mode), count, dual=dual))


@session_app.command("pilot-report")
def report_pilot(since: str):
    """Render observer continuity and business probe outcomes for an ISO timestamp."""
    from datetime import datetime

    from .session_evaluation import pilot_report

    store = local_store()
    Sessions(store)
    report = pilot_report(store, datetime.fromisoformat(since))
    emit({key: value for key, value in report.items() if key != "samples"})


@skill_app.command("draft")
def skill_draft(run_id: str, mode: str = "offline", previous: str | None = None):
    settings = Settings.load(mode)
    skills = Skills(Store(settings.state_dir))
    emit(skills.show(skills.from_run(run_id, settings, previous=previous)))


@skill_app.command("list")
def skill_list():
    emit(Skills(local_store()).list())


@skill_app.command("show")
def skill_show(skill_id: str):
    emit(Skills(local_store()).show(skill_id))


@skill_app.command("replay")
def skill_replay(skill_id: str, cases: Path):
    if cases.stat().st_size > 256_000:
        raise typer.BadParameter("Replay case file exceeds 256 KB")
    emit(Skills(local_store()).replay(skill_id, json.loads(cases.read_text(encoding="utf-8"))))


@skill_app.command("enable")
def skill_enable(skill_id: str, guide_hash: str, replay_hash: str):
    Skills(local_store()).enable(skill_id, guide_hash, replay_hash)
    emit({"status": "active", "skill_id": skill_id})


@skill_app.command("disable")
def skill_disable(skill_id: str):
    Skills(local_store()).disable(skill_id)
    emit({"status": "disabled", "skill_id": skill_id})
