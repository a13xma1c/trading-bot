from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import requests

from trading_bot.data import (
    AlphaVantageProvider,
    AuthenticationError,
    CSVProvider,
    DemoProvider,
    ProviderError,
    RateLimitError,
    SymbolNotFoundError,
    TwelveDataProvider,
    create_provider,
)
from trading_bot.data.providers import quote_from_bars

from .conftest import FakeResponse, FakeSession, load_fixture

# ---------------------------------------------------------------- Alpha Vantage

def test_alphavantage_requires_key_at_request_time() -> None:
    provider = AlphaVantageProvider("")  # construction must succeed so the app can start without a key
    with pytest.raises(AuthenticationError, match="requires an API key"):
        provider.get_bars("IBM")
    assert create_provider("twelvedata", "").name == "twelvedata"


def test_alphavantage_intraday_parsing() -> None:
    session = FakeSession([FakeResponse(200, load_fixture("alphavantage_intraday.json"))])
    p = AlphaVantageProvider("key", session=session)
    df = p.get_bars("ibm", "5min", 3)
    assert len(df) == 3  # limit applied, oldest dropped
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.is_monotonic_increasing
    assert isinstance(df.index, pd.DatetimeIndex) and str(df.index.tz) == "UTC"
    # 19:55 US/Eastern (EDT, UTC-4) -> 23:55 UTC
    assert df.index[-1] == pd.Timestamp("2024-03-15 23:55", tz="UTC")
    assert df["close"].iloc[-1] == 191.1
    method, url, params = session.calls[0]
    assert params["function"] == "TIME_SERIES_INTRADAY" and params["symbol"] == "IBM"
    assert params["interval"] == "5min" and params["outputsize"] == "compact"


def test_alphavantage_daily_parsing_and_full_outputsize() -> None:
    session = FakeSession([FakeResponse(200, load_fixture("alphavantage_daily.json"))])
    df = AlphaVantageProvider("key", session=session).get_bars("IBM", "1day", 500)
    assert len(df) == 3
    assert df["volume"].iloc[-1] == 5_000_000
    assert session.calls[0][2]["function"] == "TIME_SERIES_DAILY"
    assert session.calls[0][2]["outputsize"] == "full"


def test_alphavantage_quote() -> None:
    session = FakeSession([FakeResponse(200, load_fixture("alphavantage_quote.json"))])
    q = AlphaVantageProvider("key", session=session).get_quote("ibm")
    assert q.symbol == "IBM" and q.price == 192.5 and q.change == 1.5
    assert q.change_pct == pytest.approx(0.7853)
    assert q.previous_close == 191.0


def test_alphavantage_search() -> None:
    payload = {"bestMatches": [{"1. symbol": "IBM", "2. name": "International Business Machines", "3. type": "Equity", "4. region": "United States"}]}
    session = FakeSession([FakeResponse(200, payload)])
    out = AlphaVantageProvider("key", session=session).search("ibm")
    assert out[0].symbol == "IBM" and out[0].region == "United States"


@pytest.mark.parametrize("payload, exc", [
    ({"Note": "Thank you for using Alpha Vantage! Our standard API rate limit is 5 requests per minute"}, RateLimitError),
    ({"Information": "We have detected your API key as demo and our standard API rate limit is 25 requests per day."}, RateLimitError),
    ({"Error Message": "Invalid API call. Please retry or visit the documentation"}, SymbolNotFoundError),
    ({"Information": "The **demo** API key is invalid. Please claim your free API key"}, AuthenticationError),
    ({"Information": "This is a premium endpoint. Please subscribe"}, ProviderError),
    ({"unexpected": {}}, ProviderError),
])
def test_alphavantage_error_mapping(payload: dict[str, Any], exc: type[Exception]) -> None:
    session = FakeSession([FakeResponse(200, payload)])
    with pytest.raises(exc):
        AlphaVantageProvider("key", session=session).get_bars("IBM", "5min")


