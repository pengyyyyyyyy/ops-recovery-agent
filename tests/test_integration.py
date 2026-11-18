import json
import sqlite3
import subprocess
import sys
import time

import pytest

from ops_recovery.backends import LabBackend
from ops_recovery.config import Settings
from ops_recovery.coordinator import Coordinator
from ops_recovery.domain import Phase, ToolFault
from ops_recovery.lab import FAULTS, child_environment, start_lab
from ops_recovery.mcp_transport import MCPBackend
from ops_recovery.storage import Store
from ops_recovery.tools import ReadTool


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    directory = tmp_path_factory.mktemp("lab")
    start_lab(directory)
    backend = LabBackend(directory)
    yield backend
    backend.close()
    time.sleep(0.3)


@pytest.mark.integration
@pytest.mark.parametrize("fault", list(FAULTS))
def test_real_fault_diagnosis_approval_repair_and_http_verification(lab, tmp_path, fault):
    lab.inject(fault)
    settings = Settings(state_dir=tmp_path)
    store = Store(tmp_path)
    coordinator = Coordinator(settings, store)
    run_id = coordinator.diagnose(lab)
    run = store.get(run_id)
    assert run["diagnosis"]["service"] == FAULTS[fault]["service"]
    assert run["phase"] == Phase.WAITING_APPROVAL
    with pytest.raises(PermissionError):
        coordinator.resume(run_id, lab)
    with pytest.raises(ValueError):
        coordinator.approve(run_id, "wrong digest")
    coordinator.approve(run_id, run["proposal_digest"])
    # Simulate CLI process restart: reopen the database and construct a new coordinator.
    recovered = Coordinator(settings, Store(tmp_path))
    assert recovered.resume(run_id, lab)["phase"] == Phase.RESOLVED
    assert all(probe["status"] == 200 for probe in lab.probe()["probes"])


@pytest.mark.integration
def test_write_commit_with_lost_response_reconciles_without_duplicate(lab, tmp_path):
    lab.inject("api_crash")
    settings, store = Settings(state_dir=tmp_path), Store(tmp_path)
    coordinator = Coordinator(settings, store)
    run_id = coordinator.diagnose(lab)
    coordinator.approve(run_id, store.get(run_id)["proposal_digest"])
    assert coordinator.resume(run_id, lab, drop_response=True)["phase"] == Phase.UNKNOWN
    assert coordinator.resume(run_id, lab)["phase"] == Phase.RESOLVED
    with sqlite3.connect(lab.directory / "operations.sqlite3") as database:
        assert (
            database.execute("SELECT COUNT(*) FROM operations WHERE id=?", ("repair-" + run_id,)).fetchone()[
                0
            ]
            == 1
        )
    completed = [
        row
        for row in lab.read("query_logs", {"service": "all"})["evidence"]
        if row.get("operation_id") == "repair-" + run_id
    ]
    assert len(completed) == 1


@pytest.mark.integration
def test_approval_cannot_cross_an_incident_boundary(lab, tmp_path):
    lab.inject("api_crash")
    store = Store(tmp_path)
    coordinator = Coordinator(Settings(state_dir=tmp_path), store)
    run_id = coordinator.diagnose(lab)
    coordinator.approve(run_id, store.get(run_id)["proposal_digest"])
    lab.inject("database_lock")
    with pytest.raises(PermissionError, match="scope changed"):
        coordinator.resume(run_id, lab)


@pytest.mark.integration
def test_real_mcp_schema_and_round_trip(lab, tmp_path):
    lab.inject("api_crash")
    store = Store(tmp_path)
    run_id = store.create(lab.spec, "offline")
    with MCPBackend(lab) as remote:
        assert "gateway" in remote.read("list_services", {})["services"]
        with pytest.raises(ToolFault):
            remote.read("query_logs", {"service": "not-a-service"})
        tool = ReadTool("query_logs", remote, store, run_id)
        assert tool(service="all")["ok"]


