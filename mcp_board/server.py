"""The Waypoint ticket board as an MCP server (stdio).

Deliberately policy-free: it exposes create / assign / mark_executed / get /
list -- never delete -- and trusts its caller. The permission gate lives in
the agent, above the protocol, so the policy stays in one place; any other
MCP client pointed at this server gets exactly the write scope a real board
integration would grant, and no more.

    python -m mcp_board.server        # BOARD_DB_PATH picks the SQLite file
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from app.board_adapter import BoardAdapter

_board = BoardAdapter(Path(os.environ["BOARD_DB_PATH"]) if os.environ.get("BOARD_DB_PATH") else None)


class Ticket(BaseModel):
    id: str
    title: str
    description: str | None = None
    status: str
    assignee: str | None = None
    labels: str | None = None
    created_by: str | None = None
    created_at: float


class TicketResult(BaseModel):
    ticket: Ticket | None


class TicketList(BaseModel):
    tickets: list[Ticket]


def _one(row: dict | None) -> TicketResult:
    return TicketResult(ticket=Ticket(**row) if row else None)

server = MCPServer(
    "waypoint-board",
    instructions="Northwire's Waypoint ticket board. Create, assign, and read tickets. There is no delete.",
)

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Idempotent when called with an idempotency_key, which is how the agent always calls it.
_CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


@server.tool(annotations=_CREATE, structured_output=True)
def create_ticket(title: str, description: str, created_by: str, labels: list[str] | None = None,
                  assignee: str | None = None, idempotency_key: str | None = None) -> TicketResult:
    """Open a ticket on the board, optionally already assigned. With an idempotency_key, a repeat
    call returns the ticket the first call created instead of opening another."""
    return _one(_board.create_ticket(title, description, created_by=created_by, labels=labels,
                                     assignee=assignee, idempotency_key=idempotency_key))


@server.tool(annotations=_WRITE, structured_output=True)
def assign_ticket(ticket_id: str, assignee: str) -> TicketResult:
    """Assign an existing ticket to someone."""
    return _one(_board.assign_ticket(ticket_id, assignee))


@server.tool(annotations=_WRITE, structured_output=True)
def mark_executed(ticket_id: str) -> TicketResult:
    """Mark a ticket's requested action as carried out."""
    return _one(_board.mark_executed(ticket_id))


@server.tool(annotations=_READ, structured_output=True)
def get_ticket(ticket_id: str) -> TicketResult:
    """Read one ticket (null if it doesn't exist)."""
    return _one(_board.get_ticket(ticket_id))


@server.tool(annotations=_READ, structured_output=True)
def list_tickets() -> TicketList:
    """All tickets, newest first."""
    return TicketList(tickets=[Ticket(**r) for r in _board.list_tickets()])


if __name__ == "__main__":
    server.run("stdio")
