from __future__ import annotations

from pathlib import Path

import httpx

from .domain import ToolFault

PILOT_ACTIONS = {"restart_umami": "umami", "rollback_umami_config": "umami"}


class UmamiBackend:
    allowed_actions = PILOT_ACTIONS

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path.absolute()

    @property
    def spec(self):
        return {"kind": "umami", "socket": str(self.socket_path)}

    def request(self, method, route, *, payload=None, params=None, timeout=15):
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(self.socket_path)), timeout=timeout
            ) as client:
                response = client.request(method, "http://pilot" + route, json=payload, params=params)
            if not response.is_success:
                raise ToolFault(
                    "pilot_rejected",
                    f"Pilot broker returned HTTP {response.status_code}",
                    retryable=response.status_code >= 500,
                    ambiguous=method == "POST" and response.status_code >= 500,
                )
            return response.json()
        except httpx.TransportError as error:
            raise ToolFault(
                "pilot_transport",
                "Pilot broker is unavailable or timed out",
                retryable=True,
                ambiguous=method == "POST",
            ) from error

    def scope(self):
        return self.request("GET", "/scope")

    def read(self, name, arguments):
        return self.request("GET", "/read/" + name, params=arguments)

    def health(self):
        return self.request("GET", "/health")

    def operation(self, operation_id):
        return self.request("GET", "/operations/" + operation_id)["operation"]

    def repair(self, operation_id, action, scope, *, drop_response=False):
        result = self.request(
            "POST",
            "/repair",
            payload={"operation_id": operation_id, "action": action, "scope": scope},
            timeout=130,
        )
        if drop_response:
            raise ToolFault(
                "timeout", "Test: response dropped after the broker completed the operation", ambiguous=True
            )
        return result

    def probe(self):
        return self.request("POST", "/probe", payload={}, timeout=100)
