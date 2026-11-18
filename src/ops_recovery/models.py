from __future__ import annotations

import time
import uuid
from dataclasses import replace

import httpx
from smolagents import Model
from smolagents.models import ChatMessage
from smolagents.monitoring import TokenUsage

from .config import Settings
from .storage import Store, canonical, digest


def tool_message(name: str, arguments: dict) -> ChatMessage:
    return ChatMessage.from_dict(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": uuid.uuid4().hex,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            ],
        }
    )


def heuristic(evidence: list[dict]) -> dict:
    """Transparent offline baseline; consumes observations, never ground-truth labels."""
    for signal, service, cause, action in [
        ("process_exited", "gateway", "process_crash", "restart_api"),
        ("database_locked", "database", "database_lock", "release_database_lock"),
        ("upstream_timeout", "inventory", "dependency_timeout", "reset_dependency_delay"),
    ]:
        rows = [row for row in evidence if row.get("signal") == signal]
        if rows:
            return {
                "service": service,
                "root_cause": cause,
                "summary": "Offline rule matched an observed " + signal,
                "evidence_ids": [row["id"] for row in rows[:3]],
                "action": action,
            }
    ranked = sorted(
        (row for row in evidence if row.get("kind") == "metric"),
        key=lambda row: row.get("anomaly_score", 0),
        reverse=True,
    )
    if ranked:
        row = ranked[0]
        return {
            "service": row["service"],
            "root_cause": "metric_anomaly",
            "summary": "Offline metric-change baseline",
            "evidence_ids": [row["id"]],
            "action": "none",
        }
    observations = [row for row in evidence if not row["id"].startswith("rb-")]
    return {
        "service": "unknown",
        "root_cause": "unknown",
        "summary": "Insufficient evidence; abstain",
        "evidence_ids": [row["id"] for row in observations[:1]],
        "action": "none",
    }


class OfflineModel(Model):
    def __init__(self, store: Store, run_id: str, role: str):
        super().__init__(model_id="offline-scripted-engineering-baseline")
        self.store, self.run_id, self.role, self.step = store, run_id, role, 0

    def generate(self, messages, **kwargs):
        run = self.store.get(self.run_id)
        if self.role == "reviewer":
            diagnosis = run["diagnosis"]
            if self.step == 0:
                self.step += 1
                return tool_message("get_evidence", {"evidence_ids": diagnosis["evidence_ids"]})
            observed = run["evidence"]
            accepted = (
                all(key in observed for key in diagnosis["evidence_ids"])
                and diagnosis["service"] != "unknown"
            )
            return tool_message(
                "final_answer",
                {
                    "answer": {
                        "accepted": accepted,
                        "reason": "Offline structural evidence check; not an independent LLM judgment",
                        "evidence_ids": diagnosis["evidence_ids"],
                    }
                },
            )
        plan = [
            ("list_services", {}),
            ("query_logs", {"service": "all"}),
            ("query_metrics", {"service": "all"}),
            ("search_runbooks", {"query": "incident troubleshooting"}),
        ]
        if self.step < len(plan):
            name, arguments = plan[self.step]
            self.step += 1
            return tool_message(name, arguments)
        return tool_message("final_answer", {"answer": heuristic(list(run["evidence"].values()))})


class HTTPModel(Model):
    """One HTTP attempt per generate; durable reservations include failed/unknown requests."""

    def __init__(self, settings: Settings, store: Store, run_id: str, role: str):
        super().__init__(model_id=settings.model_id, flatten_messages_as_text=True)
        self.settings, self.store, self.run_id, self.role = settings, store, run_id, role
        self.started = time.monotonic()

    def generate(
        self, messages, stop_sequences=None, response_format=None, tools_to_call_from=None, **kwargs
    ):
        remaining = self.settings.agent_deadline - (time.monotonic() - self.started)
        if remaining <= 0:
            raise TimeoutError("Agent deadline exceeded")
        normalized = []
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else message.content
            if not isinstance(content, list):
                blocks = [{"type": "text", "text": content or ""}]
                message = (
                    {**message, "content": blocks}
                    if isinstance(message, dict)
                    else replace(message, content=blocks)
                )
            normalized.append(message)
        payload = self._prepare_completion_kwargs(
            normalized,
            stop_sequences=stop_sequences,
            response_format=response_format,
            tools_to_call_from=tools_to_call_from,
            model=self.model_id,
            max_tokens=self.settings.max_output,
            temperature=0,
            **kwargs,
        )
        if self.settings.api_style == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        serialized = canonical(payload)
        if len(serialized.encode()) > 120_000:
            raise ValueError("Model context exceeds the project's bounded input budget")
        # UTF-8 bytes plus an explicit protocol margin bound text-token usage conservatively.
        upper_input = len(serialized.encode()) + 8192
        reservation = (
            upper_input * self.settings.input_price + self.settings.max_output * self.settings.output_price
        ) / 1_000_000
        request_id = self.store.reserve(self.run_id, reservation, self.settings.budget)
        self.store.event(
            self.run_id,
            "model_request",
            {
                "role": self.role,
                "model": self.model_id,
                "request_id": request_id,
                "input_digest": digest(payload),
                "reserved_cny": reservation,
                "request": payload,
            },
        )
        try:
            with httpx.Client(timeout=min(self.settings.model_timeout, remaining)) as client:
                response = client.post(
                    self.settings.base_url + "/chat/completions",
                    json=payload,
                    headers={"Authorization": "Bearer " + self.settings.api_key},
                )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Model provider returned HTTP {response.status_code}; reservation retained"
                )
            data = response.json()
            usage = data.get("usage")
            token_usage = None
            if usage and "prompt_tokens" in usage and "completion_tokens" in usage:
                inputs, outputs = usage["prompt_tokens"], usage["completion_tokens"]
                actual = (
                    inputs * self.settings.input_price + outputs * self.settings.output_price
                ) / 1_000_000
                self.store.settle(request_id, actual, inputs, outputs)
                token_usage = TokenUsage(input_tokens=inputs, output_tokens=outputs)
            self.store.event(
                self.run_id,
                "model_response",
                {
                    "role": self.role,
                    "request_id": request_id,
                    "usage": usage,
                    "provider_model": data.get("model"),
                    "system_fingerprint": data.get("system_fingerprint"),
                    "message": data["choices"][0]["message"],
                },
            )
            return ChatMessage.from_dict(data["choices"][0]["message"], token_usage=token_usage)
        except httpx.HTTPError as error:
            raise RuntimeError("Model request failed; conservative reservation retained") from error


def make_model(settings: Settings, store: Store, run_id: str, role: str):
    return (
        OfflineModel(store, run_id, role)
        if settings.mode == "offline"
        else HTTPModel(settings, store, run_id, role)
    )
