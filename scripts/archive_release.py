"""Archive complete comparable regression campaigns and anonymized pilot observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from collections import Counter
from pathlib import Path

from ops_recovery.config import Settings
from ops_recovery.evaluation import summarize
from ops_recovery.report import render_evaluation, render_run
from ops_recovery.storage import Store, canonical


def scrub(value, root):
    if isinstance(value, dict):
        return {
            key: scrub(item, root)
            for key, item in value.items()
            if key.lower() not in {"api_key", "authorization", "control_token"}
        }
    if isinstance(value, list):
        return [scrub(item, root) for item in value]
    if isinstance(value, str):
        return (
            value.replace(str(root).replace("\\", "\\\\"), "$PROJECT")
            .replace(str(root), "$PROJECT")
            .replace(root.as_posix(), "$PROJECT")
        )
    return value


def archive_campaign(directory, output, store, root, name):
    protocol = json.loads((directory / "protocol.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line) for line in (directory / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if len(rows) != 60 or len({(r["suite"], r["case"], r["variant"], r["repeat"]) for r in rows}) != 60:
        raise ValueError("A release campaign requires 60 unique run identities")
    for suite, expected in (("local", 6), ("public", 24)):
        for variant in ("reliable", "dual"):
            if sum(r["suite"] == suite and r["variant"] == variant for r in rows) != expected:
                raise ValueError("The frozen campaign matrix is incomplete")
    path = output / name
    path.mkdir(parents=True, exist_ok=True)
    trace_path = root / "reports" / "release-export" / name / "traces"
    trace_path.mkdir(parents=True, exist_ok=True)
    ids, failures, errors = [], [], Counter()
    for row in rows:
        run_id = row["run_id"]
        ids.append(run_id)
        run = store.get(run_id)
        events = store.events(run_id)
        record = scrub({"run": {**run, "backend": {"kind": run["backend"]["kind"]}}, "events": events}, root)
        (trace_path / (run_id + ".json")).write_text(canonical(record), encoding="utf-8")
        temporary = trace_path / (run_id + ".html")
        render_run(store, run_id, temporary)
        temporary.write_text(scrub(temporary.read_text(encoding="utf-8"), root), encoding="utf-8")
        if row.get("error") or not row["diagnosis_correct"]:
            message = (row.get("error") or {}).get("message", "incorrect service")
            errors[message] += 1
            failures.append(
                {
                    "case": row["case"],
                    "variant": row["variant"],
                    "repeat": row["repeat"],
                    "run_id": run_id,
                    "scoring_prediction": row.get("prediction"),
                    "persisted_diagnosis": run.get("diagnosis"),
                    "error": row.get("error"),
                    "phase": run["phase"],
                    "rejected_outputs": [e["payload"] for e in events if e["kind"] == "output_rejected"],
                }
            )
    members = []
    with tarfile.open(path / "traces.tar.gz", "w:gz") as bundle:
        for run_id in ids:
            for suffix in (".json", ".html"):
                file = trace_path / (run_id + suffix)
                content = file.read_bytes()
                members.append(
                    {"path": file.name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                )
                info = bundle.gettarinfo(str(file), arcname=file.name)
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                with file.open("rb") as source:
                    bundle.addfile(info, source)
    (path / "trace-manifest.json").write_text(canonical(members), encoding="utf-8")
    protocol["evaluation_status"] = (
        "regression; same case groups have been used in prior evaluation and engineering diagnosis"
    )
    protocol["limitations"] = [
        "Small historical regression sample; not unseen held-out evaluation or a full RCAEval reproduction"
        if "held-out" in item
        else item
        for item in protocol["limitations"]
    ]
    protocol["aggregation"] = {"local": 0.6, "public": 0.4}
    summary = {
        "protocol": protocol,
        "completed_runs": len(rows),
        "summary": summarize(rows),
        "experiment_cost_ledger": store.cost(ids),
        "failure_reasons": dict(errors),
    }
    for filename, value in (
        ("protocol.json", protocol),
        ("summary.json", summary),
        ("failure-analysis.json", failures),
    ):
        (path / filename).write_text(canonical(scrub(value, root)), encoding="utf-8")
    (path / "results.jsonl").write_text(
        "".join(canonical(scrub(row, root)) + "\n" for row in rows), encoding="utf-8"
    )
    render_evaluation(scrub(summary, root), path / "report.html")
    return summary


def archive_pilot(output, root):
    source = root / "reports"
    facts = json.loads((source / "cloud-continuity-2026-11-23-summary.json").read_text(encoding="utf-8"))
    samples = json.loads((source / "cloud-continuity-2026-11-23-samples.json").read_text(encoding="utf-8"))
    rows = samples["rows"]
    if len(rows) != facts["samples"] or samples["source_full_rows_sha256"] != facts["sample_sha256"]:
        raise ValueError("Pilot observation export does not match its source audit")
    gaps = [b[0] - a[0] for a, b in zip(rows, rows[1:])]
    bad = [r for r in rows if not r[1]]
    clean = [r for r in rows if r[0] > bad[-1][0]] if bad else rows
    clean_hours = (clean[-1][0] - clean[0][0]) / 3600
    if clean_hours < 72 or max(gaps) > 150 or not all(r[1] for r in clean):
        raise ValueError("The pilot continuity check did not pass")
    path = output / "pilot"
    path.mkdir(parents=True, exist_ok=True)
    report = {
        "observation": facts,
        "clean_post_fault_hours": round(clean_hours, 4),
        "sampled_healthy_fraction": sum(r[1] for r in rows) / len(rows),
        "acceptance": "passed",
        "load": json.loads(
            (source / "aliyun-v020-baseline-test-2026-11-19.json").read_text(encoding="utf-8")
        ),
        "statistics": (source / "aliyun-v020-umami-statistics-2026-11-19.txt").read_text(encoding="utf-8"),
        "scope": "Dedicated Umami/PostgreSQL pilot with synthetic requests and an offline diagnostic model. Paid model measurements are separate local regression campaigns.",
    }
    (path / "summary.json").write_text(canonical(report), encoding="utf-8")
    (path / "samples.json").write_text(canonical(samples), encoding="utf-8")
    render_evaluation(report, path / "report.html")
    return report


def archive_supporting(output, root):
    sources = {
        "context.json": "context-ablation-385d889d/summary.json",
        "skills.json": "skill-ablation-81fb33a9/summary.json",
        "concurrency.json": "session-concurrency-114a880e/summary.json",
        "skill-lifecycle.json": "v020-skill-activation-fixture.json",
        "cloud-faults.txt": "aliyun-v020-fault-test-2026-11-19.txt",
        "cloud-worker-recovery.txt": "aliyun-v020-worker-test-2026-11-19.txt",
        "cloud-config-recovery.txt": "aliyun-v020-config-recovery-2026-11-19.txt",
    }
    path = output / "supporting"
    path.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        content = (root / "reports" / source).read_text(encoding="utf-8")
        if name.endswith(".json"):
            content = canonical(scrub(json.loads(content), root))
        else:
            content = scrub(content, root)
        (path / name).write_text(content, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/evaluation/2026-11-23"))
    args = parser.parse_args()
    root = Path.cwd().resolve()
    settings = Settings.load("offline")
    store = Store(settings.state_dir)
    before = archive_campaign(args.before, args.output, store, root, "before")
    after = archive_campaign(args.after, args.output, store, root, "after")
    pilot = archive_pilot(args.output, root)
    archive_supporting(args.output, root)
    overview = {
        "before": before["summary"],
        "after": after["summary"],
        "pilot": {
            "acceptance": pilot["acceptance"],
            "clean_post_fault_hours": pilot["clean_post_fault_hours"],
        },
    }
    render_evaluation(overview, args.output / "report.html")
    # Preserve hashed evidence bytes on checkout on both Windows and Linux.
    (args.output / ".gitattributes").write_bytes(b"** -text\n")
    secret = settings.api_key
    manifest = []
    for file in sorted(args.output.rglob("*")):
        if file.is_file() and file != args.output / "manifest.json":
            content = file.read_bytes()
            if secret and secret.encode() in content:
                raise ValueError("A configured credential was found in an archive artifact")
            manifest.append(
                {
                    "path": file.relative_to(args.output).as_posix(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                }
            )
    for file in (root / "reports" / "release-export").rglob("*"):
        if file.is_file() and secret and secret.encode() in file.read_bytes():
            raise ValueError("A configured credential was found in an expanded trace")
    (args.output / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    print(canonical({"output": str(args.output), "files": len(manifest), **overview}))


if __name__ == "__main__":
    main()
