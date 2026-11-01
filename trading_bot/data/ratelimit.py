"""Request budgeting for market-data providers.

Free API tiers are tiny (Alpha Vantage allows 25 requests *per day*), so the
bot must never fire requests blindly.  :class:`RateLimiter` tracks a sliding
one-minute window and a sliding 24-hour window and can persist its state so a
restart does not reset the daily count.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

MINUTE = 60.0
DAY = 24 * 3600.0


class RateLimiter:
    def __init__(self, per_minute: int, per_day: int, state_file: str | os.PathLike[str] | None = None,
                 clock=time.time):
        if per_minute < 1 or per_day < 1:
            raise ValueError("rate limits must be >= 1")
        self.per_minute = per_minute
        self.per_day = per_day
        self._clock = clock
        self._lock = threading.Lock()
        self._events: deque[float] = deque()
        self._state_file = Path(state_file) if state_file else None
        self._load()

    # ------------------------------------------------------------ public
    def try_acquire(self) -> bool:
        """Consume one request slot if available; never blocks."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            if self._minute_count(now) >= self.per_minute or len(self._events) >= self.per_day:
                return False
            self._events.append(now)
            self._save()
            return True

    def acquire(self, timeout: float | None = None) -> bool:
        """Block until a slot is available (or ``timeout`` seconds elapse)."""
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            if self.try_acquire():
                return True
            wait = self.seconds_until_available()
            if deadline is not None and self._clock() + wait > deadline:
                return False
            time.sleep(min(max(wait, 0.05), 5.0))

    def remaining_minute(self) -> int:
        with self._lock:
            now = self._clock()
            self._prune(now)
            return max(0, self.per_minute - self._minute_count(now))

    def remaining_day(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return max(0, self.per_day - len(self._events))

    def seconds_until_available(self) -> float:
        with self._lock:
            now = self._clock()
            self._prune(now)
            waits = [0.0]
            minute_events = [t for t in self._events if now - t < MINUTE]
            if len(minute_events) >= self.per_minute:
                waits.append(minute_events[-self.per_minute] + MINUTE - now)
            if len(self._events) >= self.per_day:
                waits.append(self._events[0] + DAY - now)
            return max(waits)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._save()

    # ----------------------------------------------------------- internal
    def _minute_count(self, now: float) -> int:
        return sum(1 for t in self._events if now - t < MINUTE)

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0] >= DAY:
            self._events.popleft()

    def _load(self) -> None:
        if not self._state_file or not self._state_file.exists():
            return
        try:
            with self._state_file.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self._events = deque(float(t) for t in raw.get("events", []))
            self._prune(self._clock())
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("Could not read rate-limit state %s: %s", self._state_file, exc)

    def _save(self) -> None:
        if not self._state_file:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"events": list(self._events)}, fh)
            os.replace(tmp, self._state_file)
        except OSError as exc:  # pragma: no cover - disk problems must not stop trading
            logger.warning("Could not persist rate-limit state: %s", exc)