@pytest.mark.integration
def test_changed_diagnosis_invalidates_existing_approval(lab, tmp_path):
    lab.inject("api_crash")
    store = Store(tmp_path)
    coordinator = Coordinator(Settings(state_dir=tmp_path), store)
    run_id = coordinator.diagnose(lab)
    run = store.get(run_id)
    coordinator.approve(run_id, run["proposal_digest"])
    diagnosis = run["diagnosis"]
    diagnosis["action"] = "release_database_lock"
    store.update(run_id, diagnosis=diagnosis)
    with pytest.raises(PermissionError, match="changed"):
        coordinator.resume(run_id, lab)


@pytest.mark.integration
def test_unknown_write_checkpoint_resumes_in_a_new_cli_process(lab, tmp_path):
    lab.inject("api_crash")
    store = Store(tmp_path)
    coordinator = Coordinator(Settings(state_dir=tmp_path), store)
    run_id = coordinator.diagnose(lab)
    coordinator.approve(run_id, store.get(run_id)["proposal_digest"])
    assert coordinator.resume(run_id, lab, drop_response=True)["phase"] == Phase.UNKNOWN
    environment = child_environment()
    environment.update(OPS_STATE_DIR=str(tmp_path), OPS_MODEL_MODE="offline", PYTHONUTF8="1")
    process = subprocess.run(
        [sys.executable, "-m", "ops_recovery.cli", "resume", run_id],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["phase"] == Phase.RESOLVED


@pytest.mark.integration
def test_controller_denies_requests_without_local_authorization(lab):
    import httpx

    response = httpx.post(lab.manifest["url"] + "/repair", json={}, trust_env=False)
    assert response.status_code == 403


@pytest.mark.integration
@pytest.mark.parametrize("defect", ["too_many", "invented_id"])
def test_invalid_model_output_is_corrected_inside_the_agent_loop(lab, tmp_path, monkeypatch, defect):
    from ops_recovery import coordinator as module
    from ops_recovery.models import OfflineModel, heuristic, tool_message

    class InvalidOnceModel(OfflineModel):
        bad_answer_sent = False

        def generate(self, messages, **kwargs):
            if self.role == "diagnostician" and self.step >= 4 and not self.bad_answer_sent:
                self.bad_answer_sent = True
                answer = heuristic(list(self.store.get(self.run_id)["evidence"].values()))
                answer["evidence_ids"] = (
                    [answer["evidence_ids"][0]] * 13 if defect == "too_many" else ["invented"]
                )
                return tool_message("final_answer", {"answer": answer})
            return super().generate(messages, **kwargs)

    monkeypatch.setattr(
        module, "make_model", lambda settings, store, run_id, role: InvalidOnceModel(store, run_id, role)
    )
    lab.inject("api_crash")
    store = Store(tmp_path)
    run_id = Coordinator(Settings(state_dir=tmp_path), store).diagnose(lab)
    assert store.get(run_id)["phase"] == Phase.WAITING_APPROVAL
    assert any(event["kind"] == "output_rejected" for event in store.events(run_id))


@pytest.mark.integration
def test_exhausted_loop_with_free_text_fallback_fails_without_repair(lab, tmp_path, monkeypatch):
    from smolagents.models import ChatMessage

    from ops_recovery import coordinator as module
    from ops_recovery.models import OfflineModel

    class ProseOnlyModel(OfflineModel):
        def generate(self, messages, **kwargs):
            if self.step < 4:
                return super().generate(messages, **kwargs)
            return ChatMessage.from_dict(
                {"role": "assistant", "content": "The gateway process is down. Restart it."}
            )

    monkeypatch.setattr(
        module, "make_model", lambda settings, store, run_id, role: ProseOnlyModel(store, run_id, role)
    )
    lab.inject("api_crash")
    store = Store(tmp_path)
    run_id = store.create(lab.spec, "offline")
    with pytest.raises(json.JSONDecodeError):
        Coordinator(Settings(state_dir=tmp_path), store).diagnose(lab, run_id=run_id, dual=False)
    assert store.get(run_id)["phase"] == Phase.FAILED
    assert lab.operation("repair-" + run_id) is None
