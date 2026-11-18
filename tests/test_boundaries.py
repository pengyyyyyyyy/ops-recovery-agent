import json

import httpx
import pytest

from ops_recovery.backends import DatasetBackend
from ops_recovery.config import Settings
from ops_recovery.coordinator import Coordinator
from ops_recovery.datasets import choose_cases
from ops_recovery.domain import BudgetExceeded
from ops_recovery.models import HTTPModel
from ops_recovery.report import render_run
from ops_recovery.storage import Store


def test_public_agent_adapter_never_reads_answers(tmp_path):
    directory = tmp_path / "blind" / "test-001"
    directory.mkdir(parents=True)
    (directory / "task.json").write_text(
        json.dumps({"id": "test-001", "telemetry_digest": "d", "services": ["service-a"]})
    )
    (directory / "evidence.json").write_text(
        json.dumps([{"id": "metric-1", "kind": "metric", "service": "service-a"}])
    )
    # Deliberately invalid JSON: initialization/reads must never parse a labels file.
    (directory / "answers.json").write_text("SECRET_LABEL_DO_NOT_READ")
    backend = DatasetBackend(directory)
    assert "SECRET_LABEL" not in json.dumps(backend.read("query_metrics", {"service": "all"}))
    with pytest.raises(Exception, match="bounded read tools"):
        backend.read("read_file", {"path": "answers.json"})


def test_group_split_excludes_repeated_cases_of_the_same_fault_service():
    rows = [
        {
            "dataset": "RE2-OB",
            "root_cause_service": service,
            "fault": fault,
            "repetition": repeat,
            "case": f"{service}-{fault}-{repeat}",
        }
        for service in ["checkoutservice", "other", "third", "fourth"]
        for fault in ["cpu", "mem", "disk"]
        for repeat in range(1, 4)
    ]
    selected = choose_cases(rows, 3, 6, 42)
    dev = {(r["root_cause_service"], r["fault"]) for split, r in selected if split == "dev"}
    test = {(r["root_cause_service"], r["fault"]) for split, r in selected if split == "test"}
    assert dev.isdisjoint(test)
    assert ("checkoutservice", "cpu") in dev


def test_html_report_escapes_untrusted_telemetry(tmp_path):
    store = Store(tmp_path)
    run = store.create({"kind": "test"}, "offline")
    store.event(run, "tool", {"result": "<script>alert('telemetry')</script>"})
    output = render_run(store, run, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "<script>" not in output
    assert "&lt;script&gt;" in output


def test_unobserved_evidence_is_rejected(tmp_path):
    store = Store(tmp_path)
    run = store.create({"kind": "test"}, "offline")
    with pytest.raises(ValueError, match="not actually observed"):
        Coordinator(Settings(), store).validate_evidence(run, ["invented-id"])


def test_budget_overrun_is_recorded_and_latches_future_requests(tmp_path):
    store = Store(tmp_path)
    run = store.create({"kind": "test"}, "online")
    request = store.reserve(run, 0.01, 100)
    with pytest.raises(BudgetExceeded):
        store.settle(request, 0.02, 20, 20)
    assert store.cost()["overrun"]["cny"] == pytest.approx(0.02)
    with pytest.raises(BudgetExceeded, match="blocked"):
        store.reserve(run, 0.01, 100)


def test_model_key_only_enters_authorization_not_trace(monkeypatch, tmp_path):
    secret = "unit-test-private-key-not-a-real-credential"
    store = Store(tmp_path)
    run = store.create({"kind": "test"}, "online")
    captured = {}

    def mock_post(self, url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(
            200,
            json={
                "model": "mock",
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
                "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            },
        )

    monkeypatch.setattr(httpx.Client, "post", mock_post)
    settings = Settings(
        mode="online",
        base_url="https://example.invalid",
        model_id="mock",
        api_key=secret,
        input_price=2,
        output_price=8,
    )
    model = HTTPModel(settings, store, run, "diagnostician")
    model.generate([{"role": "user", "content": "test synthetic incident"}])
    assert captured["headers"]["Authorization"] == "Bearer " + secret
    assert secret not in json.dumps(captured["json"])
    assert secret not in json.dumps(store.events(run))
    assert secret not in render_run(store, run, tmp_path / "report.html").read_text(encoding="utf-8")
