from __future__ import annotations

import json

from smolagents import Tool

from .storage import canonical


class FinalAnswer(Tool):
    name = "final_answer"
    description = "Submit the final structured answer for validation. Call this tool by itself."
    inputs = {"answer": {"type": "object", "description": "The object required by the task schema"}}
    output_type = "object"

    def __init__(self, answer_schema=None):
        if answer_schema is not None:
            self.inputs = {"answer": {**answer_schema, "description": "Validated final answer"}}
        super().__init__()

    def forward(self, answer: dict) -> dict:
        return answer


def bounded_result(result: dict, limit: int) -> str:
    serialized = canonical(result)
    if len(serialized.encode()) <= limit:
        return serialized
    data = result.get("data", {})
    rows = data.get("evidence", []) if isinstance(data, dict) else []
    # Keep causal measurements when trimming verbose observations. IDs alone force
    # the next step to fetch every observation again within the same step budget.
    fields = (
        "id",
        "service",
        "kind",
        "signal",
        "metric",
        "anomaly_score",
        "baseline_median",
        "incident_median",
    )
    ranked = sorted(rows, key=lambda row: row.get("anomaly_score", 0), reverse=True)
    compact = {
        "ok": result.get("ok"),
        "data": {
            "evidence": [],
            "trimmed": True,
            "total_observations": len(rows),
            "retrieval": "get_evidence can retrieve previously observed IDs",
        },
    }
    for row in ranked:
        observation = {key: row[key] for key in fields if key in row}
        compact["data"]["evidence"].append(observation)
        if len(canonical(compact).encode()) > limit:
            compact["data"]["evidence"].pop()
            break
    return canonical(compact)


class AgentLoop:
    """A bounded model/tool loop with one result for every provider tool-call ID."""

    def __init__(self, *, model, tools, max_steps, instructions, check, trace, answer_schema=None):
        self.model = model
        self.tools = {tool.name: tool for tool in tools}
        self.final = FinalAnswer(answer_schema)
        self.max_steps = max_steps
        self.instructions = instructions
        self.check = check
        self.trace = trace

    def run(self, task: str):
        initial = [{"role": "system", "content": self.instructions}, {"role": "user", "content": task}]
        exchanges: list[list[dict]] = []
        correction = ""
        last_error = None
        for step in range(1, self.max_steps + 1):
            history = [*initial, *(item for exchange in exchanges for item in exchange)]
            if correction:
                history.append({"role": "user", "content": correction})
            remaining = self.max_steps - step
            history.append(
                {
                    "role": "user",
                    "content": (
                        f"Investigation calls remaining after this request: {remaining}. "
                        "Finish with final_answer once the evidence is sufficient."
                        if remaining
                        else "This is the final request. Call final_answer now using the evidence already observed. "
                        "Report unknown or reject if evidence is insufficient."
                    ),
                }
            )
            response = self.model.generate(
                history,
                tools_to_call_from=[*self.tools.values(), self.final] if remaining else [self.final],
                native_tool_history=True,
            )
            calls = response.tool_calls or []
            self.trace("loop_step", {"step": step, "tools": [call.function.name[:80] for call in calls[:8]]})
            correction = ""
            if not calls or len(calls) > 4 or len({call.id for call in calls}) != len(calls):
                correction = (
                    "Return one to four distinct tool calls. Submit a validated final_answer by itself."
                )
                if not calls:
                    last_error = json.JSONDecodeError("Expected final_answer tool call", "", 0)
                continue
            exchange = [{"role": "assistant", "content": "", "tool_calls": []}]
            for call in calls:
                name, raw = call.function.name, call.function.arguments
                raw = raw if isinstance(raw, str) else canonical(raw)
                exchange[0]["tool_calls"].append(
                    {"id": call.id, "type": "function", "function": {"name": name, "arguments": raw[:4096]}}
                )
                try:
                    if len(raw) > 4096:
                        raise ValueError("Tool arguments exceed 4096 characters")
                    arguments = json.loads(raw)
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be an object")
                    if name == self.final.name:
                        if len(calls) != 1 or set(arguments) != {"answer"}:
                            raise ValueError("Call final_answer alone with a single answer field")
                        self.check(arguments["answer"])
                        return arguments["answer"]
                    if name not in self.tools:
                        raise ValueError("Tool is outside the read allowlist")
                    result = self.tools[name](**arguments)
                except ValueError as error:
                    last_error = error
                    result = {"ok": False, "error": {"message": str(error)[:500]}}
                exchange.append(
                    {"role": "tool", "tool_call_id": call.id, "content": bounded_result(result, 16000)}
                )
            exchanges.append(exchange)
            # Keep provider call/result pairs intact; full observations live in the trace and evidence store.
            while len(exchanges) > 1 and len(canonical(exchanges).encode()) > 26_000:
                exchanges.pop(0)
                self.trace("loop_compacted", {"step": step})
        if last_error is not None:
            raise last_error
        raise ValueError("Agent step limit reached without a validated final answer")
