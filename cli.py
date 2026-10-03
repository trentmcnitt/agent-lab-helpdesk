"""Command-line runner: `uv run cli.py run <request_id>` or `run-all`.
Real credentials are injected by the operator at launch time (see SETUP.md)
-- this script only ever reads from the environment."""
from __future__ import annotations

import argparse
import json
import uuid

from langgraph.checkpoint.memory import InMemorySaver

from app import config
from app.board_adapter import BoardAdapter
from app.boards import make_board
from app.events import EventBus
from app.graph import build_graph
from app.langfuse_sink import flush_langfuse
from app.retrieval import HandbookIndex
from app.slack_adapter import MockSlackAdapter


def load_seed_requests() -> list[dict]:
    return json.loads(config.SEED_REQUESTS_PATH.read_text())


def run_one(req: dict, index: HandbookIndex, board: BoardAdapter, auto_approve: bool = True, verbose: bool = True) -> dict:
    run_id = f"run-{req['id']}-{uuid.uuid4().hex[:6]}"
    bus = EventBus(run_id=run_id, jsonl_path=config.RUNS_DIR / f"{run_id}.jsonl")
    slack = MockSlackAdapter()

    posted = slack.post_message(req["channel"], req["requester_name"], req["message"])

    checkpointer = InMemorySaver()
    graph = build_graph(bus, index, board, checkpointer=checkpointer)

    initial_state = {
        "run_id": run_id,
        "request_id": req["id"],
        "requester_name": req["requester_name"],
        "requester_role": req["requester_role"],
        "channel": req["channel"],
        "thread_ts": posted["thread_ts"],
        "message": req["message"],
        "auto_approve": auto_approve,
    }
    thread_config = {"configurable": {"thread_id": run_id}}
    final_state = graph.invoke(initial_state, config=thread_config)

    slack.reply_in_thread(req["channel"], posted["thread_ts"], final_state.get("final_response", "(no response)"))
    bus.close()

    if verbose:
        print(f"[{req['id']}] category={final_state.get('category')} outcome={final_state.get('final_outcome')} run_id={run_id}")
        print(f"    -> {final_state.get('final_response')}")

    final_state["run_id"] = run_id
    return final_state


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("request_id")
    sub.add_parser("run-all")
    args = parser.parse_args()

    requests = load_seed_requests()
    index = HandbookIndex()
    board = make_board()

    if args.cmd == "run":
        req = next(r for r in requests if r["id"] == args.request_id)
        run_one(req, index, board)
    elif args.cmd == "run-all":
        for req in requests:
            run_one(req, index, board)

    flush_langfuse()


if __name__ == "__main__":
    main()
