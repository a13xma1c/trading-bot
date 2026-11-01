"""Shared fixtures and test doubles."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pytest

if TYPE_CHECKING:
    from trading_bot.engine import TradingEngine

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert isinstance(data, dict)
    return data


def make_ohlcv(n: int = 200, seed: int = 0, freq: str = "5min", start: str = "2024-03-01 14:30") -> pd.DataFrame:
    """Deterministic random-walk OHLCV frame indexed in UTC."""
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0, 0.5, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + rng.uniform(0, 0.5, n)
    low = np.minimum(open_, close) - rng.uniform(0, 0.5, n)
    volume = rng.integers(1_000, 10_000, n).astype(float)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC", name="time")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


@pytest.fixture
def ohlcv() -> pd.DataFrame:
    return make_ohlcv()


class FakeResponse:
    """Stand-in for :class:`requests.Response`."""

    def __init__(self, status_code: int = 200, json_data: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text if text else (json.dumps(json_data) if json_data is not None else "")

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Minimal stand-in for :class:`requests.Session` that records calls.

    Implements the ``HttpSession`` / ``HttpPostSession`` protocols used by the
    providers and the webhook channel.
    """

    def __init__(self, responses: list[FakeResponse] | None = None, exc: Exception | None = None) -> None:
        self.responses: list[FakeResponse] = list(responses or [])
        self.exc = exc
        self.calls: list[tuple[str, str, Any]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> FakeResponse:
        self.calls.append(("GET", url, dict(params or {})))
        if self.exc:
            raise self.exc
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)

    def post(self, url: str, data: Any = None, headers: dict[str, str] | None = None,
             timeout: float | None = None) -> FakeResponse:
        self.calls.append(("POST", url, data))
        if self.exc:
            raise self.exc
        return self.responses.pop(0) if self.responses else FakeResponse(200, {"ok": True})

    def close(self) -> None:
        pass


class FakeClock:
    """Controllable clock usable both as ``time.time`` and as a UTC ``datetime`` source."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.now, tz=UTC)


# ---------------------------------------------------------------- engines

def twelvedata_payload(bars: int = 300, last: str = "2024-03-15 15:55:00") -> dict[str, Any]:
    """A realistic Twelve Data ``/time_series`` payload with ``bars`` 5-minute bars ending at ``last``.

    No function-local imports here: this runs on the engine's fetch threads.
    """
    payload = load_fixture("twelvedata_time_series.json")
    t0 = dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    payload["values"] = [{
        "datetime": (t0 - dt.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S"),
        "open": "172", "high": "173", "low": "171", "close": f"{172 + (i % 5) * 0.1:.2f}", "volume": "1000",
    } for i in range(bars)]
    return payload


class TwelveDataSession(FakeSession):
    """Serves Twelve Data payloads; ``mode`` switches to failure responses."""

    def __init__(self) -> None:
        super().__init__()
        self.mode = "ok"  # ok | auth | 429
        self.last = "2024-03-15 15:55:00"

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> FakeResponse:
        self.calls.append(("GET", url, dict(params or {})))
        if self.mode == "auth":
            return FakeResponse(200, {"code": 401, "message": "**apikey** parameter is incorrect", "status": "error"})
        if self.mode == "429":
            return FakeResponse(429, {"code": 429, "message": "You have run out of API credits", "status": "error"})
        if url.endswith("/symbol_search"):
            query = str((params or {}).get("symbol", "")).upper()
            return FakeResponse(200, {"data": [{"symbol": query, "instrument_name": f"{query.title()} Holdings",
                                                "country": "United States", "instrument_type": "Common Stock"}]})
        return FakeResponse(200, twelvedata_payload(last=self.last))


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[TradingEngine]:
    """Engine on the demo provider with a frozen clock, market hours disabled and an always-buy strategy."""
    from trading_bot.broker import PaperBroker
    from trading_bot.config import AppConfig
    from trading_bot.data import DemoProvider, MarketDataService, RateLimiter
    from trading_bot.engine import TradingEngine
    from trading_bot.notify import Notifier
    from trading_bot.storage import Storage
    from trading_bot.strategy import StrategyDefinition, StrategyStore

    clock = FakeClock(1_750_000_000.0)
    config = AppConfig.from_dict({
        "watchlist": ["AAPL", "MSFT"], "scan_interval": 1,
        "data": {"provider": "demo", "interval": "5min", "bars": 120, "cache_ttl_seconds": 1},
        "trading": {"trading_enabled": False},
        "market_hours": {"enabled": False},
    })
    service = MarketDataService(DemoProvider(clock=clock.datetime), RateLimiter(1000, 100000, clock=clock),
                                cache_ttl=1, clock=clock)
    store = StrategyStore(tmp_path / "strategies.json")
    store.load()
    store.upsert(StrategyDefinition(name="Always", buy_rule="close > 0"))
    eng = TradingEngine(config, service, store, Notifier([]), PaperBroker(10_000), Storage(":memory:"))
    eng._now = clock.datetime
    yield eng
    eng.close()


@pytest.fixture
def td_engine(tmp_path: Path) -> Iterator[tuple[TradingEngine, TwelveDataSession]]:
    """Fully built engine (``build_engine``) on the real Twelve Data provider with a fake HTTP session."""
    from trading_bot.app import build_engine
    from trading_bot.config import AppConfig
    from trading_bot.data import TwelveDataProvider

    config = AppConfig.from_dict({
        "watchlist": ["AAPL", "MSFT"], "scan_interval": 1, "data_dir": str(tmp_path / "bot_data"),
        "strategies_file": str(tmp_path / "strategies.json"),
        "data": {"provider": "twelvedata", "api_key": "k", "interval": "5min", "bars": 300},
        "market_hours": {"enabled": False},
    })
    session = TwelveDataSession()
    eng = build_engine(config, provider=TwelveDataProvider("k", session=session))
    yield eng, session
    eng.close()


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    """One headless Chromium for the whole session (Playwright's sync API allows a single instance per thread)."""
    from .web_helpers import playwright_browser

    yield from playwright_browser()
