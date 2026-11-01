"""Budget-aware, cached access to market data."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from .providers import (
    MarketDataProvider,
    ProviderError,
    Quote,
    RateLimitError,
    quote_from_bars,
)
from .ratelimit import RateLimiter

logger = logging.getLogger(__name__)


@dataclass
class BarsResult:
    symbol: str
    interval: str
    bars: pd.DataFrame
    fetched_at: float
    stale: bool = False      # served from cache because of the request budget
    error: str | None = None # last error, if data could not be refreshed

    @property
    def age_seconds(self) -> float:
        return time.time() - self.fetched_at

    @property
    def empty(self) -> bool:
        return self.bars is None or self.bars.empty


@dataclass
class BudgetStatus:
    remaining_minute: int
    per_minute: int
    remaining_day: int
    per_day: int
    wait_seconds: float

    def describe(self) -> str:
        return (f"API budget: {self.remaining_minute}/{self.per_minute} per minute, "
                f"{self.remaining_day}/{self.per_day} per day")


class MarketDataService:
    """Serves bars from cache, refreshing them through the provider when the budget allows."""

    def __init__(self, provider: MarketDataProvider, limiter: RateLimiter, cache_ttl: float = 60.0, clock=time.time):
        self.provider = provider
        self.limiter = limiter
        self.cache_ttl = cache_ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], BarsResult] = {}
        self._inflight: dict[tuple[str, str], threading.Lock] = {}  # one provider call per symbol at a time

    # ------------------------------------------------------------- bars
    def get_bars(self, symbol: str, interval: str, limit: int = 300, force: bool = False,
                 max_age: float | None = None) -> BarsResult:
        """Return bars for ``symbol``.

        Cached data younger than ``max_age`` seconds (default: ``cache_ttl``) is
        returned immediately; ``force`` ignores the cache age.  Otherwise one
        request slot is consumed; if none is available the stale cache entry is
        returned (flagged ``stale=True``) or, without any cache, a
        :class:`RateLimitError` is raised.  Provider failures never evict good
        cached data.
        """
        symbol = symbol.upper()
        key = (symbol, interval)
        ttl = self.cache_ttl if max_age is None else max_age
        with self._lock:
            cached = self._cache.get(key)
            gate = self._inflight.setdefault(key, threading.Lock())
        if cached is not None and not force and self._age(cached) < ttl and not cached.stale:
            return cached
        with gate:  # concurrent refreshes of the same symbol run one after the other
            return self._refresh(key, limit, force, ttl)

    def _refresh(self, key: tuple[str, str], limit: int, force: bool, ttl: float) -> BarsResult:
        symbol, interval = key
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None and not force and self._age(cached) < ttl and not cached.stale:
            return cached  # refreshed by the call we were waiting for

        if not self.limiter.try_acquire():
            wait = self.limiter.seconds_until_available()
            if cached is not None:
                logger.debug("Budget exhausted; serving stale %s (%.0fs old)", symbol, self._age(cached))
                cached.stale = True
                return cached
            raise RateLimitError(f"Request budget exhausted; next slot in {wait:.0f}s")

        try:
            bars = self.provider.get_bars(symbol, interval, limit)
        except RateLimitError as exc:
            logger.warning("Provider rate limit hit for %s: %s", symbol, exc)
            if cached is not None:
                cached.stale = True
                cached.error = str(exc)
                return cached
            raise
        except ProviderError as exc:
            logger.error("Failed to fetch %s: %s", symbol, exc)
            if cached is not None:
                cached.error = str(exc)
                return cached
            raise
        result = BarsResult(symbol, interval, bars, self._clock())
        with self._lock:
            self._cache[key] = result
        return result

    def _age(self, result: BarsResult) -> float:
        return self._clock() - result.fetched_at

    def cached_bars(self, symbol: str, interval: str) -> BarsResult | None:
        with self._lock:
            return self._cache.get((symbol.upper(), interval))

    def get_quote(self, symbol: str, interval: str) -> Quote | None:
        """Derive a quote from cached bars without spending a request."""
        cached = self.cached_bars(symbol, interval)
        if cached is None or cached.empty:
            return None
        try:
            return quote_from_bars(symbol, cached.bars)
        except ProviderError:
            return None

    def invalidate(self, symbol: str | None = None) -> None:
        with self._lock:
            if symbol is None:
                self._cache.clear()
            else:
                for key in [k for k in self._cache if k[0] == symbol.upper()]:
                    del self._cache[key]

    # ----------------------------------------------------------- budget
    def budget(self) -> BudgetStatus:
        return BudgetStatus(
            self.limiter.remaining_minute(), self.limiter.per_minute,
            self.limiter.remaining_day(), self.limiter.per_day,
            self.limiter.seconds_until_available(),
        )

    def close(self) -> None:
        self.provider.close()


def utcnow() -> datetime:
    return datetime.now(UTC)
