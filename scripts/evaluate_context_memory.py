"""Compare retention and cost on synthetic, reproducible incident transcripts."""

from __future__ import annotations

import argparse
import statistics
import uuid

from pydantic import BaseModel, ConfigDict

from ops_recovery.agent_loop import AgentLoop
from ops_recovery.config import Settings
from ops_recovery.models import make_model
from ops_recovery.session_evaluation import write_report
from ops_recovery.sessions import Sessions
from ops_recovery.storage import Store, canonical
from ops_recovery.tools import PriorTurnTool


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    constraint: str


CASES = [
    ("journal-cursor", "JRN-7631", "journal read only"),
    ("upstream-sample", "UP-4517", "no dependency restart"),
    ("database-receipt", "DB-3958", "no database write"),
    ("rollback-proof", "RB-6813", "keep configuration unchanged"),
]


def fixture(sessions, name, code, constraint):
    backend = {"kind": "context-fixture"}
    sid = sessions.create(backend, "online", "Synthetic test of historical evidence recall.", [constraint])
    padding = "Collector captured an ordinary health metric with no relevant incident evidence. "
    sessions.tell(
        sid,
        f"Verified source for {name}. {padding * 6} The verified historical code is {code}.",
        "verification",
    )
    for index in range(12):
        message = f"Background turn {index}. " + padding * 10
        if index == 11:
            message += f" A speculation for {name} proposed XYZ-0000 without verification."
        sessions.tell(sid, message, f"background-{index}")
    question = f"Return the exact verified historical code for {name} and the exact operator constraint."
    run_id = sessions.tell(sid, question, "query")
    history = sessions.history(sid)
    job = history["turns"][-1]
    prior = history["turns"][:-1]
    layer = sessions.context(job)
    return run_id, question, layer, [turn["message"] for turn in prior]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, choices=range(1, len(CASES) + 1), default=len(CASES))
    args = parser.parse_args()
    settings = Settings.load("online")
    store = Store(settings.state_dir)
    fixtures = Store(settings.state_dir / ("context-fixture-" + uuid.uuid4().hex[:8]))
    sessions = Sessions(fixtures)
    results = []
    used_runs = []
    for name, expected, constraint in CASES[: args.cases]:
        original, question, layer, prior_messages = fixture(sessions, name, expected, constraint)
        variants = {
            "full": (
                canonical(
                    {"operator_constraints": [constraint], "question": question, "history": prior_messages}
                ),
                [],
            ),
            "truncated": (
                canonical(
                    {
                        "operator_constraints": [constraint],
                        "question": question,
                        "history": prior_messages[-2:],
                    }
                ),
                [],
            ),
            "layered": (layer, [PriorTurnTool(fixtures, original)]),
        }
        for variant, (context, tools) in variants.items():
            run_id = store.create({"kind": "context-evaluation"}, "online")
            used_runs.append(run_id)
            loop = AgentLoop(
                model=make_model(settings, store, run_id, "context-evaluation"),
                tools=tools,
                max_steps=5,
                instructions=(
                    "Answer only from the supplied incident record. If a historical source is referenced "
                    "by run ID and a retrieval tool is available, retrieve it. A speculation is not a verified "
                    "code. Use 'unknown' when absent. Submit the exact operator constraint without paraphrasing."
                ),
                check=Answer.model_validate,
                answer_schema=Answer.model_json_schema(),
                trace=lambda kind, data: store.event(run_id, kind, data),
            )
            try:
                answer, error = Answer.model_validate(loop.run(context)).model_dump(), None
            except Exception as exc:
                answer, error = {}, str(exc)[:350]
            events = store.events(run_id)
            prompt_tokens = sum(
                (row["payload"].get("usage") or {}).get("prompt_tokens", 0)
                for row in events
                if row["kind"] == "model_response"
            )
            row = {
                "case": name,
                "variant": variant,
                "context_bytes": len(context.encode()),
                "constraint_correct": answer.get("constraint") == constraint,
                "evidence_correct": answer.get("code") == expected,
                "answer": answer,
                "error": error,
                "prompt_tokens": prompt_tokens,
                "trace_run_id": run_id,
                "history_run_id": original,
                "model_calls": sum(r["kind"] == "model_request" for r in events),
            }
            results.append(row)
            print(canonical(row), flush=True)
    totals = {}
    for variant in ("full", "truncated", "layered"):
        rows = [r for r in results if r["variant"] == variant]
        totals[variant] = {
            "runs": len(rows),
            "constraints_retained": sum(r["constraint_correct"] for r in rows),
            "evidence_retrieved": sum(r["evidence_correct"] for r in rows),
            "mean_initial_bytes": statistics.mean(r["context_bytes"] for r in rows),
            "mean_prompt_tokens_including_tools": statistics.mean(r["prompt_tokens"] for r in rows),
        }
    print(
        canonical(
            write_report(
                "context-ablation",
                {
                    "model": settings.model_id,
                    "kind": "synthetic retention tasks",
                    "summary": totals,
                    "results": results,
                    "cost": store.cost(used_runs),
                },
            )
        )
    )


if __name__ == "__main__":
    main()
