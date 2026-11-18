"""MCP's only exposed surface is the bounded, read-only investigation interface."""

from __future__ import annotations

import argparse
import json
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .backends import backend_from_spec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-spec", required=True)
    arguments = parser.parse_args()
    spec = json.loads(arguments.backend_spec)
    spec.pop("transport", None)
    backend = backend_from_spec(spec)
    server = FastMCP("ops-recovery-read-tools")

    @server.tool()
    def list_services() -> dict:
        """List service names and observed process health."""
        return backend.read("list_services", {})

    @server.tool()
    def query_logs(service: Annotated[str, Field(min_length=1, max_length=120)]) -> dict:
        """Return bounded incident logs for a listed service, or all."""
        return backend.read("query_logs", {"service": service})

    @server.tool()
    def query_metrics(service: Annotated[str, Field(min_length=1, max_length=120)]) -> dict:
        """Return bounded metrics for a listed service, or all."""
        return backend.read("query_metrics", {"service": service})

    @server.tool()
    def search_runbooks(query: Annotated[str, Field(min_length=1, max_length=200)]) -> dict:
        """Return generic investigation guidance, never a ground-truth incident label."""
        return backend.read("search_runbooks", {"query": query})

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
