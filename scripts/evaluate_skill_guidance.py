"""Compare three runbook policies on isolated lab incidents and HTTP verification."""

from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from pathlib import Path

from ops_recovery.backends import LabBackend
from ops_recovery.config import Settings
from ops_recovery.coordinator import Coordinator
from ops_recovery.domain import Phase
from ops_recovery.lab import FAULTS, start_lab
from ops_recovery.report import render_run
from ops_recovery.session_evaluation import write_report
from ops_recovery.skills import Guide, matches
from ops_recovery.storage import Store, canonical

MANUAL = {
    "title": "Investigate a gateway process exit",
    "service": "gateway",
    "required_signals": ["process_exited"],
    "steps": [
        "Confirm the current gateway process state and correlate the exit event with the incident timeline.",
        "Check database and dependency evidence before proposing any action. Escalate when another component is the root cause.",
    ],
    "stop_conditions": ["Stop if the exit is no longer current or if a different root cause is supported."],
    "verify": "After the approved recovery, require three successful business HTTP probes.",
}


class GuidedLab:
    def __init__(self, backend, guide, arm):
        self.backend, self.guide, self.arm = backend, guide, arm
        self.observed: dict[str, set[str]] = {}
        self.served = 0

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def read(self, name, arguments):
        if name != "search_runbooks":
            result = self.backend.read(name, arguments)
            for row in result.get("evidence", []):
                if row.get("service") and row.get("signal") and not row["id"].startswith("rb-"):
                    self.observed.setdefault(row["service"], set()).add(row["signal"])
            return result
        if not self.guide or not matches(self.guide, self.observed):
            return {"evidence": []}
        self.served += 1
        return {
            "evidence": [
                {
                    "id": f"rb-skill-study-{self.arm}",
                    "kind": "documentation",
                    "service": self.guide["service"],
                    "guide": self.guide,
                }
            ]
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--mode", choices=("offline", "online"), default="online")
    args = parser.parse_args()
    candidate = Guide.model_validate(
        json.loads(args.candidate.read_text(encoding="utf-8"))["guide"]
    ).model_dump()
    settings = Settings.load(args.mode)
    store = Store(settings.state_dir)
    coordinator = Coordinator(settings, store)
    traces = Path("reports") / ("skill-study-traces-" + uuid.uuid4().hex[:8])
    lab_path = settings.state_dir / ("skill-study-lab-" + uuid.uuid4().hex[:8])
    start_lab(lab_path)
    lab = LabBackend(lab_path)
    variants = {"off": None, "manual": MANUAL, "generated": candidate}
    rows, ids = [], []
    try:
        for fault, expected in FAULTS.items():
            for variant, guide in variants.items():
                tick = time.monotonic()
                lab.inject(fault)
                backend = GuidedLab(lab, guide, variant)
                run_id = store.create(lab.spec, settings.mode)
                ids.append(run_id)
                fault_error = None
                try:
                    coordinator.diagnose(
                        backend,
                        run_id=run_id,
                        dual=False,
                        context=(
                            "After the current logs and metrics, call search_runbooks for an optional contextual guide. "
                            "The guide may offer advice. Cite only evidence observed from the current incident."
                        ),
                    )
                    run = store.get(run_id)
                    if (
                        run["phase"] == Phase.WAITING_APPROVAL
                        and run["diagnosis"]["service"] == expected["service"]
                        and run["diagnosis"]["action"] == expected["action"]
                    ):
                        coordinator.approve(run_id, run["proposal_digest"], actor="automated-test-policy")
                        coordinator.resume(run_id, lab)
                except Exception as exc:
                    fault_error = str(exc)[:350]
                run = store.get(run_id)
                diagnosis = run.get("diagnosis") or {}
                verified = run["phase"] == Phase.RESOLVED and len(run.get("verification", [])) == 3
                model_events = [event for event in store.events(run_id) if event["kind"] == "model_request"]
                row = {
                    "case": fault,
                    "variant": variant,
                    "run_id": run_id,
                    "phase": run["phase"],
                    "correct_service": diagnosis.get("service") == expected["service"],
                    "correct_action": diagnosis.get("action") == expected["action"],
                    "guide_retrieved": bool(backend.served),
                    "verified_recovery": verified,
                    "model_calls": len(model_events),
                    "elapsed_s": round(time.monotonic() - tick, 3),
                    "error": fault_error,
                }
                rows.append(row)
                render_run(store, run_id, traces / f"{run_id}.html")
                print(canonical(row), flush=True)
    finally:
        lab.close()
    summary = {}
    for variant in variants:
        group = [r for r in rows if r["variant"] == variant]
        summary[variant] = {
            "runs": len(group),
            "verified": sum(r["verified_recovery"] for r in group),
            "correct_service": sum(r["correct_service"] for r in group),
            "positive_retrieval": sum(r["case"] == "api_crash" and r["guide_retrieved"] for r in group),
            "negative_retrieval": sum(r["case"] != "api_crash" and r["guide_retrieved"] for r in group),
            "mean_calls": statistics.mean(r["model_calls"] for r in group),
        }
    print(
        canonical(
            write_report(
                "skill-ablation",
                {
                    "mode": settings.mode,
                    "summary": summary,
                    "results": rows,
                    "traces": str(traces.resolve()),
                    "cost": store.cost(ids),
                    "scope": "Separate lab instance using the same fault family as the candidate source, plus two counterexample families. Repairs require the lab test approval policy.",
                },
            )
        )
    )


if __name__ == "__main__":
    main()
