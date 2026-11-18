from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import platform
import statistics
import time
import uuid
from collections import defaultdict
from pathlib import Path

from .backends import DatasetBackend, FaultyBackend, LabBackend
from .config import Settings
from .coordinator import DIAGNOSTIC_PROMPT, REVIEW_PROMPT, Coordinator
from .domain import Phase
from .lab import FAULTS, start_lab
from .report import render_evaluation, render_run
from .storage import Store, canonical, digest

VARIANTS = {
    "baseline": {"dual": False, "reliable": False},
    "reliable": {"dual": False, "reliable": True},
    "dual": {"dual": True, "reliable": True},
}


def wilson(successes: int, total: int) -> list[float] | None:
    if not total:
        return None
    z, p = 1.96, successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [round(max(0, center - margin), 4), round(min(1, center + margin), 4)]


def summarize(rows: list[dict]) -> dict:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["suite"], row["variant"])].append(row)
    result = {}
    for (suite, variant), group in groups.items():
        correct = sum(row["diagnosis_correct"] for row in group)
        summary = {
            "n_runs": len(group),
            "n_unique_cases": len({r["case"] for r in group}),
            "service_ac_at_1": correct / len(group),
            "wilson_95_run_level": wilson(correct, len(group)),
            "accepted_correct_rate": sum(r["accepted_correct"] for r in group) / len(group),
            "failures": sum(bool(r.get("error")) for r in group),
            "mean_tool_attempts": statistics.mean(r["tool_attempts"] for r in group),
            "mean_model_calls": statistics.mean(r["model_calls"] for r in group),
            "mean_elapsed_seconds": statistics.mean(r["elapsed_seconds"] for r in group),
            "review_rejection_rate": sum(r.get("review_rejected", False) for r in group) / len(group),
        }
        if suite == "local":
            summary["verified_repair_rate"] = sum(r["repaired"] for r in group) / len(group)
            summary["unknown_response_recovered"] = sum(r.get("unknown_recovered", False) for r in group)
            summary["duplicate_repair_events"] = sum(r.get("duplicate_repairs", 0) for r in group)
        result.setdefault(suite, {})[variant] = summary
    if "local" in result and "public" in result:
        result["weighted_diagnosis"] = {
            variant: 0.6 * result["local"][variant]["service_ac_at_1"]
            + 0.4 * result["public"][variant]["service_ac_at_1"]
            for variant in result["local"].keys() & result["public"].keys()
        }
    return result


