from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ops_recovery.config import Settings
from ops_recovery.domain import Phase
from ops_recovery.sessions import Daemon, Sessions
from ops_recovery.skills import Skills
from ops_recovery.storage import Store, canonical, digest
from ops_recovery.tools import PriorTurnTool


class Backend:
    spec = {"kind": "lab", "directory": "test"}

    def __init__(self):
        self.repairs = 0
        self.result = None

    def scope(self):
        return {"incident": "example"}

    def read(self, name, arguments):
        if name == "list_services":
            return {"services": ["gateway"], "evidence": []}
        return {
            "evidence": [{"id": "crash", "service": "gateway", "kind": "log", "signal": "process_exited"}]
        }

    def operation(self, operation_id):
        return self.result

    def repair(self, operation_id, action, scope, *, drop_response=False):
        self.repairs += 1
        self.result = {"completed": True, "operation_id": operation_id}
        return self.result

    def probe(self):
        return {"probes": [{"status": 200}] * 3}


def prepare(tmp_path, count=1):
    settings = Settings(state_dir=tmp_path)
    backend = Backend()
    daemon = Daemon(settings, factory=lambda _: backend)
    sessions = daemon.sessions
    ids = [
        sessions.create(backend.spec, "offline", f"incident {i}", ["Preserve the telemetry"])
        for i in range(count)
    ]
    return daemon, sessions, backend, ids


def test_durable_followup_context_and_history_boundaries(tmp_path):
    daemon, sessions, _, (first, second) = prepare(tmp_path, 2)
    daemon.run(drain=True)
    source = sessions.history(first)["turns"][0]["run_id"]
    for i in range(6):
        assert sessions.tell(first, f"Read the gateway details {i}", str(i)) == sessions.tell(
            first, f"Read the gateway details {i}", str(i)
        )
    with pytest.raises(ValueError):
        sessions.tell(first, "Different payload", "0")
    daemon.run(drain=True)
    turns = sessions.history(first)["turns"]
    context = json.loads(sessions.context(turns[-1]))
    assert context["operator_constraints"] == ["Preserve the telemetry"]
    assert len(turns) == 7 and len(context["recent_turns"]) == 2 and context["archived_turns"] == 4
    tool = PriorTurnTool(daemon.store, turns[-1]["run_id"])
    assert tool.forward(source)["data"]["evidence"][0]["id"] == "crash"
    assert not tool.forward(sessions.history(second)["turns"][0]["run_id"])["ok"]


def test_restart_invalidates_pending_approval_and_never_replays_unknown_write(tmp_path):
    daemon, sessions, backend, (session,) = prepare(tmp_path)
    daemon.run(drain=True)
    run_id = sessions.history(session)["turns"][0]["run_id"]
    run = daemon.store.get(run_id)
    sessions.recover("old-daemon")
    sessions.request_repair(session, run_id, run["proposal_digest"])
    assert sessions.claim("old-daemon")["kind"] == "repair"
    sessions.recover("new-daemon")
    assert daemon.store.get(run_id)["phase"] == Phase.WAITING_APPROVAL
    assert sessions.claim("new-daemon") is None
    sessions.request_repair(session, run_id, run["proposal_digest"])
    operation_id = "repair-" + run_id
    daemon.store.update(run_id, phase=Phase.EXECUTING, operation_id=operation_id)
    daemon.run(drain=True)
    assert backend.repairs == 0
    assert daemon.store.get(run_id)["phase"] == Phase.UNKNOWN
    backend.result = {"completed": True, "operation_id": operation_id}
    daemon.run(drain=True)
    assert daemon.store.get(run_id)["phase"] == Phase.RESOLVED
    assert backend.repairs == 0
    assert any(event["kind"] == "approval_invalidated" for event in daemon.store.events(run_id))


