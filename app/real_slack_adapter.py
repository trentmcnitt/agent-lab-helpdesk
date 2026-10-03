"""Real Slack behind the same interface as MockSlackAdapter, so the graph,
board, and evals don't change. A bot token can't post as a human, so seeded
requests are posted by the app under a display-name override labeled
"(seeded)" and registered here -- that's how the Socket Mode listener tells a
seeded request apart from the app's own replies."""
from __future__ import annotations

import json
import threading
import time

from slack_sdk import WebClient

from . import config

AGENT_NAME = "helpdesk-agent"


class RealSlackAdapter:
    def __init__(self, bot_token: str, channel_name: str | None = None):
        self.client = WebClient(token=bot_token)
        auth = self.client.auth_test()
        self.bot_user_id = auth["user_id"]
        self.bot_id = auth.get("bot_id")
        self.team = auth.get("team")
        self.channel_name = (channel_name or config.SLACK_CHANNEL).lstrip("#")
        self.channel_id = self._resolve_channel(self.channel_name)
        self.client.conversations_join(channel=self.channel_id)  # no-op if already a member
        self._names: dict[str, str] = {}
        self._seeds: dict[str, dict] = {}
        self._lock = threading.Lock()
        try:
            self._roles = json.loads(config.SLACK_ROLES_PATH.read_text())
        except FileNotFoundError:
            self._roles = {}

    def _resolve_channel(self, name: str) -> str:
        cursor = None
        while True:
            resp = self.client.conversations_list(
                types="public_channel", exclude_archived=True, limit=200, cursor=cursor
            )
            for c in resp["channels"]:
                if c["name"] == name:
                    return c["id"]
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                raise RuntimeError(f"Public Slack channel #{name} not found")

    def _cid(self, channel: str) -> str:
        return self.channel_id if channel.lstrip("#") == self.channel_name else channel

    def display_name(self, user_id: str) -> str:
        if user_id not in self._names:
            profile = self.client.users_info(user=user_id)["user"]
            p = profile.get("profile", {})
            self._names[user_id] = p.get("display_name") or p.get("real_name") or profile.get("name", user_id)
        return self._names[user_id]

    def role_for(self, user_id: str) -> str:
        return self._roles.get(user_id, "viewer")

    def is_self(self, event: dict) -> bool:
        return bool(self.bot_id and event.get("bot_id") == self.bot_id) or event.get("user") == self.bot_user_id

    def register_seed(self, ts: str, info: dict) -> None:
        with self._lock:
            self._seeds[ts] = info

    def pop_seed(self, ts: str, wait: float = 3.0) -> dict | None:
        # The message event can arrive over the socket before chat.postMessage's
        # response has been registered, so give registration a moment.
        deadline = time.time() + wait
        while True:
            with self._lock:
                if ts in self._seeds:
                    return self._seeds.pop(ts)
            if time.time() >= deadline:
                return None
            time.sleep(0.1)

    def claim_seed(self, ts: str) -> dict | None:
        """Takes a seed without waiting: whoever claims it first starts the run, once."""
        with self._lock:
            return self._seeds.pop(ts, None)

    def post_message(self, channel: str, user: str, text: str, thread_ts: str | None = None,
                     label: str = "seeded") -> dict:
        kwargs = {"channel": self._cid(channel), "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if user != AGENT_NAME:
            kwargs["username"] = f"{user} ({label})"
        ts = self.client.chat_postMessage(**kwargs)["ts"]
        return {"type": "message", "channel": channel, "user": user, "text": text, "ts": ts, "thread_ts": thread_ts or ts}

    def reply_in_thread(
        self, channel: str, thread_ts: str, text: str, user: str = AGENT_NAME, blocks: list | None = None
    ) -> dict:
        kwargs = {"channel": self._cid(channel), "thread_ts": thread_ts, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        ts = self.client.chat_postMessage(**kwargs)["ts"]
        return {"type": "message", "channel": channel, "user": user, "text": text, "ts": ts, "thread_ts": thread_ts}

    def ephemeral(self, channel: str, user_id: str, text: str, thread_ts: str | None = None) -> None:
        kwargs = {"channel": self._cid(channel), "user": user_id, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        self.client.chat_postEphemeral(**kwargs)

    def update_message(self, channel: str, ts: str, text: str, blocks: list | None = None) -> None:
        self.client.chat_update(channel=self._cid(channel), ts=ts, text=text, blocks=blocks or [])

    def _to_event(self, m: dict) -> dict:
        if m.get("username"):
            user = m["username"]
        elif self.is_self(m):
            user = AGENT_NAME
        elif m.get("user"):
            user = self.display_name(m["user"])
        else:
            user = "unknown"
        return {
            "type": "message",
            "channel": self.channel_name,
            "user": user,
            "text": m.get("text", ""),
            "ts": m["ts"],
            "thread_ts": m.get("thread_ts", m["ts"]),
        }

    def get_thread(self, channel: str, thread_ts: str) -> list[dict]:
        resp = self.client.conversations_replies(channel=self._cid(channel), ts=thread_ts, limit=100)
        return [self._to_event(m) for m in resp["messages"]]

    def get_all_threads(self, channel: str) -> list[list[dict]]:
        resp = self.client.conversations_history(channel=self._cid(channel), limit=50)
        return [[self._to_event(m)] for m in reversed(resp["messages"])]
