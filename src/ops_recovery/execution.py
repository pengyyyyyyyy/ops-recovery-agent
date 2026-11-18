from __future__ import annotations

import time
from collections.abc import Callable

from .domain import ToolFault
from .storage import Store


class ReliableExecutor:
    """Only read operations are retried automatically; unknown writes are reconciled by their owner."""

    def __init__(self, store: Store, run_id: str, *, attempts: int = 3, sleep: Callable = time.sleep):
        self.store, self.run_id = store, run_id
        self.attempts, self.sleep = attempts, sleep

    def call(self, name: str, arguments: dict, function: Callable, *, read_only: bool = True) -> dict:
        limit = self.attempts if read_only else 1
        for attempt in range(1, limit + 1):
            start = time.monotonic()
            try:
                result = function()
                self.store.event(
                    self.run_id,
                    "tool",
                    {
                        "name": name,
                        "arguments": arguments,
                        "attempt": attempt,
                        "ok": True,
                        "duration_ms": round((time.monotonic() - start) * 1000, 2),
                        "read_only": read_only,
                        "result": result,
                    },
                )
                return {"ok": True, "data": result, "attempts": attempt}
            except ToolFault as error:
                failure = {
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                    "ambiguous": error.ambiguous,
                }
                self.store.event(
                    self.run_id,
                    "tool",
                    {
                        "name": name,
                        "arguments": arguments,
                        "attempt": attempt,
                        "ok": False,
                        "duration_ms": round((time.monotonic() - start) * 1000, 2),
                        "error": failure,
                        "read_only": read_only,
                    },
                )
                if not read_only or not error.retryable or attempt == limit:
                    return {"ok": False, "error": failure, "attempts": attempt}
                self.sleep(min(0.1 * 2 ** (attempt - 1), 1))
        raise AssertionError("Unreachable")
