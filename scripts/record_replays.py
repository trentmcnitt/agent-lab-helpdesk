"""Records the public demo's replays: one real run per scenario, saved to
demo/replays/<scenario>.json, so the demo can show the full trace (peek view
included) with no model calls.

    ANTHROPIC_API_KEY=... uv run scripts/record_replays.py [req-012 req-011 ...]

A run that pauses for approval is recorded twice: once approved (its events,
reply and the ticket it opened) and once denied (a second real run, with its
digest aligned to the first so the approval card and both endings match).
Events go through the normal event bus, so trace masking applies."""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app import config  # noqa: E402
from app.board_adapter import BoardAdapter  # noqa: E402
from app.demo.engine import VISITOR  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.retrieval import HandbookIndex  # noqa: E402
from cli import load_seed_requests  # noqa: E402

OUT = config.REPO_ROOT / "demo" / "replays"
DEFAULT = ["req-012", "req-011", "req-019", "req-020", "req-021", "req-024", "req-005", "req-009"]


def _run(req: dict, index: HandbookIndex, approve: bool | None) -> dict:
    run_id = f"rec-{uuid.uuid4().hex[:8]}"
    events: list[dict] = []
    bus = EventBus(run_id=run_id)
    bus.subscribe(lambda ev: events.append(ev.to_dict()))
    board = BoardAdapter(Path(":memory:"))
    graph = build_graph(bus, index, board, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": run_id}}
    state = {"run_id": run_id, "request_id": req["id"], "requester_name": req["requester_name"],
             "requester_role": req["requester_role"], "requester_id": None, "channel": "helpdesk-requests",
             "thread_ts": "0", "message": req["message"], "auto_approve": False}
    result = graph.invoke(state, config=cfg)
    out = {"paused": "__interrupt__" in result}
    if not out["paused"]:
        return {**out, "pre": events, "final": result.get("final_response", "")}
    payload = result["__interrupt__"][0].value
    bus.publish(node="approval_gate", event_type="approval_requested",
                proposed_action=payload["proposed_action"], action_digest=payload["action_digest"])
    pre, events[:] = list(events), []
    result = graph.invoke(Command(resume={"approved": approve, "approver": VISITOR,
                                          "action_digest": payload["action_digest"]}), config=cfg)
    return {**out, "pre": pre, "action_digest": payload["action_digest"], "tail": list(events),
            "final": result.get("final_response", ""), "tickets": board.list_tickets()}


def record(req: dict, index: HandbookIndex) -> dict:
    first = _run(req, index, approve=True)
    rec = {"scenario": req["id"], "requester_name": req["requester_name"], "message": req["message"],
           "recorded_at": datetime.now().isoformat(timespec="seconds"), "model": config.MODEL_ID,
           "paused": first["paused"], "pre": first["pre"]}
    if not first["paused"]:
        rec["final"] = first["final"]
        return rec
    denied = _run(req, index, approve=False)
    if not denied["paused"]:
        raise RuntimeError(f"{req['id']}: the second run didn't pause for approval, so there's no denied tail")
    # The denied tail comes from a second run; point its digests at the first run's card.
    tail = json.loads(json.dumps(denied["tail"]).replace(denied["action_digest"], first["action_digest"]))
    rec.update(action_digest=first["action_digest"], tails={
        "approved": {"events": first["tail"], "final": first["final"], "tickets": first["tickets"]},
        "denied": {"events": tail, "final": denied["final"], "tickets": []},
    })
    return rec


def main() -> None:
    wanted = sys.argv[1:] or DEFAULT
    requests = {r["id"]: r for r in load_seed_requests()}
    index = HandbookIndex()
    OUT.mkdir(parents=True, exist_ok=True)
    for rid in wanted:
        rec = record(requests[rid], index)
        (OUT / f"{rid}.json").write_text(json.dumps(rec, indent=1, default=str))
        cost = sum(e["data"].get("cost_usd", 0) for e in rec["pre"] + [x for t in rec.get("tails", {}).values()
                                                                         for x in t["events"]]
                   if e["event_type"] == "llm_call")
        print(f"{rid}: {'paused for approval' if rec['paused'] else 'finished'}, recorded (${cost:.4f})")


if __name__ == "__main__":
    main()
