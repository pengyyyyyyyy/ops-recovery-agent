"""Archive frozen experiments without copying local credentials, raw data or environment paths."""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from pathlib import Path

from ops_recovery.config import Settings
from ops_recovery.evaluation import summarize
from ops_recovery.storage import Store, canonical


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("experiments", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/evaluation/2026-11-18"))
    parser.add_argument(
        "--commit", help="Optional public source commit; source_digest always records evaluated code"
    )
    arguments = parser.parse_args()
    rows, protocols, origin = [], [], {}
    for directory in arguments.experiments:
        protocol = json.loads((directory / "protocol.json").read_text(encoding="utf-8"))
        protocols.append(protocol)
        entries = [
            json.loads(line)
            for line in (directory / "results.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        if len(entries) != 30:
            raise ValueError(f"Expected all 30 runs in {directory.name}, found {len(entries)}")
        for entry in entries:
            if not re.fullmatch(r"[a-f0-9]{32}", entry["run_id"]):
                raise ValueError("Invalid run ID in archive input")
            origin[entry["run_id"]] = directory
        rows += entries
    for field in (
        "mode",
        "model",
        "source_digest",
        "prompt_digest",
        "dataset_revision",
        "dataset_manifest_digest",
        "repeats",
        "split",
    ):
        if len({str(protocol[field]) for protocol in protocols}) != 1:
            raise ValueError(f"Experiments disagree on {field}")
    identities = {(row["suite"], row["case"], row["variant"], row["repeat"]) for row in rows}
    if len(rows) != 90 or len(identities) != 90:
        raise ValueError("A full campaign needs exactly 90 unique run identities")
    output = arguments.output
    output.mkdir(parents=True, exist_ok=True)
    settings = Settings.load("offline")
    store = Store(settings.state_dir)
    protocol = dict(protocols[0])
    protocol["variants"] = {name: config for item in protocols for name, config in item["variants"].items()}
    protocol.pop("source_commit", None)
    if arguments.commit:
        protocol["source_commit"] = arguments.commit
    protocol["parallel_variant_processes"] = 3
    protocol["timing_caveat"] = (
        "Variant processes shared a host and API concurrently; latency comparisons are descriptive, not isolated performance benchmarks."
    )
    failures = [row for row in rows if not row["diagnosis_correct"]]
    summary = {
        "protocol": protocol,
        "summary": summarize(rows),
        "completed_runs": len(rows),
        "experiment_cost_ledger": store.cost([row["run_id"] for row in rows]),
        "failed_run_ids": [row["run_id"] for row in failures],
    }
    manifest = json.loads(Path("data/manifest.json").read_text(encoding="utf-8"))
    answers = json.loads(Path("data/answers/answers.json").read_text(encoding="utf-8"))
    expected = {row["id"]: row["service"] for row in answers}
    analysis = [
        {
            "case": row["case"],
            "variant": row["variant"],
            "repeat": row["repeat"],
            "expected_service": expected.get(row["case"]),
            "prediction": row.get("prediction"),
            "error": row.get("error"),
            "run_id": row["run_id"],
        }
        for row in failures
    ]
    outputs = {
        "protocol.json": canonical(protocol),
        "summary.json": canonical(summary),
        "results.jsonl": "\n".join(canonical(row) for row in rows) + "\n",
        "failure-analysis.json": canonical(analysis),
        "dataset-manifest.json": canonical(manifest),
    }
    secret = settings.api_key
    for name, contents in outputs.items():
        if secret and secret in contents:
            raise ValueError("Refusing to archive a configured credential")
        (output / name).write_text(contents, encoding="utf-8")
    table = []
    for variant in ("baseline", "reliable", "dual"):
        local = summary["summary"]["local"][variant]
        public = summary["summary"]["public"][variant]
        weighted = summary["summary"]["weighted_diagnosis"][variant]
        table.append(
            f"<tr><td>{variant}</td><td>{local['service_ac_at_1']:.1%}</td>"
            f"<td>{public['service_ac_at_1']:.1%}</td><td>{weighted:.1%}</td>"
            f"<td>{local['verified_repair_rate']:.1%}</td><td>{local['duplicate_repair_events']}</td>"
            f"<td>{public['mean_model_calls']:.2f}</td><td>{public['mean_elapsed_seconds']:.2f}s</td></tr>"
        )
    document = (
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ops Recovery Agent — 实测评测</title><style>body{margin:0;background:#f5f5ef;color:#20312b;font:15px/1.7 system-ui}main{max-width:1100px;margin:40px auto;padding:24px}h1{font-size:34px}section{background:white;padding:24px;margin:24px 0;border:1px solid #d9dfd7;border-radius:8px}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:12px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.label{color:#38634c}</style>
<main><p class="label">真实模型 / 固定版本 / 公开盲测</p><h1>运维排障 Agent 评测</h1>
<p>DeepSeek Flash · 3 类本地故障 + 12 个公开案例 · 3 组消融 · 各重复 2 次，共 90 次运行。</p>
<section><h2>结果对照</h2><table><thead><tr><th>变体</th><th>本地诊断</th><th>公开 AC@1</th><th>60/40 加权</th><th>本地修复</th><th>重复修复</th><th>公开模型调用</th><th>公开平均耗时</th></tr></thead><tbody>"""
        + "".join(table)
        + """</tbody></table>
<p>本地修复需要三次实际 HTTP 请求验证成功；测试审批使用明确标记的自动测试策略。公开静态数据只评诊断。</p>
<p>小样本及同案例重复结果相关，不代表生产准确率。三组共享主机并发运行，延迟只作描述。多 Agent 是否值得其额外成本，应根据实际结果判断。</p></section>
<section><h2>可复现协议与费用</h2><pre>"""
        + html.escape(json.dumps(summary, ensure_ascii=False, indent=2))
        + """</pre></section>
<section><h2>失败案例</h2><p>保留全部失败，没有依据测试结果修改提示词或只挑选成功运行。</p><pre>"""
        + html.escape(json.dumps(analysis, ensure_ascii=False, indent=2))
        + """</pre></section></main></html>"""
    )
    (output / "report.html").write_text(document, encoding="utf-8")
    # One local recovery and one public failure provide concrete traces, not just aggregate scores.
    selected = [next(row for row in rows if row["suite"] == "local" and row["variant"] == "dual")]
    selected += next(([row] for row in failures if row["suite"] == "public" and row["variant"] == "dual"), [])
    for row in selected:
        source = origin[row["run_id"]] / "traces" / f"{row['run_id']}.html"
        contents = source.read_text(encoding="utf-8")
        if secret and secret in contents:
            raise ValueError("Refusing to export a trace containing a configured credential")
        shutil.copyfile(source, output / ("trace-" + row["suite"] + ".html"))
    print(
        canonical(
            {
                "directory": str(output.resolve()),
                "runs": len(rows),
                "summary": summary["summary"],
                "experiment_cost_ledger": summary["experiment_cost_ledger"],
            }
        )
    )


if __name__ == "__main__":
    main()