def test_alphavantage_unsupported_interval() -> None:
    with pytest.raises(ProviderError):
        AlphaVantageProvider("key", session=FakeSession()).get_bars("IBM", "1week")


def test_alphavantage_skips_malformed_rows() -> None:
    payload = load_fixture("alphavantage_intraday.json")
    payload["Time Series (5min)"]["2024-03-15 19:35:00"] = {"1. open": "x"}
    df = AlphaVantageProvider("key", session=FakeSession([FakeResponse(200, payload)])).get_bars("IBM", "5min", 10)
    assert len(df) == 4


@pytest.mark.parametrize("status, exc", [(429, RateLimitError), (401, AuthenticationError), (403, AuthenticationError), (500, ProviderError)])
def test_http_status_mapping(status: int, exc: type[Exception]) -> None:
    session = FakeSession([FakeResponse(status, text="oops")])
    with pytest.raises(exc):
        AlphaVantageProvider("key", session=session).get_bars("IBM")


def test_http_error_bodies_are_surfaced() -> None:
    session = FakeSession([FakeResponse(429, {"code": 429, "message": "You have run out of API credits", "status": "error"})])
    with pytest.raises(RateLimitError, match="run out of API credits"):
        TwelveDataProvider("key", session=session).get_bars("AAPL")
    session = FakeSession([FakeResponse(401, text="plain text denial")])
    with pytest.raises(AuthenticationError, match="plain text denial"):
        TwelveDataProvider("key", session=session).get_bars("AAPL")


def test_network_error_and_non_json() -> None:
    session = FakeSession(exc=requests.ConnectionError("boom"))
    with pytest.raises(ProviderError, match="network error"):
        AlphaVantageProvider("key", session=session).get_bars("IBM")
    session = FakeSession([FakeResponse(200, text="<html>")])
    with pytest.raises(ProviderError, match="not JSON"):
        AlphaVantageProvider("key", session=session).get_bars("IBM")


# ------------------------------------------------------------------ Twelve Data

def test_twelvedata_parsing() -> None:
    session = FakeSession([FakeResponse(200, load_fixture("twelvedata_time_series.json"))])
    p = TwelveDataProvider("key", session=session)
    df = p.get_bars("aapl", "60min", 10)
    assert len(df) == 3
    assert df.index.is_monotonic_increasing
    # 15:55 New York (EDT) -> 19:55 UTC
    assert df.index[-1] == pd.Timestamp("2024-03-15 19:55", tz="UTC")
    assert df["close"].iloc[-1] == 172.6
    params = session.calls[0][2]
    assert params["interval"] == "1h" and params["symbol"] == "AAPL" and params["outputsize"] == 10


def test_twelvedata_quote_and_search() -> None:
    quote = {"symbol": "AAPL", "close": "172.60", "previous_close": "171.00", "change": "1.6", "percent_change": "0.93",
             "volume": "100", "datetime": "2024-03-15 15:55:00", "exchange_timezone": "America/New_York"}
    session = FakeSession([FakeResponse(200, quote), FakeResponse(200, {"data": [{"symbol": "AAPL", "instrument_name": "Apple", "country": "US", "instrument_type": "Common Stock"}]})])
    p = TwelveDataProvider("key", session=session)
    q = p.get_quote("AAPL")
    assert q.price == 172.6 and q.change == 1.6 and q.timestamp.tzinfo is not None
    assert p.search("apple")[0].name == "Apple"


@pytest.mark.parametrize("payload, exc", [
    ({"code": 429, "message": "You have run out of API credits", "status": "error"}, RateLimitError),
    ({"code": 401, "message": "Invalid key", "status": "error"}, AuthenticationError),
    ({"code": 400, "message": "**symbol** not found", "status": "error"}, SymbolNotFoundError),
    ({"code": 500, "message": "server", "status": "error"}, ProviderError),
])
def test_twelvedata_error_mapping(payload: dict[str, Any], exc: type[Exception]) -> None:
    with pytest.raises(exc):
        TwelveDataProvider("key", session=FakeSession([FakeResponse(200, payload)])).get_bars("AAPL")


