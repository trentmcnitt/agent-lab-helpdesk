"""Masks secret-looking content before it leaves the run: the JSONL event log,
the live view's event stream, and Langfuse. Helpdesk channels attract pasted
passwords and keys, and a trace is the wrong place to keep them.

It's a pattern screen, so it catches the common shapes (provider API keys,
Slack/GitHub tokens, JWTs, private-key blocks, "my password is ...", email
addresses, phone numbers) and misses anything that doesn't look like one. The
model itself still sees the raw message; only the traces are masked.

TRACE_CONTENT=full turns it off, for local demos on fake data."""
from __future__ import annotations

import re
from typing import Any

from . import config

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[private key]"),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"), "[api key]"),
    (re.compile(r"\bx(?:ox[abposr]|app)-[A-Za-z0-9-]{10,}"), "[slack token]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[github token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[aws key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[jwt]"),
    # Keep the word so the trace still says a password was shared; drop the value.
    (re.compile(r"(?i)\b(password|passcode|passphrase|pwd|pw|pin)(\s*(?:is|was|=|:)\s*)[\"']?\S+"), r"\1\2[redacted]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[email]"),
    (re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"), "[phone]"),
]


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any) -> Any:
    """Recursively masks strings inside dicts, lists and tuples; other values pass through."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v) for v in value)
    return value


def enabled() -> bool:
    return config.TRACE_CONTENT != "full"


def langfuse_mask(*, data: Any, **kwargs: Any) -> Any:
    return redact(data)
