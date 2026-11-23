import json

from ops_recovery.agent_loop import AgentLoop, bounded_result
from ops_recovery.models import tool_message


def test_large_metric_result_keeps_numeric_observations():
    result = {
        "ok": True,
        "data": {
            "evidence": [
                {
                    "id": f"m-{i}",
                    "service": "api",
                    "kind": "metric",
                    "metric": "latency",
                    "baseline_median": 0.01,
                    "incident_median": 2.0,
                    "anomaly_score": 99,
                    "description": "redundant description " * 300,
                }
                for i in range(40)
            ]
        },
    }
    summary = json.loads(bounded_result(result, 5500))
    evidence = summary["data"]["evidence"]
    assert evidence and evidence[0]["incident_median"] == 2.0
    assert evidence[0]["baseline_median"] == 0.01
    assert len(json.dumps(summary, ensure_ascii=False, separators=(",", ":")).encode()) <= 5500


def test_last_step_exposes_only_final_answer_and_remaining_budget():
    seen = []

    class Model:
        def generate(self, messages, **kwargs):
            seen.append((messages, [tool.name for tool in kwargs["tools_to_call_from"]]))
            return tool_message("final_answer", {"answer": {"ok": True}})

    loop = AgentLoop(
        model=Model(),
        tools=[],
        max_steps=1,
        instructions="Investigate",
        check=lambda x: True,
        trace=lambda kind, data: None,
    )
    assert loop.run("Task") == {"ok": True}
    assert seen[0][1] == ["final_answer"]
    assert "final" in seen[0][0][-1]["content"].lower()
