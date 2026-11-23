"""Exercise kernel lock release and repair reconciliation in an isolated real lab."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ops_recovery.backends import LabBackend
from ops_recovery.config import Settings
from ops_recovery.domain import Phase, ToolFault
from ops_recovery.lab import start_lab
from ops_recovery.sessions import Daemon
from ops_recovery.storage import canonical

CHILD = """
import os, sys
from pathlib import Path
from ops_recovery.backends import backend_from_spec
from ops_recovery.locks import try_lock
from ops_recovery.sessions import Sessions
from ops_recovery.storage import Store
store=Store(Path(sys.argv[1]))
with try_lock(store.path.with_name('incident-daemon.lock')) as lock:
    assert lock
    sessions=Sessions(store)
    sessions.recover('killed-worker')
    job=sessions.claim('killed-worker')
    with store.lease(job['run_id']):
        backend=backend_from_spec(store.get(job['run_id'])['backend'])
        backend.read('list_services',{})
        store.event(job['run_id'],'fixture_read_completed',{})
        os._exit(27)
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parent = parser.parse_args().directory.resolve()
    path = parent / ("recovery-fixture-" + uuid.uuid4().hex)
    lab_path = path / "lab"
    tick = time.monotonic()
    start_lab(lab_path)
    lab = LabBackend(lab_path)
    try:
        lab.inject("api_crash")
        daemon = Daemon(Settings(state_dir=path))
        sessions, store = daemon.sessions, daemon.store
        sid = sessions.create(lab.spec, "offline", "Isolated crash recovery fixture")
        run_id = sessions.history(sid)["turns"][0]["run_id"]
        operation = "repair-" + run_id
        child = subprocess.run([sys.executable, "-c", CHILD, str(path)], check=False, timeout=40)
        assert child.returncode == 27
        assert sessions.history(sid)["turns"][0]["status"] == "running"
        daemon.run(drain=True)
        run = store.get(run_id)
        assert run["phase"] == Phase.WAITING_APPROVAL
        assert any(event["kind"] == "fixture_read_completed" for event in store.events(run_id))
        print("RUNNING_READ_RESUMED", flush=True)

        def stage_repair(epoch):
            sessions.recover(epoch)
            current = store.get(run_id)
            sessions.request_repair(sid, run_id, current["proposal_digest"])
            approval = {**store.get(run_id)["approval"], "actor": "automated-test-policy"}
            store.update(run_id, approval=approval)
            job = sessions.claim(epoch)
            assert job and job["kind"] == "repair"

        stage_repair("crashed-before-write")
        daemon.run(drain=True)
        assert store.get(run_id)["phase"] == Phase.WAITING_APPROVAL
        assert lab.operation(operation) is None
        assert any(event["kind"] == "approval_invalidated" for event in store.events(run_id))
        print("UNEXECUTED_APPROVAL_INVALIDATED", flush=True)

        stage_repair("crashed-after-intent")
        current = store.update(run_id, phase=Phase.EXECUTING, operation_id=operation)
        daemon.run(drain=True)
        assert store.get(run_id)["phase"] == Phase.UNKNOWN
        assert lab.operation(operation) is None
        print("UNKNOWN_WITHOUT_RECORD_PAUSED", flush=True)

        try:
            lab.repair(operation, "restart_api", current["scope"], drop_response=True)
        except ToolFault as lost:
            assert lost.ambiguous
        else:
            raise AssertionError("Fixture did not lose the post-commit response")
        assert lab.operation(operation)["completed"]
        daemon.run(drain=True)
        recovered = store.get(run_id)
        assert recovered["phase"] == Phase.RESOLVED
        assert len(recovered["verification"]) == 3
        completed = [
            row
            for row in lab.read("query_logs", {"service": "all"})["evidence"]
            if row.get("operation_id") == operation
        ]
        assert len(completed) == 1
        print(
            "WORKER_RECOVERY_MATRIX="
            + canonical(
                {
                    "running_read_resumed": True,
                    "stale_approval_invalidated": True,
                    "unknown_without_record_paused": True,
                    "lost_response_reconciled": True,
                    "verification_probes": 3,
                    "duplicate_repairs": 0,
                    "elapsed_s": round(time.monotonic() - tick, 3),
                }
            )
        )
    finally:
        lab.close()


if __name__ == "__main__":
    main()
