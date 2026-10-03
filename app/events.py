"""The event bus is the primary artifact: every node in the graph publishes
here. The JSONL log, the peek-behind-the-curtain UI (via SSE), and evals all
read from this -- not from each other, and not from Langfuse, so any one
consumer can fail without taking the others down."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import redact


@dataclass
class Event:
    run_id: str
    node: str
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    def __init__(self, run_id: str, jsonl_path: Path | None = None):
        self.run_id = run_id
        self._subscribers: list[Callable[[Event], None]] = []
        self._fh = None
        if jsonl_path:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(jsonl_path, "a")

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subscribers.append(fn)

    def publish(self, node: str, event_type: str, **data: Any) -> Event:
        # Masked before any consumer sees it: the JSONL log, the live view, the evals' log readers.
        ev = Event(run_id=self.run_id, node=node, event_type=event_type,
                   data=redact.redact(data) if redact.enabled() else data)
        if self._fh:
            self._fh.write(json.dumps(ev.to_dict(), default=str) + "\n")
            self._fh.flush()
        for fn in list(self._subscribers):
            try:
                fn(ev)
            except Exception:
                pass  # a subscriber (e.g. a dropped SSE client) must never break the run
        return ev

    def close(self) -> None:
        if self._fh:
            self._fh.close()
