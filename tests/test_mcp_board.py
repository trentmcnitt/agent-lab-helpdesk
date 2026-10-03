"""The board over real MCP (stdio subprocess). The permission gate is on the
client side of the protocol: a forbidden action is refused before any MCP
call is made, and the server itself offers no delete at all."""
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.graph import ClassifyResult, ProposedAction, build_graph
from app.mcp_board_client import MCPBoardClient
from conftest import FakeLLM


@pytest.fixture
def mcp_board(tmp_path):
    client = MCPBoardClient(tmp_path / "board.db")
    yield client
    client.close()


def _state():
    return {
        "run_id": "t", "request_id": "t", "requester_name": "Test User", "requester_role": "viewer",
        "channel": "helpdesk-requests", "thread_ts": "1", "message": "stub", "auto_approve": True,
    }


def _llm(action_type):
    return FakeLLM({
        ClassifyResult: ClassifyResult(category="needs_write", rationale="stub", confidence=0.95),
        ProposedAction: ProposedAction(action_type=action_type, title="stub action", description="stub"),
    })


def test_server_offers_no_delete(mcp_board):
    assert set(mcp_board.tool_names) == {"create_ticket", "assign_ticket", "mark_executed", "get_ticket", "list_tickets"}


def test_approved_write_goes_over_mcp(bus, index, mcp_board):
    seen = []
    bus.subscribe(lambda ev: seen.append(ev))
    graph = build_graph(bus, index, mcp_board, checkpointer=InMemorySaver(), llm=_llm("create_ticket"))
    result = graph.invoke(_state(), config={"configurable": {"thread_id": "t"}})
    assert result["final_outcome"] == "executed"
    assert [t["status"] for t in mcp_board.list_tickets()] == ["open"]  # a ticket is opened, not "done"
    assert any(ev.event_type == "tool_call" and ev.data["transport"] == "mcp:stdio" for ev in seen)


def test_forbidden_action_never_reaches_the_server(bus, index, mcp_board):
    graph = build_graph(bus, index, mcp_board, checkpointer=InMemorySaver(), llm=_llm("disable_mfa"))
    result = graph.invoke(_state(), config={"configurable": {"thread_id": "t"}})
    assert result["final_outcome"] == "escalated"
    assert mcp_board.list_tickets() == []


def test_board_subprocess_gets_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-should-not-leak")
    client = MCPBoardClient(tmp_path / "board.db")
    try:
        env = client._params.env
        assert set(env) == {"PATH", "HOME", "BOARD_DB_PATH", "HELPDESK_NO_DOTENV"}
        assert not any("should-not-leak" in v for v in env.values())
    finally:
        client.close()


def test_board_process_dying_is_survived_without_a_duplicate_ticket(tmp_path):
    import os
    import signal
    import subprocess

    client = MCPBoardClient(tmp_path / "board.db")
    try:
        first = client.create_ticket("t", "d", created_by="x", idempotency_key="run-1")
        # Kill only this test process's own board child (never the live server's).
        children = subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True).stdout.split()
        assert children
        for pid in children:
            os.kill(int(pid), signal.SIGKILL)
        # The next call reconnects, and the same run's create returns the ticket it already made.
        again = client.create_ticket("t", "d", created_by="x", idempotency_key="run-1")
        assert again["id"] == first["id"]
        assert len(client.list_tickets()) == 1
    finally:
        client.close()


def test_assign_is_a_single_idempotent_create(tmp_path):
    from app.board_adapter import BoardAdapter
    board = BoardAdapter(tmp_path / "b.db")
    t = board.create_ticket("t", "d", created_by="x", assignee="Dana", idempotency_key="k")
    assert t["status"] == "assigned" and t["assignee"] == "Dana"
    assert board.create_ticket("t", "d", created_by="x", assignee="Dana", idempotency_key="k")["id"] == t["id"]
