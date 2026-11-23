from __future__ import annotations

import json
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .domain import Phase
from .report import render_evaluation
from .sessions import Daemon
from .storage import Store, canonical, digest


def run_public_concurrency(settings, count: int, *, dual: bool):
    if not 1 <= count <= 10:
        raise ValueError("Concurrency benchmark accepts 1 through 10 cases")
    candidates = json.loads(Path("data/answers/answers.json").read_text(encoding="utf-8"))
    cases = sorted((row for row in candidates if row["split"] == "test"), key=lambda row: row["id"])[:count]
    if len(cases) != count:
        raise ValueError("Import enough RCAEval cases before benchmarking")
    daemon = Daemon(settings, dual=dual)
    started = time.monotonic()
    suite = []
    for case in cases:
        path = (Path("data/blind/test") / case["id"]).resolve()
        session = daemon.sessions.create(
            {"kind": "dataset", "directory": str(path)},
            settings.mode,
            "Investigate the observed incident and identify the root cause component.",
        )
        suite.append((session, case))
    daemon.run(drain=True)
    rows, events, run_ids = [], [], []
    for session, case in suite:
        turn = daemon.sessions.history(session, limit=1)["turns"][0]
        run_id = turn["run_id"]
        run_ids.append(run_id)
        trace = daemon.store.events(run_id)
        events.extend(trace)
        prediction = (turn["run"]["diagnosis"] or {}).get("service")
        rows.append(
            {
                "case": case["id"],
                "run_id": run_id,
                "phase": turn["run"]["phase"],
                "error": turn["error"],
                "correct": prediction == case["service"] and turn["run"]["phase"] == Phase.DIAGNOSED,
                "queue_s": round(turn["started"] - turn["created"], 3),
                "processing_s": round(turn["finished"] - turn["started"], 3),
                "model_calls": sum(event["kind"] == "model_request" for event in trace),
            }
        )
    active = peak = 0
    for event in sorted(events, key=lambda entry: entry["seq"]):
        if event["kind"] == "model_slot_acquired":
            active += 1
        elif event["kind"] == "model_slot_released":
            active -= 1
        peak = max(peak, active)
    report = {
        "mode": settings.mode,
        "model": settings.model_id if settings.mode == "online" else "offline-scripted",
        "assessment": "RCAEval prior benchmark groups (regression)",
        "sessions": count,
        "dual": dual,
        "verified_diagnoses": sum(row["correct"] for row in rows),
        "max_client_model_slots": peak,
        "model_requests": sum(row["model_calls"] for row in rows),
        "queue_median_s": statistics.median(row["queue_s"] for row in rows),
        "elapsed_s": round(time.monotonic() - started, 3),
        "cost": daemon.store.cost(run_ids),
        "runs": rows,
    }
    return write_report("session-concurrency", report)


def pilot_report(store: Store, since: datetime):
    end = datetime.now(timezone.utc)
    if since.tzinfo is None or not 0 < (end - since).total_seconds() <= 90 * 86400:
        raise ValueError("Provide an ISO timestamp with timezone within the past 90 days")
    with store.connect() as db:
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM probe_history WHERE observed>=? AND observed<=? ORDER BY observed",
                (since.timestamp(), end.timestamp()),
            )
        ]
    sequences: dict[str, list[dict]] = {}
    for row in rows:
        sequences.setdefault(row["key"], []).append(row)
    reports = []
    for key, samples in sequences.items():
        times = [since.timestamp(), *(row["observed"] for row in samples), end.timestamp()]
        biggest_gap = max(later - earlier for earlier, later in zip(times, times[1:]))
        span = samples[-1]["observed"] - samples[0]["observed"]
        reports.append(
            {
                "watch": key,
                "samples": len(samples),
                "healthy_samples": sum(row["healthy"] for row in samples),
                "elapsed_observation_hours": round(span / 3600, 4),
                "largest_sample_gap_s": round(biggest_gap, 2),
                "continuity_72h": span >= 72 * 3600 and biggest_gap <= 150,
                "alerts_opened": sum(row["event"] == "alert_opened" for row in samples),
                "alerts_closed": sum(row["event"] == "alert_closed" for row in samples),
            }
        )
    summary = {
        "since": since.isoformat(),
        "until": end.isoformat(),
        "watch_reports": reports,
        "samples_sha256": digest(rows),
        "samples": rows,
    }
    return write_report("pilot-observation", summary)


def write_report(name, result):
    path = Path("reports") / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    (path / "summary.json").write_text(canonical(result), encoding="utf-8")
    render_evaluation(result, path / "report.html")
    return {**result, "report": str((path / "report.html").resolve())}
