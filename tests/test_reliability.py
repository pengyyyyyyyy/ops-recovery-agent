import time

import pytest

from ops_recovery.domain import BudgetExceeded, BusyRun, ToolFault
from ops_recovery.execution import ReliableExecutor
from ops_recovery.storage import Store


@pytest.fixture
def record(tmp_path):
    store = Store(tmp_path)
    return store, store.create({"kind": "test"}, "offline")


def test_transient_reads_retry_with_one_trace_per_attempt(record):
    store, run_id = record
    calls = []

    def read():
        calls.append(1)
        if len(calls) < 3:
            raise ToolFault("timeout", "temporary timeout", retryable=True)
        return 42

    result = ReliableExecutor(store, run_id, sleep=lambda _: None).call("read", {}, read)
    assert result == {"ok": True, "data": 42, "attempts": 3}
    assert [event["payload"]["ok"] for event in store.events(run_id) if event["kind"] == "tool"] == [
        False,
        False,
        True,
    ]


def test_ambiguous_writes_are_not_automatically_retried(record):
    store, run_id = record
    calls = []

    def write():
        calls.append(1)
        raise ToolFault("timeout", "response missing", retryable=True, ambiguous=True)

    result = ReliableExecutor(store, run_id).call("write", {}, write, read_only=False)
    assert len(calls) == 1
    assert result["error"]["ambiguous"]


def test_permissions_do_not_retry(record):
    store, run_id = record

    def refused():
        raise ToolFault("permission_denied", "no access")

    result = ReliableExecutor(store, run_id).call("read", {}, refused)
    assert result["attempts"] == 1


def test_budget_includes_unknown_requests_and_is_shared_across_runs(record):
    store, run_id = record
    request = store.reserve(run_id, 0.7, 1)
    other = store.create({"kind": "test"}, "offline")
    with pytest.raises(BudgetExceeded):
        store.reserve(other, 0.4, 1)
    store.settle(request, 0.2, 10, 10)
    store.reserve(other, 0.7, 1)
    assert store.cost()["reserved"]["cny"] == pytest.approx(0.7)


def test_lease_excludes_concurrent_workers_and_recovers_after_expiry(record):
    store, run_id = record
    with store.lease(run_id):
        with pytest.raises(BusyRun):
            with store.lease(run_id):
                pass
    with store.connect() as database:
        database.execute(
            "UPDATE runs SET lease_until=?,lease_owner='crashed' WHERE id=?", (time.time() - 1, run_id)
        )
    with store.lease(run_id):
        assert store.get(run_id)["id"] == run_id
