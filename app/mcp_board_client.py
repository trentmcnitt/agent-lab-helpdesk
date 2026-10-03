"""BoardAdapter's interface, but every call crosses MCP (stdio) to
mcp_board/server.py. The graph's nodes are sync and the MCP client is async,
so one background event loop holds a single session open for the process;
sync calls are submitted to it.

If the board process dies mid-run, the next call starts a fresh one and
retries once. That's safe for writes because the agent's create_ticket
carries an idempotency key: a create that landed before the crash is found,
not repeated."""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

from mcp import Client, StdioServerParameters

from . import config


class MCPBoardClient:
    transport = "mcp:stdio"

    def __init__(self, db_path: Path | None = None):
        self._params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_board.server"],
            cwd=str(config.REPO_ROOT),
            # Only what the board needs -- no API keys, Slack tokens or Langfuse secrets.
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "BOARD_DB_PATH": str(db_path or config.BOARD_DB_PATH),
                "HELPDESK_NO_DOTENV": "1",
            },
        )
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name="mcp-board").start()
        self._lock = threading.Lock()
        self._connect()
        self.tool_names = [t.name for t in self._run(self._client.list_tools()).tools]

    def _connect(self) -> None:
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._client: Client | None = None
        asyncio.run_coroutine_threadsafe(self._hold_session(), self._loop)
        if not self._ready.wait(30):
            raise RuntimeError("MCP board server did not start within 30s")
        if self._error:
            raise RuntimeError(f"MCP board server failed to start: {self._error}") from self._error

    async def _hold_session(self) -> None:
        # The session must be entered and exited in the same task, so it lives
        # here for the life of the connection; calls come in from other tasks.
        self._stop = asyncio.Event()
        try:
            async with Client(self._params) as client:
                self._client = client
                self._ready.set()
                await self._stop.wait()
        except BaseException as e:  # startup failures surface to _connect; later ones to the next call
            self._error = e
            self._ready.set()

    def _run(self, coro, timeout: float = 30):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _call(self, tool: str, **args) -> dict:
        with self._lock:
            try:
                result = self._run(self._client.call_tool(tool, args))
            except Exception:
                # Most likely the board process died (the session reports "Connection closed").
                # Start a fresh one and retry once; the idempotency key makes a retried create safe.
                self._loop.call_soon_threadsafe(self._stop.set)
                self._connect()
                result = self._run(self._client.call_tool(tool, args))
        if result.is_error:
            raise RuntimeError(f"MCP tool {tool} failed: {result.content}")
        return result.structured_content

    def create_ticket(self, title: str, description: str, created_by: str, labels: list[str] | None = None,
                      assignee: str | None = None, idempotency_key: str | None = None) -> dict:
        return self._call("create_ticket", title=title, description=description, created_by=created_by,
                          labels=labels, assignee=assignee, idempotency_key=idempotency_key)["ticket"]

    def assign_ticket(self, ticket_id: str, assignee: str) -> dict | None:
        return self._call("assign_ticket", ticket_id=ticket_id, assignee=assignee)["ticket"]

    def mark_executed(self, ticket_id: str) -> dict | None:
        return self._call("mark_executed", ticket_id=ticket_id)["ticket"]

    def get_ticket(self, ticket_id: str) -> dict | None:
        return self._call("get_ticket", ticket_id=ticket_id)["ticket"]

    def list_tickets(self) -> list[dict]:
        return self._call("list_tickets")["tickets"]

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
