"""A run paused at the approval gate must survive the process dying: a fresh
checkpointer + a fresh graph over the same SQLite file resume it, without
re-running any model call."""
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.events import EventBus
from app.graph import ClassifyResult, ProposedAction, build_graph
from conftest import FakeLLM

STATE = {
    "run_id": "run-1", "request_id": "run-1", "requester_name": "Test User", "requester_role": "viewer",
    "channel": "helpdesk-requests", "thread_ts": "1", "message": "please open a ticket", "auto_approve": False,
}
CFG = {"configurable": {"thread_id": "run-1"}}


def test_paused_approval_survives_a_restart(tmp_path, index, board):
    db = tmp_path / "checkpoints.db"

    # "Process 1": run until the approval gate, then die.
    conn = sqlite3.connect(db, check_same_thread=False)
    llm = FakeLLM({
        ClassifyResult: ClassifyResult(category="needs_write", rationale="stub", confidence=0.95),
        ProposedAction: ProposedAction(action_type="create_ticket", title="durable ticket", description="stub"),
    })
    first = build_graph(EventBus("p1"), index, board, checkpointer=SqliteSaver(conn), llm=llm)
    paused = first.invoke(STATE, config=CFG)
    digest = paused["__interrupt__"][0].value["action_digest"]
    conn.close()
    del first

    # "Process 2": nothing in memory survives. An empty FakeLLM raises if resume
    # tried to call the model again.
    conn = sqlite3.connect(db, check_same_thread=False)
    second = build_graph(EventBus("p2"), index, board, checkpointer=SqliteSaver(conn), llm=FakeLLM({}))
    assert second.get_state(CFG).next == ("approval_gate",)
    assert second.get_state(CFG).values["thread_ts"] == "1"

    approver = {"id": "U_APPROVER", "name": "After Restart", "role": "admin", "via": "slack"}
    done = second.invoke(Command(resume={"approved": True, "approver": approver, "action_digest": digest}), config=CFG)
    assert done["final_outcome"] == "executed"
    assert [t["title"] for t in board.list_tickets()] == ["durable ticket"]
    conn.close()
