"""An owned Slack-like message surface, used when no Slack tokens are set.
Its event shape mirrors Slack's Bolt SDK ({channel, user, text, ts,
thread_ts}), and it shares one interface with real_slack_adapter.py, so the
graph never knows which one it's talking to."""
from __future__ import annotations

import time
import uuid
from collections import defaultdict


class MockSlackAdapter:
    def __init__(self):
        # channel -> list of message events, in post order
        self._threads: dict[str, list[dict]] = defaultdict(list)

    def post_message(self, channel: str, user: str, text: str, thread_ts: str | None = None,
                     label: str | None = None) -> dict:
        ts = f"{time.time():.6f}"
        event = {
            "type": "message",
            "channel": channel,
            "user": user,
            "text": text,
            "ts": ts,
            "thread_ts": thread_ts or ts,
        }
        self._threads[channel].append(event)
        return event

    def reply_in_thread(self, channel: str, thread_ts: str, text: str, user: str = "helpdesk-agent") -> dict:
        return self.post_message(channel, user, text, thread_ts=thread_ts)

    def get_thread(self, channel: str, thread_ts: str) -> list[dict]:
        return [m for m in self._threads[channel] if m["thread_ts"] == thread_ts]

    def get_all_threads(self, channel: str) -> list[list[dict]]:
        by_thread: dict[str, list[dict]] = defaultdict(list)
        for m in self._threads[channel]:
            by_thread[m["thread_ts"]].append(m)
        return [by_thread[k] for k in sorted(by_thread.keys())]
