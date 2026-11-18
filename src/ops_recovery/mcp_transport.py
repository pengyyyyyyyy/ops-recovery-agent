from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .domain import ToolFault
from .lab import child_environment
from .storage import canonical


class MCPBackend:
    def __init__(self, backend):
        self.backend, self.ready = backend, threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()
        if not self.ready.wait(15):
            raise TimeoutError("MCP initialization timed out")
        if self.error:
            raise RuntimeError("MCP initialization failed") from self.error

    @property
    def spec(self):
        return {**self.backend.spec, "transport": "mcp"}

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def _thread(self):
        try:
            asyncio.run(self._serve())
        except BaseException as error:
            self.error = error
            self.ready.set()

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ops_recovery.mcp_server", "--backend-spec", canonical(self.backend.spec)],
            env=child_environment(),
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                self.session = session
                await session.initialize()
                self.ready.set()
                await self.stop_event.wait()

    async def _read(self, name, arguments):
        response = await self.session.call_tool(name, arguments)
        if response.isError:
            raise ToolFault("mcp_error", "MCP rejected the tool call")
        structured = getattr(response, "structuredContent", None)
        if structured is not None:
            return structured
        return json.loads(response.content[0].text)

    def read(self, name: str, arguments: dict):
        future = asyncio.run_coroutine_threadsafe(self._read(name, arguments), self.loop)
        try:
            return future.result(timeout=10)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            raise ToolFault("mcp_timeout", "MCP read timed out", retryable=True) from error

    def transport_close(self):
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.stop_event.set)
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError("MCP subprocess failed to shut down")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.transport_close()
