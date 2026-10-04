"""Local SQLite ticket board, data-shaped like Linear/GitHub Projects (id,
title, status, assignee, labels). The agent normally reaches it through the
MCP server in mcp_board/ (BOARD_BACKEND=local uses it directly). create /
assign / get / list only -- never delete.

create_ticket takes an idempotency key (the agent passes its run id): a
retried create -- after a dropped MCP connection, say -- returns the ticket
it already made instead of opening a second one."""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

import agentlab as lab

from . import config

# The one change the agent can make in the world, for Agent Lab's "what it can do" list.
CREATE_TICKET = lab.Action("create_ticket", "Open an IT ticket",
                           "Files a ticket on the IT team's board, only after a person approves that exact ticket.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    assignee TEXT,
    labels TEXT,
    created_by TEXT,
    created_at REAL NOT NULL,
    idempotency_key TEXT UNIQUE
);
"""

_COLUMNS = ["id", "title", "description", "status", "assignee", "labels", "created_by", "created_at"]


class BoardAdapter:
    transport = "local"

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or config.BOARD_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the server invokes the graph (and thus the
        # board) from a worker-thread pool. No concurrent-write contention in
        # this single-process demo, so one shared connection is fine.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tickets)")}
        if "idempotency_key" not in cols:  # boards created before the key existed
            self._conn.execute("ALTER TABLE tickets ADD COLUMN idempotency_key TEXT")
            self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS tickets_idem ON tickets(idempotency_key)")
        self._conn.commit()

    def create_ticket(
        self, title: str, description: str, created_by: str, labels: list[str] | None = None,
        assignee: str | None = None, idempotency_key: str | None = None,
    ) -> dict:
        if idempotency_key:
            row = self._conn.execute("SELECT id FROM tickets WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if row:
                return self.get_ticket(row[0])
        ticket_id = f"REQ-{uuid.uuid4().hex[:6]}"
        now = time.time()
        self._conn.execute(
            "INSERT INTO tickets (id, title, description, status, assignee, labels, created_by, created_at, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, title, description, "assigned" if assignee else "open", assignee,
             ",".join(labels or []), created_by, now, idempotency_key),
        )
        self._conn.commit()
        return self.get_ticket(ticket_id)

    def assign_ticket(self, ticket_id: str, assignee: str) -> dict | None:
        self._conn.execute(
            "UPDATE tickets SET assignee = ?, status = 'assigned' WHERE id = ?", (assignee, ticket_id)
        )
        self._conn.commit()
        return self.get_ticket(ticket_id)

    def mark_executed(self, ticket_id: str) -> dict | None:
        self._conn.execute("UPDATE tickets SET status = 'executed' WHERE id = ?", (ticket_id,))
        self._conn.commit()
        return self.get_ticket(ticket_id)

    def get_ticket(self, ticket_id: str) -> dict | None:
        row = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        return dict(zip(_COLUMNS, row)) if row else None

    def list_tickets(self) -> list[dict]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM tickets ORDER BY created_at DESC"
        ).fetchall()
        return [dict(zip(_COLUMNS, r)) for r in rows]
