"""Guards for the public demo: per-IP rate limits and a hard daily spend cap.

Both take an injectable clock, so tests can move time instead of waiting.
Neither calls a model; they decide whether a live run may start at all."""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class RateLimiter:
    """Live runs per client IP: at most `per_minute` in any rolling 60 s, and
    `per_day` per UTC day. Replay mode doesn't count against it."""

    def __init__(self, per_minute: int, per_day: int, clock: Callable[[], float] = time.time):
        self.per_minute, self.per_day, self._clock = per_minute, per_day, clock
        self._recent: dict[str, deque] = defaultdict(deque)
        self._daily: dict[tuple[str, str], int] = defaultdict(int)
        self._lock = threading.Lock()

    def _day(self, now: float) -> str:
        return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")

    def check(self, ip: str) -> str | None:
        """Records one attempt and returns None, or returns why it's refused."""
        now = self._clock()
        with self._lock:
            recent = self._recent[ip]
            while recent and now - recent[0] >= 60:
                recent.popleft()
            day_key = (ip, self._day(now))
            if self._daily[day_key] >= self.per_day:
                return f"daily limit of {self.per_day} live runs reached for your address"
            if len(recent) >= self.per_minute:
                return f"limit of {self.per_minute} live runs a minute reached; try again shortly"
            recent.append(now)
            self._daily[day_key] += 1
            return None


class SpendCap:
    """A hard daily cap on model spend across all visitors. Spend is recorded from
    each run's own cost events and persisted, so a restart doesn't reset the day.
    `exhausted()` is checked before a live run starts; once it's true, the demo
    serves recorded replays until the UTC day rolls over."""

    def __init__(self, daily_usd: float, ledger_path: Path, clock: Callable[[], float] = time.time):
        self.daily_usd, self._path, self._clock = daily_usd, ledger_path, clock
        self._lock = threading.Lock()
        try:
            self._ledger = json.loads(ledger_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self._ledger = {}

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%d")

    def spent_today(self) -> float:
        with self._lock:
            return float(self._ledger.get(self._today(), 0.0))

    def exhausted(self) -> bool:
        return self.spent_today() >= self.daily_usd

    def record(self, usd: float) -> None:
        with self._lock:
            day = self._today()
            self._ledger = {day: float(self._ledger.get(day, 0.0)) + usd}  # keep only today
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._ledger))
            tmp.replace(self._path)