def test_twelvedata_unsupported_interval() -> None:
    with pytest.raises(ProviderError):
        TwelveDataProvider("key", session=FakeSession()).get_bars("AAPL", "2min")


# ------------------------------------------------------------------------ CSV

def test_csv_provider(tmp_path: Path) -> None:
    (tmp_path / "ABC.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2024-01-02,10,11,9,10.5,100\n"
        "2024-01-01,9,10,8,9.5,50\n"
        "bad,row,,,,\n"
    )
    p = CSVProvider(tmp_path)
    df = p.get_bars("abc", "1day", 10)
    assert len(df) == 2 and df["close"].tolist() == [9.5, 10.5]
    assert isinstance(df.index, pd.DatetimeIndex) and str(df.index.tz) == "UTC"
    with pytest.raises(SymbolNotFoundError):
        p.get_bars("NOPE")
    (tmp_path / "BAD.csv").write_text("a,b\n1,2\n")
    with pytest.raises(ProviderError):
        p.get_bars("BAD")
    (tmp_path / "NOCOLS.csv").write_text("time,open\n2024-01-01,1\n")
    with pytest.raises(ProviderError, match="missing column"):
        p.get_bars("NOCOLS")


# ----------------------------------------------------------------------- Demo

def test_demo_provider_is_deterministic_and_consistent() -> None:
    clock = lambda: datetime(2025, 6, 1, 12, 0, tzinfo=UTC)  # noqa: E731
    a = DemoProvider(clock=clock).get_bars("AAPL", "5min", 100)
    b = DemoProvider(clock=clock).get_bars("AAPL", "5min", 100)
    assert a.equals(b)
    assert len(a) == 100 and a.index[-1] == pd.Timestamp("2025-06-01 12:00", tz="UTC")
    assert (a["high"] >= a[["open", "close"]].max(axis=1)).all()
    assert (a["low"] <= a[["open", "close"]].min(axis=1)).all()
    later = DemoProvider(clock=lambda: datetime(2025, 6, 1, 12, 30, tzinfo=UTC)).get_bars("AAPL", "5min", 100)
    common = a.index.intersection(later.index)
    assert len(common) == 94
    assert np.allclose(a.loc[common], later.loc[common])
    other = DemoProvider(clock=clock).get_bars("MSFT", "5min", 100)
    assert not np.allclose(a["close"], other["close"])
    with pytest.raises(ProviderError):
        DemoProvider(clock=clock).get_bars("AAPL", "2min")


def test_demo_provider_all_intervals_have_sane_prices() -> None:
    clock = lambda: datetime(2025, 6, 1, 12, 0, tzinfo=UTC)  # noqa: E731
    for interval in ("1min", "5min", "15min", "30min", "60min", "1day"):
        df = DemoProvider(clock=clock).get_bars("TSLA", interval, 300)
        assert len(df) == 300
        assert df["close"].between(1, 10_000).all()


def test_quote_from_bars(ohlcv: pd.DataFrame) -> None:
    q = quote_from_bars("X", ohlcv)
    assert q.price == ohlcv["close"].iloc[-1]
    assert q.previous_close == ohlcv["close"].iloc[-2]
    with pytest.raises(SymbolNotFoundError):
        quote_from_bars("X", ohlcv.iloc[0:0])


def test_default_get_quote_uses_bars() -> None:
    clock = lambda: datetime(2025, 6, 1, 12, 0, tzinfo=UTC)  # noqa: E731
    q = DemoProvider(clock=clock).get_quote("AAPL")
    assert q.symbol == "AAPL" and q.price > 0


def test_create_provider_factory(tmp_path: Path) -> None:
    assert create_provider("demo").name == "demo"
    assert create_provider("csv", csv_directory=tmp_path).name == "csv"
    assert create_provider("alphavantage", "k").name == "alphavantage"
    assert create_provider("twelvedata", "k").name == "twelvedata"
    with pytest.raises(ProviderError):
        create_provider("nope")
