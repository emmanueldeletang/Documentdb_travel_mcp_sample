"""
Synchronous wrapper around the DocumentDB MCP server.

The MCP Python SDK is asyncio-based and a stdio session is tied to a live
subprocess. Flask is synchronous/threaded, so we run a dedicated asyncio event
loop in a background thread that owns one long-lived ClientSession, and proxy
calls into it with run_coroutine_threadsafe.
"""
from __future__ import annotations

import asyncio
import os
import threading
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPClient:
    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None):
        self._command = command
        self._args = args
        # Merge with the current environment so PATH/npx and creds resolve.
        self._env = {**os.environ, **(env or {})}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._call_lock = threading.Lock()
        self._connected = False
        self.last_error: str | None = None

    # ---- event loop plumbing -------------------------------------------------
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ---- connection ----------------------------------------------------------
    async def _connect(self) -> None:
        params = StdioServerParameters(
            command=self._command, args=self._args, env=self._env
        )
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()

    def connect(self, timeout: float = 90.0) -> bool:
        """Idempotent. Returns True on success, stores message in last_error on failure."""
        if self._connected:
            return True
        try:
            self._submit(self._connect(), timeout=timeout)
            self._connected = True
            self.last_error = None
            return True
        except Exception as exc:  # surface a readable message to the UI
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    @property
    def connected(self) -> bool:
        return self._connected

    # ---- tools ---------------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        if not self.connect():
            raise RuntimeError(self.last_error or "MCP server not connected")
        result = self._submit(self._session.list_tools(), timeout=30)
        tools = []
        for t in result.tools:
            tools.append(
                {
                    "name": t.name,
                    "description": (t.description or "").strip(),
                    "input_schema": t.inputSchema or {"type": "object", "properties": {}},
                }
            )
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.connect():
            raise RuntimeError(self.last_error or "MCP server not connected")
        with self._call_lock:  # one in-flight tool call at a time (simple + safe)
            result = self._submit(
                self._session.call_tool(name, arguments or {}), timeout=120
            )
        # Flatten content blocks into a single text payload.
        parts: list[str] = []
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(str(block))
        return {
            "is_error": bool(getattr(result, "isError", False)),
            "text": "\n".join(parts).strip(),
        }