def test_worker_process_exit_releases_daemon_lock_and_recovers_read_job(tmp_path):
    from ops_recovery import sessions as module
    from ops_recovery.lab import child_environment

    daemon, sessions, _, (session,) = prepare(tmp_path)
    run_id = sessions.history(session)["turns"][0]["run_id"]
    child = """
import os, sys
from pathlib import Path
from ops_recovery.locks import try_lock
from ops_recovery.sessions import Sessions
from ops_recovery.storage import Store
store = Store(Path(sys.argv[1]))
with try_lock(store.path.with_name('incident-daemon.lock')) as acquired:
    assert acquired
    runtime = Sessions(store)
    runtime.recover('crashed-daemon')
    job = runtime.claim('crashed-daemon')
    with store.lease(job['run_id']):
        os._exit(27)
"""
    env = child_environment()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(module.__file__).resolve().parents[1]), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run([sys.executable, "-c", child, str(tmp_path)], env=env, timeout=12, check=False)
    assert result.returncode == 27
    turn = sessions.history(session)["turns"][0]
    assert turn["status"] == "running"
    daemon.run(drain=True)
    recovered = sessions.history(session)["turns"][0]
    assert (recovered["run_id"], recovered["status"], recovered["run"]["phase"]) == (
        run_id,
        "done",
        Phase.WAITING_APPROVAL,
    )


def test_probe_hysteresis_deduplicates_across_restarts(tmp_path):
    daemon, sessions, backend, _ = prepare(tmp_path, 0)

    def sample(number, healthy=False):
        return sessions.watch(backend.spec, "offline", key="pilot", sample=str(number), healthy=healthy)

    assert not sample(1)["event"]
    assert sample(1)["duplicate"]
    assert not sample(2)["event"]
    first = sample(3)
    assert first["event"] == "alert_opened"
    sessions = Sessions(Store(tmp_path))
    assert sample(4)["active_session"] == first["active_session"]
    assert not sample(5, True)["event"]
    assert sample(6, True)["event"] == "alert_closed"
    assert len(sessions.list()) == 1
    daemon.run(drain=True)


def test_skill_requires_counterexample_replay_and_exact_version_approval(tmp_path):
    daemon, sessions, backend, (session,) = prepare(tmp_path)
    daemon.run(drain=True)
    run = sessions.history(session)["turns"][0]
    sessions.recover("test-daemon")
    daemon.epoch = "test-daemon"
    sessions.request_repair(session, run["run_id"], run["run"]["proposal_digest"])
    daemon.process(sessions.claim(daemon.epoch))
    assert backend.repairs == 1
    skills = Skills(daemon.store)
    skill = skills.list()[0]
    assert not skills.match("lab", [{"id": "ev", "service": "gateway", "signal": "process_exited"}])
    cases = [
        {
            "id": "positive",
            "group": "alternate process fixture",
            "backend": "lab",
            "observations": {"gateway": ["process_exited"]},
            "expected_match": True,
        },
        {
            "id": "similar",
            "group": "dependency incident",
            "backend": "lab",
            "observations": {"gateway": ["upstream_timeout"]},
            "expected_match": False,
        },
    ]
    replay = skills.replay(skill["id"], cases)
    assert replay["passed"]
    with pytest.raises(ValueError):
        skills.enable(skill["id"], "changed", replay["replay_hash"])
    skills.enable(skill["id"], skill["hash"], replay["replay_hash"])
    assert skills.match("lab", [{"id": "ev", "service": "gateway", "signal": "process_exited"}])
    skills.disable(skill["id"])
    assert not skills.match("lab", [{"id": "ev", "service": "gateway", "signal": "process_exited"}])