def evaluate(
    settings: Settings,
    *,
    suite: str,
    repeats: int,
    variants: list[str],
    split: str = "test",
    limit: int = 0,
    name: str = "evaluation",
) -> dict:
    if suite not in {"local", "public", "all"} or split not in {"dev", "test"}:
        raise ValueError("Invalid evaluation suite or split")
    if not 1 <= repeats <= 5 or not variants or any(variant not in VARIANTS for variant in variants):
        raise ValueError("Choose 1..5 repeats and known ablation variants")
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Report name must contain only letters, numbers, hyphens or underscores")
    experiment_dir = Path("reports") / (name + "-" + uuid.uuid4().hex[:8])
    experiment_dir.mkdir(parents=True)
    store = Store(settings.state_dir)
    coordinator = Coordinator(settings, store)
    cases = []
    lab = None
    if suite in {"local", "all"}:
        lab_dir = settings.state_dir / ("evaluation-lab-" + uuid.uuid4().hex[:8])
        start_lab(lab_dir)
        lab = LabBackend(lab_dir)
        cases += [("local", fault, {"service": expected["service"]}) for fault, expected in FAULTS.items()]
    manifest = None
    if suite in {"public", "all"}:
        manifest = json.loads(Path("data/manifest.json").read_text(encoding="utf-8"))
        answers = json.loads(Path("data/answers/answers.json").read_text(encoding="utf-8"))
        selected = [row for row in answers if row["split"] == split]
        if limit:
            selected = selected[:limit]
        cases += [("public", row["id"], row) for row in selected]
    rows = []
    metadata = {
        "mode": settings.mode,
        "model": settings.model_id if settings.mode == "online" else "offline-scripted",
        "variants": {v: VARIANTS[v] for v in variants},
        "repeats": repeats,
        "split": split,
        "prompt_digest": digest({"diagnostic": DIAGNOSTIC_PROMPT, "review": REVIEW_PROMPT}),
        "source_digest": hashlib.sha256(
            "\n".join(
                p.name + "\n" + p.read_text(encoding="utf-8")
                for p in sorted(Path(__file__).parent.glob("*.py"))
            ).encode()
        ).hexdigest(),
        "python": platform.python_version(),
        "packages": {
            name: importlib.metadata.version(name) for name in ("smolagents", "mcp", "httpx", "pydantic")
        },
        "model_limits": {
            "max_output_tokens": settings.max_output,
            "stage_deadline_seconds": settings.agent_deadline,
        },
        "dataset_manifest_digest": digest(manifest) if manifest else None,
        "dataset_revision": manifest["revision"] if manifest else None,
        "tool_fault": "first query_logs request times out once in every run",
        "approval_policy": "automated-test-policy (not a human approval measurement)",
        "price_cny_per_million": {"input": settings.input_price, "output": settings.output_price},
        "limitations": [
            "Small held-out sample, not a full RCAEval reproduction",
            "Run-level intervals are descriptive; repeated runs on a case are correlated",
            "Public metrics/logs only; raw traces are not imported in this release",
            "Static public replay cannot validate repair success",
            "Both roles use the same model; isolated context is not statistical independence",
        ],
    }
    (experiment_dir / "protocol.json").write_text(canonical(metadata), encoding="utf-8")
    try:
        for current_suite, case_id, answer in cases:
            for repeat in range(repeats):
                for variant in variants:
                    start = time.monotonic()
                    run_id = None
                    row = {
                        "suite": current_suite,
                        "case": case_id,
                        "repeat": repeat,
                        "variant": variant,
                        "diagnosis_correct": False,
                        "accepted_correct": False,
                        "repaired": False,
                        "tool_attempts": 0,
                        "model_calls": 0,
                    }
                    try:
                        if current_suite == "local":
                            lab.inject(case_id)
                            base = lab
                        else:
                            base = DatasetBackend(Path("data/blind") / split / case_id)
                        backend = FaultyBackend(base)
                        run_id = store.create(backend.spec, settings.mode)
                        coordinator.diagnose(backend, run_id=run_id, **VARIANTS[variant])
                        run = store.get(run_id)
                        row["prediction"] = run["diagnosis"]
                        row["diagnosis_correct"] = bool(
                            run["diagnosis"] and run["diagnosis"]["service"] == answer["service"]
                        )
                        row["accepted_correct"] = row["diagnosis_correct"] and run["phase"] not in {
                            Phase.REJECTED,
                            Phase.FAILED,
                        }
                        row["review_rejected"] = bool(run.get("review") and not run["review"]["accepted"])
                        if current_suite == "local" and run["phase"] == Phase.WAITING_APPROVAL:
                            coordinator.approve(run_id, run["proposal_digest"], actor="automated-test-policy")
                            coordinator.resume(run_id, base, drop_response=True)
                            unknown = store.get(run_id)["phase"] == Phase.UNKNOWN
                            if unknown:
                                coordinator.resume(run_id, base)
                            row["repaired"] = store.get(run_id)["phase"] == Phase.RESOLVED
                            row["unknown_recovered"] = unknown and row["repaired"]
                            repair_events = [
                                r
                                for r in base.read("query_logs", {"service": "all"})["evidence"]
                                if r.get("operation_id") == "repair-" + run_id
                            ]
                            row["duplicate_repairs"] = max(0, len(repair_events) - 1)
                        row["phase"] = store.get(run_id)["phase"]
                    except Exception as error:
                        row["error"] = {"type": type(error).__name__, "message": str(error)[:300]}
                    finally:
                        if run_id:
                            events = store.events(run_id)
                            row.update(
                                run_id=run_id,
                                tool_attempts=sum(e["kind"] == "tool" for e in events),
                                model_calls=sum(e["kind"] == "model_request" for e in events),
                            )
                            render_run(store, run_id, experiment_dir / "traces" / f"{run_id}.html")
                        row["elapsed_seconds"] = round(time.monotonic() - start, 3)
                        rows.append(row)
                        with (experiment_dir / "results.jsonl").open("a", encoding="utf-8") as stream:
                            stream.write(canonical(row) + "\n")
                        print(
                            canonical(
                                {
                                    key: row.get(key)
                                    for key in (
                                        "suite",
                                        "case",
                                        "variant",
                                        "repeat",
                                        "phase",
                                        "diagnosis_correct",
                                        "error",
                                    )
                                }
                            ),
                            flush=True,
                        )
                        partial = {
                            "protocol": metadata,
                            "summary": summarize(rows),
                            "cost_ledger": store.cost(),
                            "experiment_cost_ledger": store.cost(
                                [row["run_id"] for row in rows if row.get("run_id")]
                            ),
                            "completed_runs": len(rows),
                            "output_directory": str(experiment_dir.resolve()),
                        }
                        (experiment_dir / "summary.json").write_text(canonical(partial), encoding="utf-8")
                        render_evaluation(partial, experiment_dir / "report.html")
    finally:
        if lab:
            lab.close()
    result = {
        "protocol": metadata,
        "summary": summarize(rows),
        "cost_ledger": store.cost(),
        "experiment_cost_ledger": store.cost([row["run_id"] for row in rows if row.get("run_id")]),
        "completed_runs": len(rows),
        "output_directory": str(experiment_dir.resolve()),
    }
    (experiment_dir / "summary.json").write_text(canonical(result), encoding="utf-8")
    render_evaluation(result, experiment_dir / "report.html")
    return result
