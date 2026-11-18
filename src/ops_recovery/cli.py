from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from .backends import DatasetBackend, LabBackend, backend_from_spec
from .config import Settings
from .coordinator import Coordinator
from .lab import FAULTS, start_lab
from .report import render_run
from .storage import Store

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
    help="Evidence-driven diagnosis, approved repairs and reproducible evaluations.",
)
lab_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
app.add_typer(lab_app, name="lab")


def display(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def laboratory() -> LabBackend:
    return LabBackend(Settings.load("offline").state_dir / "lab")


@lab_app.command("up")
def lab_up():
    settings = Settings.load("offline")
    manifest = start_lab(settings.state_dir / "lab")
    display({"lab_id": manifest["lab_id"], "url": manifest["url"]})


@lab_app.command("inject")
def lab_inject(fault: Annotated[str, typer.Argument(help="api_crash, dependency_timeout or database_lock")]):
    if fault not in FAULTS:
        raise typer.BadParameter("Choose one of: " + ", ".join(FAULTS))
    display(laboratory().inject(fault))


@lab_app.command("down")
def lab_down():
    laboratory().close()
    typer.echo("Lab shutdown requested; owned child processes will be stopped.")


@lab_app.command("probe")
def lab_probe():
    display(laboratory().probe())


@app.command()
def diagnose(
    mode: str | None = None,
    dataset_case: Path | None = None,
    mcp: bool = False,
    single: bool = False,
    no_retry: bool = False,
):
    settings = Settings.load(mode)
    store = Store(settings.state_dir)
    backend = DatasetBackend(dataset_case) if dataset_case else laboratory()
    transport = None
    if mcp:
        from .mcp_transport import MCPBackend

        backend = transport = MCPBackend(backend)
    try:
        run_id = Coordinator(settings, store).diagnose(backend, dual=not single, reliable=not no_retry)
        run = store.get(run_id)
        report = render_run(store, run_id, Path("reports") / f"{run_id}.html")
        display(
            {
                "run_id": run_id,
                "mode": run["mode"],
                "phase": run["phase"],
                "diagnosis": run["diagnosis"],
                "proposal_digest": run.get("proposal_digest"),
                "report": str(report.resolve()),
            }
        )
        if run["phase"] == "waiting_approval":
            typer.echo(f"Review the proposal, then: ops approve {run_id} --digest {run['proposal_digest']}")
    finally:
        if transport:
            transport.transport_close()


@app.command()
def approve(run_id: str, digest: Annotated[str, typer.Option(help="Full proposal digest shown by diagnose")]):
    settings = Settings.load("offline")
    store = Store(settings.state_dir)
    Coordinator(settings, store).approve(run_id, digest)
    typer.echo(f"Approval persisted. Execute or recover with: ops resume {run_id}")


@app.command()
def resume(run_id: str):
    settings = Settings.load()
    store = Store(settings.state_dir)
    run = store.get(run_id)
    # An offline run stays offline even if .env was changed for later online experiments.
    settings.mode = run["mode"]
    backend = backend_from_spec(run["backend"])
    transport = None
    if run["backend"].get("transport") == "mcp":
        from .mcp_transport import MCPBackend

        backend = transport = MCPBackend(backend)
    try:
        Coordinator(settings, store).resume(run_id, backend)
        display(store.get(run_id))
        render_run(store, run_id, Path("reports") / f"{run_id}.html")
    finally:
        if transport:
            transport.transport_close()


@app.command()
def show(run_id: str):
    store = Store(Settings.load("offline").state_dir)
    display(store.get(run_id))
    typer.echo(str(render_run(store, run_id, Path("reports") / f"{run_id}.html").resolve()))


@app.command()
def budget():
    display(Store(Settings.load("offline").state_dir).cost())


@app.command("privacy-preview")
def privacy_preview():
    import uuid

    from .coordinator import DIAGNOSTIC_PROMPT, REVIEW_PROMPT
    from .storage import canonical

    settings = Settings.load("offline")
    directory = settings.state_dir / ("preview-lab-" + uuid.uuid4().hex[:8])
    start_lab(directory)
    backend = LabBackend(directory)
    try:
        backend.inject("api_crash")
        observations = {
            name: backend.read(name, arguments)
            for name, arguments in (
                ("list_services", {}),
                ("query_logs", {"service": "all"}),
                ("query_metrics", {"service": "all"}),
                ("search_runbooks", {"query": "incident"}),
            )
        }
        preview = {
            "destination": settings.base_url + "/chat/completions",
            "model": settings.model_id,
            "content_categories": [
                "project-generated synthetic incident telemetry",
                "read-tool schemas",
                "agent instructions and intermediate diagnoses",
                "anonymous public RCAEval evidence",
            ],
            "diagnostic_instructions": DIAGNOSTIC_PROMPT,
            "review_instructions": REVIEW_PROMPT,
            "sample_local_observations": observations,
            "excluded": [
                ".env contents",
                "user documents",
                "other projects",
                "raw dataset labels/answers",
                "API key in prompt or report",
            ],
            "authentication": "Your configured API key is sent only as the provider Authorization header",
        }
        public = Path("data/blind/dev/dev-001")
        if public.exists():
            public_backend = DatasetBackend(public)
            preview["sample_public_evidence"] = public_backend.read("query_metrics", {"service": "all"})
        output = Path("reports/privacy-preview.json")
        output.parent.mkdir(exist_ok=True)
        output.write_text(canonical(preview), encoding="utf-8")
        display(
            {
                "preview": str(output.resolve()),
                "destination": preview["destination"],
                "model": settings.model_id,
            }
        )
    finally:
        backend.close()


@app.command("data-import")
def data_import(dev: int = 3, test: int = 12, seed: int = 20261118):
    from .datasets import import_rcaeval

    display(import_rcaeval(Path("data"), dev_count=dev, test_count=test, seed=seed))


@app.command("evaluate")
def evaluate(
    mode: str = "offline",
    suite: str = "local",
    repeats: int = 2,
    variants: str = "baseline,reliable,dual",
    split: str = "test",
    limit: int = 0,
    name: str = "evaluation",
):
    from .evaluation import evaluate as run_evaluation

    display(
        run_evaluation(
            Settings.load(mode),
            suite=suite,
            repeats=repeats,
            variants=variants.split(","),
            split=split,
            limit=limit,
            name=name,
        )
    )


if __name__ == "__main__":
    app()
