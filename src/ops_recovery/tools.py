from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from smolagents import Tool

from .domain import ToolFault
from .execution import ReliableExecutor
from .storage import Store


class ServiceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str = Field(min_length=1, max_length=120)


class SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=200)


class EvidenceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


class ReadTool(Tool):
    output_type = "object"
    skip_forward_signature_validation = True  # Dynamic schemas; forward validates each argument model below.

    def __init__(self, name: str, backend, store: Store, run_id: str, *, reliable: bool = True):
        self.name = name
        self.description = {
            "list_services": "List the available services and observed process health. Call this first.",
            "query_logs": "Query recent incident logs for a service, or use service='all'. Returns evidence IDs.",
            "query_metrics": "Query bounded metric summaries for a service, or service='all'.",
            "search_runbooks": "Search trusted generic runbooks. Runbooks are guidance, not proof of an incident.",
            "get_evidence": "Retrieve exact previously observed evidence by ID for independent review.",
        }[name]
        self.inputs = (
            {}
            if name == "list_services"
            else (
                {
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Previously observed evidence IDs",
                    }
                }
                if name == "get_evidence"
                else {"query": {"type": "string", "description": "Runbook search query"}}
                if name == "search_runbooks"
                else {"service": {"type": "string", "description": "A listed service name, or all"}}
            )
        )
        self.backend, self.store, self.run_id = backend, store, run_id
        self.executor = ReliableExecutor(store, run_id, attempts=3 if reliable else 1)
        super().__init__()

    def forward(self, **arguments):
        try:
            if self.name == "get_evidence":
                validated = EvidenceArguments.model_validate(arguments)
                existing = self.store.get(self.run_id)["evidence"]
                if any(key not in existing for key in validated.evidence_ids):
                    raise ToolFault("invalid_evidence", "An evidence ID was not observed in this run")
                result = {"evidence": [existing[key] for key in validated.evidence_ids]}
                checked = self.store.get(self.run_id).get("review_checked_ids", [])
                self.store.update(
                    self.run_id, review_checked_ids=sorted(set(checked + validated.evidence_ids))
                )
                return self.executor.call(self.name, arguments, lambda: result)
            if self.name in {"query_logs", "query_metrics"}:
                arguments = ServiceArguments.model_validate(arguments).model_dump()
            elif self.name == "search_runbooks":
                arguments = SearchArguments.model_validate(arguments).model_dump()
            elif arguments:
                raise ToolFault("invalid_arguments", "This tool accepts no arguments")
            result = self.executor.call(self.name, arguments, lambda: self.backend.read(self.name, arguments))
            if result["ok"]:
                run = self.store.get(self.run_id)
                observed = run["evidence"]
                if self.name == "search_runbooks" and run.get("session_id"):
                    from .skills import Skills

                    guides = Skills(self.store).match(self.backend.spec["kind"], observed.values())
                    result["data"].setdefault("evidence", []).extend(guides)
                    if guides:
                        self.store.event(
                            self.run_id, "skills_retrieved", {"ids": [guide["id"] for guide in guides]}
                        )
                for row in result["data"].get("evidence", []):
                    observed[row["id"]] = row
                self.store.update(self.run_id, evidence=observed)
                if "services" in result["data"]:
                    self.store.update(self.run_id, services=result["data"]["services"])
            return result
        except (ValidationError, ToolFault) as error:
            code = error.code if isinstance(error, ToolFault) else "invalid_arguments"
            return {"ok": False, "error": {"code": code, "message": str(error), "retryable": False}}


class PriorTurnTool(Tool):
    name = "get_prior_turn"
    description = (
        "Read the source of a prior context entry in this incident. Historical evidence may be stale."
    )
    inputs = {"run_id": {"type": "string", "description": "A prior run ID from the context"}}
    output_type = "object"

    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id
        super().__init__()

    def forward(self, run_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute(
                """SELECT r.data,j.message FROM incident_jobs j JOIN runs r ON r.id=j.run_id
                JOIN incident_jobs current ON current.run_id=? AND current.kind='diagnose'
                WHERE j.kind='diagnose' AND j.run_id=? AND j.session_id=current.session_id AND j.seq<current.seq""",
                (self.run_id, run_id),
            ).fetchone()
        if not row:
            return {"ok": False, "error": {"message": "Prior turn is outside this incident"}}
        run = json.loads(row[0])
        keys = (run.get("diagnosis") or {}).get("evidence_ids", [])[:4]
        result = {
            "ok": True,
            "data": {
                "historical": True,
                "run_id": run_id,
                "message": row[1][:1500],
                "diagnosis": run.get("diagnosis"),
                "evidence": [run["evidence"][key] for key in keys],
            },
        }
        self.store.event(self.run_id, "prior_turn_retrieved", {"run_id": run_id, "evidence_ids": keys})
        return result


def diagnostic_tools(backend, store: Store, run_id: str, *, reliable: bool = True):
    return [
        ReadTool(name, backend, store, run_id, reliable=reliable)
        for name in ("list_services", "query_logs", "query_metrics", "search_runbooks")
    ]