def test_ten_sessions_run_through_http_gate_with_native_tool_pairs(tmp_path):
    lock = threading.Lock()
    first_wave = threading.Barrier(3)
    stats = {"inflight": 0, "max": 0, "protocol_errors": 0, "requests": 0}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = payload["messages"]
            with lock:
                stats["inflight"] += 1
                stats["requests"] += 1
                request_number = stats["requests"]
                stats["max"] = max(stats["inflight"], stats["max"])
                pairs = [
                    (m["tool_calls"][0]["id"], messages[i + 1].get("tool_call_id"))
                    for i, m in enumerate(messages)
                    if m.get("tool_calls")
                ]
                stats["protocol_errors"] += sum(a != b for a, b in pairs)
            # Hold the first three requests together so scheduler timing cannot
            # accidentally make a correct three-slot gate look serial.
            if request_number <= 3:
                first_wave.wait(timeout=15)
            time.sleep(0.06)
            tools = [
                ("list_services", {}),
                ("query_logs", {"service": "all"}),
                (
                    "final_answer",
                    {
                        "answer": {
                            "service": "gateway",
                            "root_cause": "process_crash",
                            "summary": "Observed process exit",
                            "evidence_ids": ["crash"],
                            "action": "none",
                        }
                    },
                ),
            ]
            name, args = tools[len(pairs)]
            content = canonical(
                {
                    "model": "mock",
                    "usage": {"prompt_tokens": 50, "completion_tokens": 25},
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-" + str(len(pairs)),
                                        "type": "function",
                                        "function": {"name": name, "arguments": canonical(args)},
                                    }
                                ],
                            }
                        }
                    ],
                }
            ).encode()
            with lock:
                stats["inflight"] -= 1
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    runner = threading.Thread(target=http.serve_forever, daemon=True)
    runner.start()
    try:
        daemon = Daemon(
            Settings(
                mode="online",
                base_url=f"http://127.0.0.1:{http.server_port}",
                model_id="mock",
                input_price=2,
                output_price=8,
                api_key="fake-test-key",
                state_dir=tmp_path,
            ),
            dual=False,
            factory=lambda _: Backend(),
        )
        ids = [daemon.sessions.create(Backend.spec, "online", str(i)) for i in range(10)]
        daemon.run(drain=True)
        runs = [daemon.sessions.history(s)["turns"][0] for s in ids]
        assert all(run["status"] == "done" and run["run"]["phase"] == Phase.DIAGNOSED for run in runs)
        assert stats == {"inflight": 0, "max": 3, "protocol_errors": 0, "requests": 30}
        assert daemon.store.cost()["settled"]["requests"] == 30
    finally:
        http.shutdown()
        http.server_close()
        runner.join(2)


def test_broker_rejects_modified_scope_and_reuses_completed_operation(tmp_path, monkeypatch):
    from subprocess import CompletedProcess

    from ops_recovery.pilot_broker import Pilot

    daemon, sessions, _, _ = prepare(tmp_path, 0)
    backend = {"kind": "umami", "socket": "/run/ops-pilot/broker.sock"}
    session_id = sessions.create(backend, "offline", "restart only when approved")
    run_id = sessions.history(session_id)["turns"][0]["run_id"]
    broker = Pilot(tmp_path, daemon.store.path)
    scope = {"target": "umami", "version": "old"}
    monkeypatch.setattr(broker, "scope", lambda: scope)
    commands = []

    def compose(*args, **kwargs):
        commands.append(args)
        return CompletedProcess(args, 0)

    monkeypatch.setattr(broker, "compose", compose)
    operation = {"operation_id": "repair-" + run_id, "action": "restart_umami", "scope": scope}
    with pytest.raises(PermissionError):
        broker.repair(operation)
    proposal = {"run_id": run_id, "scope": scope, "action": "restart_umami", "service": "umami"}
    daemon.store.update(
        run_id, phase=Phase.WAITING_APPROVAL, proposal=proposal, proposal_digest=digest(proposal)
    )
    sessions.recover("test-daemon")
    sessions.request_repair(session_id, run_id, digest(proposal))
    daemon.store.update(run_id, phase=Phase.EXECUTING, operation_id=operation["operation_id"])
    with pytest.raises(PermissionError):
        broker.repair({**operation, "scope": {"target": "other"}})
    assert broker.repair(operation) == broker.repair(operation)
    assert commands == [("restart", "--timeout", "10", "umami")]
