"""Socket Mode listener: a top-level message in the helpdesk channel starts a
run; Approve/Deny buttons on the approval message resume it. Socket Mode
means no public URL -- the app dials out to Slack."""
from __future__ import annotations

import json
import logging
from typing import Callable

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .real_slack_adapter import RealSlackAdapter

log = logging.getLogger("helpdesk.slack")


def start_listener(
    adapter: RealSlackAdapter,
    bot_token: str,
    app_token: str,
    on_request: Callable[..., None],
    on_decision: Callable[..., None],
) -> SocketModeHandler:
    # Seeded requests are the app's own posts, so self-events must not be dropped;
    # is_self() + the seed registry keep the agent from answering its own replies.
    app = App(token=bot_token, ignoring_self_events_enabled=False)

    @app.event("message")
    def on_message(event, logger):
        log.info(
            "slack message event: channel=%s subtype=%s bot_id=%s user=%s ts=%s thread_ts=%s",
            event.get("channel"), event.get("subtype"), event.get("bot_id"), event.get("user"),
            event.get("ts"), event.get("thread_ts"),
        )
        if event.get("channel") != adapter.channel_id:
            return
        if event.get("subtype") not in (None, "bot_message"):
            return  # edits, deletes, joins
        ts = event["ts"]
        if event.get("thread_ts") and event["thread_ts"] != ts:
            return  # replies inside a thread aren't new requests
        text = event.get("text", "")
        if adapter.is_self(event):
            seed = adapter.pop_seed(ts)
            if seed is not None:
                on_request(thread_ts=ts, text=text, **seed)
            return
        user = event.get("user")
        if user:
            on_request(
                thread_ts=ts,
                text=text,
                requester_name=adapter.display_name(user),
                requester_role=adapter.role_for(user),
                requester_id=user,  # verified by Slack; the approval gate uses it to block self-approval
            )

    def _decide(ack, body, approved: bool) -> None:
        ack()
        raw = body["actions"][0]["value"]
        try:
            value = json.loads(raw)  # {"run_id", "digest"} the approver was shown
        except json.JSONDecodeError:
            value = {"run_id": raw}  # a button from before approvals were digest-bound
        where = ((body.get("channel") or {}).get("id"), (body.get("container") or {}).get("message_ts"))
        # body["user"]["id"] comes from Slack's signed Socket Mode payload, not from the button value.
        on_decision(value["run_id"], approved, body["user"]["id"], value.get("digest"), where if all(where) else None)

    @app.action("approve_request")
    def approve(ack, body):
        _decide(ack, body, True)

    @app.action("deny_request")
    def deny(ack, body):
        _decide(ack, body, False)

    handler = SocketModeHandler(app, app_token)
    handler.connect()  # non-blocking; runs on its own threads
    return handler
