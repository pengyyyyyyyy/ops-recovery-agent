from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .domain import ToolFault
from .lab import read_json


class LabBackend:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.manifest = read_json(self.directory / "manifest.json")
        endpoint = urlsplit(self.manifest["url"])
        if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1":
            raise ValueError("The lab controller must be bound to 127.0.0.1")

    @property
    def spec(self) -> dict:
        return {"kind": "lab", "directory": str(self.directory)}

    def request(self, method: str, path: str, payload: dict | None = None, **kwargs) -> dict:
        try:
            with httpx.Client(timeout=kwargs.pop("timeout", 6), trust_env=False) as client:
                response = client.request(
                    method,
                    self.manifest["url"] + path,
                    json=payload,
                    headers={"Authorization": "Bearer " + self.manifest["token"]},
                    **kwargs,
                )
            if response.status_code >= 400:
                raise ToolFault(
                    "permission_denied" if response.status_code == 403 else "http_error",
                    f"Lab returned HTTP {response.status_code}",
                    retryable=response.status_code in {429, 502, 503, 504},
                    ambiguous=method != "GET" and response.status_code >= 500,
                )
            return response.json()
        except httpx.TimeoutException as error:
            raise ToolFault(
                "timeout", "Lab request timed out", retryable=True, ambiguous=method != "GET"
            ) from error
        except httpx.TransportError as error:
            raise ToolFault(
                "transport_error", "Lab connection failed", retryable=True, ambiguous=method != "GET"
            ) from error

    def scope(self):
        return self.request("GET", "/scope")

    def read(self, name: str, arguments: dict):
        return self.request("GET", "/read/" + name, params=arguments)

    def inject(self, fault: str):
        return self.request("POST", "/inject", {"fault": fault}, timeout=15)

    def probe(self):
        return self.request("POST", "/probe", {}, timeout=5)

    def repair(self, operation_id: str, action: str, scope: dict, *, drop_response: bool = False):
        return self.request(
            "POST",
            "/repair",
            {"operation_id": operation_id, "action": action, "scope": scope, "drop_response": drop_response},
            timeout=12,
        )

    def operation(self, operation_id: str):
        return self.request("GET", "/operations/" + operation_id)["operation"]

    def close(self):
        self.request("POST", "/shutdown", {})


class DatasetBackend:
    """Read only anonymous exported evidence. Never opens the answers or source-case index."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.task = json.loads((self.directory / "task.json").read_text(encoding="utf-8"))
        self.evidence = json.loads((self.directory / "evidence.json").read_text(encoding="utf-8"))

    @property
    def spec(self):
        return {"kind": "dataset", "directory": str(self.directory)}

    def scope(self):
        return {"dataset_case": self.task["id"], "generation": self.task["telemetry_digest"]}

    def read(self, name: str, arguments: dict):
        service = arguments.get("service", "all")
        if service != "all" and service not in self.task["services"]:
            raise ToolFault("invalid_arguments", "Unknown service in this dataset case")
        if name == "list_services":
            return {"scope": self.scope(), "services": self.task["services"], "evidence": []}
        kinds = {"query_logs": "log", "query_metrics": "metric", "search_runbooks": "documentation"}
        if name not in kinds:
            raise ToolFault("permission_denied", "Dataset adapter exposes only bounded read tools")
        rows = [
            row
            for row in self.evidence
            if row["kind"] == kinds[name] and (service == "all" or row.get("service") == service)
        ]
        return {"evidence": rows[:40]}


class FaultyBackend:
    """Controlled tool transport fault, independent from the application's root cause."""

    def __init__(self, backend, *, failures: int = 1):
        self.backend, self.remaining = backend, failures

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def read(self, name: str, arguments: dict):
        if name == "query_logs" and self.remaining:
            self.remaining -= 1
            raise ToolFault("timeout", "Injected first-read transport timeout", retryable=True)
        return self.backend.read(name, arguments)


def backend_from_spec(spec: dict):
    if spec["kind"] == "lab":
        return LabBackend(Path(spec["directory"]))
    if spec["kind"] == "dataset":
        return DatasetBackend(Path(spec["directory"]))
    if spec["kind"] == "umami":
        from .umami import UmamiBackend

        return UmamiBackend(Path(spec["socket"]))
    raise ValueError("Unknown backend kind")
