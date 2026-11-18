from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from .storage import Store


def escaped(value) -> str:
    return html.escape(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def render_run(store: Store, run_id: str, output: Path) -> Path:
    run = store.get(run_id)
    events = store.events(run_id)
    rows = "".join(
        f"<tr><td>{event['seq']}</td><td>{datetime.fromtimestamp(event['timestamp'], timezone.utc).isoformat(timespec='milliseconds')}</td>"
        f"<td>{html.escape(event['kind'])}</td><td><pre>{escaped(event['payload'])}</pre></td></tr>"
        for event in events
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    label = "ONLINE MODEL RUN" if run["mode"] == "online" else "OFFLINE ENGINEERING CHECK — NOT LLM ACCURACY"
    output.write_text(
        f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Incident {html.escape(run_id)}</title>
<style>body{{font:15px/1.6 system-ui,sans-serif;color:#20312b;background:#f5f5ef;margin:0}}main{{max-width:1100px;margin:40px auto;padding:24px}}h1{{font-size:32px}}.label{{color:#38634c}}section{{background:white;padding:24px;margin:20px 0;border:1px solid #d9dfd7;border-radius:8px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;text-align:left;vertical-align:top;padding:10px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}small{{color:#5a665f}}</style>
<main><p class="label">{label}</p><h1>Incident investigation</h1><p>{html.escape(run_id)} · {html.escape(run["phase"])}</p>
<small>Read tools, model calls, approvals and recovery events are ordered by a persistent SQLite sequence.</small>
<section><h2>Diagnosis</h2><pre>{escaped(run.get("diagnosis"))}</pre><h2>Independent-context review</h2><pre>{escaped(run.get("review"))}</pre></section>
<section><h2>Approval and outcome</h2><pre>{escaped({key: run.get(key) for key in ("phase", "proposal", "proposal_digest", "approval", "verification", "error")})}</pre></section>
<section><h2>Observed evidence</h2><pre>{escaped(run["evidence"])}</pre></section>
<section><h2>Trace (UTC)</h2><table><thead><tr><th>Seq</th><th>Time</th><th>Event</th><th>Details</th></tr></thead><tbody>{rows}</tbody></table></section></main></html>""",
        encoding="utf-8",
    )
    return output


def render_evaluation(result: dict, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "<!doctype html><meta charset='utf-8'><title>Evaluation report</title>"
        "<style>body{font:15px/1.6 system-ui;max-width:1100px;margin:40px auto;padding:24px;background:#f5f5ef;color:#20312b}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:24px;border:1px solid #ddd}</style>"
        "<h1>Reproducible evaluation</h1><p>Offline checks and online model measurements are reported separately. "
        "Public replay evaluates diagnosis only; local repair results use real HTTP probes.</p><pre>"
        + escaped(result)
        + "</pre>",
        encoding="utf-8",
    )
    return output
