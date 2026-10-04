"""Records the public demo's cassettes: one real run per scenario (PAID: real model calls),
saved as demo/cassettes/<scenario>.json. Everything else the demo plays is made from the
cassettes by scripts/regen_demo.py, with no model calls.

    ANTHROPIC_API_KEY=... uv run scripts/record_cassettes.py [req-012 req-011 ...]
    uv run scripts/regen_demo.py

Run it when regen_demo.py stops with "stale: prompt for <node> changed": a prompt changed, so
the old answers no longer belong to it. A run that pauses for approval is recorded up to the
pause; nothing after the approval calls the model, so both endings come from regen_demo.py."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import json  # noqa: E402

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from app import config  # noqa: E402
from app.board_adapter import BoardAdapter  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.graph import build_graph  # noqa: E402
from app.retrieval import HandbookIndex  # noqa: E402
from cli import load_seed_requests  # noqa: E402
from extract_cassettes import cassette  # noqa: E402

OUT = ROOT / "demo" / "cassettes"
DEFAULT = ["req-012", "req-011", "req-019", "req-020", "req-021", "req-024", "req-005", "req-009"]


def record(req: dict, index: HandbookIndex) -> dict:
    run_id = f"rec-{req['id']}"
    events: list[dict] = []
    bus = EventBus(run_id=run_id)
    bus.subscribe(lambda ev: events.append(ev.to_dict()))
    graph = build_graph(bus, index, BoardAdapter(Path(":memory:")), checkpointer=InMemorySaver())
    state = {"run_id": run_id, "request_id": req["id"], "requester_name": req["requester_name"],
             "requester_role": req["requester_role"], "requester_id": None, "channel": req["channel"],
             "thread_ts": "0", "message": req["message"], "auto_approve": False}
    result = graph.invoke(state, config={"configurable": {"thread_id": run_id}})
    rec = {"scenario": req["id"], "recorded_at": datetime.now().isoformat(timespec="seconds"), "model": config.MODEL_ID,
           "paused": "__interrupt__" in result, "pre": events}
    return cassette(rec, req)


def main() -> None:
    wanted = sys.argv[1:] or DEFAULT
    requests = {r["id"]: r for r in load_seed_requests()}
    index = HandbookIndex()
    OUT.mkdir(parents=True, exist_ok=True)
    for rid in wanted:
        c = record(requests[rid], index)
        (OUT / f"{rid}.json").write_text(json.dumps(c, indent=1, ensure_ascii=False) + "\n")
        cost = sum(config.cost_for(x["model"], x["usage"]["input_tokens"] - x["usage"]["cache_read"] - x["usage"]["cache_write"],
                                   x["usage"]["output_tokens"], x["usage"]["cache_read"], x["usage"]["cache_write"])
                   for x in c["calls"])
        print(f"{rid}: {len(c['calls'])} model calls recorded (${cost:.4f})")


if __name__ == "__main__":
    main()
