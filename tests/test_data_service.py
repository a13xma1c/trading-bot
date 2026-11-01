import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_bot.data import (
    DemoProvider,
    MarketDataService,
    ProviderError,
    RateLimiter,
    RateLimitError,
)

from .conftest import FakeClock

# ------------------------------------------------------------------ limiter

def test_rate_limiter_minute_and_day_windows() -> None:
    clock = FakeClock()
    rl = RateLimiter(per_minute=2, per_day=3, clock=clock)
    assert rl.try_acquire() and rl.try_acquire()
    assert not rl.try_acquire()  # minute exhausted
    assert rl.remaining_minute() == 0 and rl.remaining_day() == 1
    assert rl.seconds_until_available() == pytest.approx(60.0)
    clock.advance(61)
    assert rl.remaining_minute() == 2
    assert rl.try_acquire()
    assert not rl.try_acquire()  # day exhausted
    assert rl.seconds_until_available() == pytest.approx(24 * 3600 - 61)
    clock.advance(24 * 3600)
    assert rl.remaining_day() == 3
    assert rl.try_acquire()
    rl.reset()
    assert rl.remaining_day() == 3


def test_rate_limiter_persists_state(tmp_path: Path) -> None:
    clock = FakeClock()
    state = tmp_path / "rl.json"
    rl = RateLimiter(5, 10, state_file=state, clock=clock)
    for _ in range(4):
        assert rl.try_acquire()
    again = RateLimiter(5, 10, state_file=state, clock=clock)
    assert again.remaining_day() == 6
    assert again.remaining_minute() == 1
    state.write_text("garbage")
    assert RateLimiter(5, 10, state_file=state, clock=clock).remaining_day() == 10


def test_rate_limiter_blocking_acquire_timeout() -> None:
    clock = FakeClock()
    rl = RateLimiter(1, 10, clock=clock)
    assert rl.acquire(timeout=0)
    assert not rl.acquire(timeout=0)


def test_rate_limiter_validation() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0, 1)


# ------------------------------------------------------------------ service

class CountingProvider(DemoProvider):
    def __init__(self, fail: Exception | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail = fail

    def get_bars(self, symbol: str, interval: str = "5min", limit: int = 300) -> pd.DataFrame:
        if self.fail is not None:
            raise self.fail
        return super().get_bars(symbol, interval, limit)


def make_service(per_minute: int = 100, per_day: int = 1000, ttl: float = 60, clock: FakeClock | None = None,
                 provider: CountingProvider | None = None) -> tuple[MarketDataService, CountingProvider, FakeClock]:
    clock = clock or FakeClock()
    provider = provider or CountingProvider(clock=clock.datetime)
    limiter = RateLimiter(per_minute, per_day, clock=clock)
    svc = MarketDataService(provider, limiter, cache_ttl=ttl, clock=clock)
    return svc, provider, clock


def test_service_caches_within_ttl() -> None:
    svc, provider, clock = make_service()
    a = svc.get_bars("AAPL", "5min", 50)
    b = svc.get_bars("aapl", "5min", 50)
    assert a is b and provider.calls == 1 and not a.stale
    clock.advance(61)
    c = svc.get_bars("AAPL", "5min", 50)
    assert c is not a and provider.calls == 2
    svc.get_bars("AAPL", "5min", 50, force=True)
    assert provider.calls == 3
    assert svc.cached_bars("AAPL", "5min") is not None
    svc.invalidate("AAPL")
    assert svc.cached_bars("AAPL", "5min") is None


def test_service_serves_stale_when_budget_exhausted() -> None:
    svc, provider, clock = make_service(per_minute=1, ttl=10)
    first = svc.get_bars("AAPL", "5min", 50)
    clock.advance(20)
    second = svc.get_bars("AAPL", "5min", 50)
    assert second is first and second.stale
    assert provider.calls == 1
    with pytest.raises(RateLimitError):
        svc.get_bars("MSFT", "5min", 50)  # nothing cached for MSFT
    status = svc.budget()
    assert status.remaining_minute == 0 and "per minute" in status.describe()


def test_service_keeps_cache_on_provider_errors() -> None:
    svc, provider, clock = make_service(ttl=10)
    good = svc.get_bars("AAPL", "5min", 50)
    clock.advance(20)
    provider.fail = ProviderError("down")
    again = svc.get_bars("AAPL", "5min", 50)
    assert again is good and again.error == "down"
    provider.fail = RateLimitError("slow down")
    again = svc.get_bars("AAPL", "5min", 50)
    assert again is good and again.stale
    with pytest.raises(RateLimitError):
        svc.get_bars("NEW", "5min", 50)
    provider.fail = ProviderError("down")
    with pytest.raises(ProviderError):
        svc.get_bars("NEW2", "5min", 50)


def test_service_serialises_concurrent_refreshes_of_a_symbol() -> None:
    import threading

    class SlowProvider(CountingProvider):
        def get_bars(self, symbol: str, interval: str = "5min", limit: int = 300) -> pd.DataFrame:
            time.sleep(0.05)
            return super().get_bars(symbol, interval, limit)

    clock = FakeClock()
    svc, provider, clock = make_service(provider=SlowProvider(clock=clock.datetime), clock=clock)
    threads = [threading.Thread(target=lambda: svc.get_bars("AAPL", "5min", 50, force=True)) for _ in range(4)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert provider.calls == 4  # forced refreshes all hit the provider ...
    assert time.monotonic() - t0 >= 0.2  # ... but strictly one after the other
    # Non-forced callers waiting behind a refresh reuse its result instead of fetching again.
    clock.advance(100)
    threads = [threading.Thread(target=lambda: svc.get_bars("AAPL", "5min", 50)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert provider.calls == 5


def test_service_quote_from_cache() -> None:
    svc, provider, clock = make_service()
    assert svc.get_quote("AAPL", "5min") is None
    svc.get_bars("AAPL", "5min", 50)
    q = svc.get_quote("AAPL", "5min")
    assert q is not None and q.symbol == "AAPL"
