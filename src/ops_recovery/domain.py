from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Phase(StrEnum):
    DIAGNOSING = "diagnosing"
    REVIEWING = "reviewing"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    UNKNOWN = "unknown"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    DIAGNOSED = "diagnosed"
    REJECTED = "rejected"
    FAILED = "failed"


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service: str
    root_cause: str
    summary: str
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    action: str = "none"


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool
    reason: str
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


ACTIONS = {
    "restart_api": "gateway",
    "reset_dependency_delay": "inventory",
    "release_database_lock": "database",
}


class ToolFault(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, ambiguous: bool = False):
        super().__init__(message)
        self.code, self.retryable, self.ambiguous = code, retryable, ambiguous


class BusyRun(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass
