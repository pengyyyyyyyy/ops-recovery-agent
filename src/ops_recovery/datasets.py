"""Versioned public data import with group-separated splits and anonymous evidence exports."""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

from .storage import canonical, digest

REPOSITORY = "phamquiluan/RCAEval"


def metric_evidence(frame, alert_time: int) -> list[dict]:
    import numpy as np

    before = frame[(frame.time >= alert_time - 300) & (frame.time < alert_time)]
    after = frame[(frame.time >= alert_time) & (frame.time <= alert_time + 300)]
    if len(before) < 10 or len(after) < 10:
        raise ValueError("Insufficient normal/fault observation window")
    evidence = []
    for column in frame.columns:
        if column == "time" or "_" not in column:
            continue
        normal = before[column].dropna().to_numpy(dtype=float)
        faulty = after[column].dropna().to_numpy(dtype=float)
        normal, faulty = normal[np.isfinite(normal)], faulty[np.isfinite(faulty)]
        if not len(normal) or not len(faulty):
            continue
        baseline, current = float(np.median(normal)), float(np.median(faulty))
        mad = float(np.median(np.abs(normal - baseline)))
        score = abs(current - baseline) / max(1.4826 * mad, abs(baseline) * 0.05, 1e-6)
        service, metric = column.split("_", 1)
        evidence.append(
            {
                "id": "metric-" + digest(column)[:16],
                "kind": "metric",
                "service": service,
                "metric": metric,
                "baseline_median": round(baseline, 5),
                "incident_median": round(current, 5),
                "anomaly_score": round(min(score, 1e8), 3),
                "window_seconds": 300,
                "normal_samples": len(normal),
                "incident_samples": len(faulty),
            }
        )
    return sorted(evidence, key=lambda row: row["anomaly_score"], reverse=True)


def choose_cases(rows: list[dict], dev_count: int, test_count: int, seed: int) -> list[tuple[str, dict]]:
    if not 1 <= dev_count <= 10 or not 1 <= test_count <= 24:
        raise ValueError("Choose 1..10 development groups and 1..24 test groups")
    groups = defaultdict(list)
    for row in rows:
        if row["dataset"] == "RE2-OB":
            groups[(row["root_cause_service"], row["fault"])].append(row)
    rng = random.Random(seed)
    keys = sorted(groups)
    rng.shuffle(keys)
    # The schema-inspection case is permanently development-only.
    known_dev = ("checkoutservice", "cpu")
    keys.remove(known_dev)
    dev_keys = [known_dev, *keys[: dev_count - 1]]
    remaining = [key for key in keys if key not in dev_keys]
    by_fault = defaultdict(list)
    for key in remaining:
        by_fault[key[1]].append(key)
    test_keys = []
    while len(test_keys) < test_count:
        changed = False
        for fault in sorted(by_fault):
            if by_fault[fault] and len(test_keys) < test_count:
                test_keys.append(by_fault[fault].pop(0))
                changed = True
        if not changed:
            raise ValueError("Not enough disjoint fault-service groups")
    assert set(dev_keys).isdisjoint(test_keys)
    return [
        (split, sorted(groups[key], key=lambda row: row["repetition"])[0])
        for split, selected in (("dev", dev_keys), ("test", test_keys))
        for key in selected
    ]


def import_rcaeval(
    directory: Path, *, dev_count: int = 3, test_count: int = 12, seed: int = 20261118
) -> dict:
    import pyarrow.parquet as parquet
    from huggingface_hub import HfApi, hf_hub_download

    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest_file = directory / "manifest.json"
    if manifest_file.exists():
        previous = json.loads(manifest_file.read_text(encoding="utf-8"))
        if (previous["dev_count"], previous["test_count"], previous["seed"]) != (dev_count, test_count, seed):
            raise ValueError("Dataset already frozen; use a different data directory for a different split")
        return previous
    revision_file = directory / "raw" / "revision.txt"
    revision = (
        revision_file.read_text().strip()
        if revision_file.exists()
        else HfApi().dataset_info(REPOSITORY, token=False).sha
    )

    def download(name):
        return Path(
            hf_hub_download(
                REPOSITORY,
                name,
                repo_type="dataset",
                revision=revision,
                token=False,
                local_dir=directory / "raw",
            )
        )

    index = parquet.read_table(download("cases.parquet")).to_pylist()
    selected = choose_cases(index, dev_count, test_count, seed)
    answers, files = [], []
    counters = defaultdict(int)
    for split, row in selected:
        counters[split] += 1
        case_id = f"{split}-{counters[split]:03}"
        source = row["case"]
        metrics_path, logs_path = download(source + "/metrics.parquet"), download(source + "/logs.parquet")
        metrics = parquet.read_table(metrics_path).to_pandas()
        logs = parquet.read_table(logs_path).to_pandas()
        alert_time = int(row["inject_time"])
        evidence = metric_evidence(metrics, alert_time)
        services = sorted({item["service"] for item in evidence})
        matching = logs[(logs.timestamp >= alert_time) & (logs.timestamp <= alert_time + 300)]
        matching = matching[
            matching.message.str.contains(
                "error|exception|fail|timeout|refused|deadline|unavailable", case=False, regex=True, na=False
            )
        ]
        matching = (
            matching.drop_duplicates(["container_name", "message"])
            .groupby("container_name", sort=False)
            .head(4)
        )
        for i, log in enumerate(matching.to_dict("records")[:40]):
            service = str(log["container_name"])
            if service not in services:
                continue
            evidence.append(
                {
                    "id": f"log-{i:04}",
                    "kind": "log",
                    "service": service,
                    "timestamp": int(log["timestamp"]),
                    "message": str(log["message"])[:600],
                }
            )
        evidence.append(
            {
                "id": "rb-public",
                "kind": "documentation",
                "service": "all",
                "text": "Compare pre-alert and post-alert metrics. CPU, memory, disk I/O and sockets may reveal resource faults. Latency/errors can be downstream symptoms. Corroborate with logs. This is static telemetry; do not propose executable repairs.",
            }
        )
        target = directory / "blind" / split / case_id
        target.mkdir(parents=True, exist_ok=True)
        task = {
            "id": case_id,
            "services": services,
            "alert_time": alert_time,
            "telemetry_digest": digest(evidence),
            "observation_window_seconds": 300,
        }
        (target / "task.json").write_text(canonical(task), encoding="utf-8")
        (target / "evidence.json").write_text(canonical(evidence), encoding="utf-8")
        answers.append(
            {
                "id": case_id,
                "split": split,
                "service": row["root_cause_service"],
                "fault": row["fault"],
                "source_case": source,
                "group": [row["root_cause_service"], row["fault"]],
            }
        )
        for path in (metrics_path, logs_path):
            files.append(
                {
                    "file": str(path.relative_to(directory)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    private = directory / "answers"
    private.mkdir(exist_ok=True)
    (private / "answers.json").write_text(canonical(answers), encoding="utf-8")
    manifest = {
        "repository": REPOSITORY,
        "revision": revision,
        "license": "MIT (upstream dataset declaration)",
        "seed": seed,
        "dev_count": dev_count,
        "test_count": test_count,
        "split_unit": "root-cause-service + fault-type; all repetitions kept on one side",
        "modalities": ["metrics", "logs"],
        "preprocessing": "fixed median/MAD, 300-second pre/post windows",
        "alert_time_information": "Benchmark injection timestamp is supplied as the incident alarm time; this is RCA with known incident time, not anomaly detection.",
        "files": files,
    }
    manifest_file.write_text(canonical(manifest), encoding="utf-8")
    return manifest
