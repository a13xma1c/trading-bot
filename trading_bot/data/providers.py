"""Market-data providers.

All providers return OHLCV data as a :class:`pandas.DataFrame` indexed by a
timezone-aware (UTC) :class:`pandas.DatetimeIndex` with the columns
``open, high, low, close, volume`` sorted oldest to newest.
"""

from __future__ import annotations

import csv
import logging
import math
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd
import requests

from ..indicators import validate_ohlcv

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
USER_AGENT = "trading-bot/1.0 (+https://github.com/)"


class HttpResponse(Protocol):
    """The subset of :class:`requests.Response` the providers rely on."""

    @property
    def status_code(self) -> int: ...

    @property
    def text(self) -> str: ...

    def json(self) -> Any: ...


class HttpSession(Protocol):
    """The subset of :class:`requests.Session` the providers rely on (easy to fake in tests)."""

    @property
    def headers(self) -> Any: ...

    def get(self, url: str, *, params: Any = ..., timeout: float | None = ...) -> HttpResponse: ...

    def close(self) -> None: ...


class ProviderError(RuntimeError):
    """Base class for provider failures."""


class RateLimitError(ProviderError):
    """The provider rejected the request because a quota was exceeded."""


class SymbolNotFoundError(ProviderError):
    """The provider does not know the requested symbol."""


class AuthenticationError(ProviderError):
    """The API key is missing or invalid."""


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    change: float
    change_pct: float
    volume: float
    timestamp: datetime
    previous_close: float | None = None


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    name: str
    region: str = ""
    type: str = ""


class MarketDataProvider(ABC):
    """Interface implemented by every data source."""

    name: str = "base"
    requires_api_key: bool = False

    @abstractmethod
    def get_bars(self, symbol: str, interval: str = "5min", limit: int = 300) -> pd.DataFrame:
        """Return up to ``limit`` most recent bars."""

    def get_quote(self, symbol: str) -> Quote:
        """Return the latest quote; default derives it from the last two bars."""
        df = self.get_bars(symbol, "1day", 2)
        return quote_from_bars(symbol, df)

    def search(self, query: str) -> list[SymbolInfo]:
        return []

    def close(self) -> None:  # noqa: B027 - optional hook for providers holding resources
        """Release resources (HTTP sessions); no-op by default."""


def quote_from_bars(symbol: str, df: pd.DataFrame) -> Quote:
    if df.empty:
        raise SymbolNotFoundError(f"No data for {symbol}")
    last = df.iloc[-1]
    prev_close = float(df["close"].iloc[-2]) if len(df) > 1 else float(last["open"])
    change = float(last["close"]) - prev_close
    pct = (change / prev_close * 100.0) if prev_close else 0.0
    return Quote(symbol, float(last["close"]), change, pct, float(last["volume"]),
                 df.index[-1].to_pydatetime(), prev_close)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class _HttpProvider(MarketDataProvider):
    base_url: str = ""

    def __init__(self, api_key: str = "", session: HttpSession | None = None, timeout: float = DEFAULT_TIMEOUT):
        # A missing key is reported when a request is made (so the app can start and let the
        # user enter the key in the UI), not at construction time.
        self.api_key = api_key
        self.timeout = timeout
        self.session: HttpSession
        self.session = session if session is not None else requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)

    def _get_json(self, params: dict[str, Any], path: str = "") -> Any:
        if self.requires_api_key and not self.api_key:
            raise AuthenticationError(f"{self.name} requires an API key - enter it under Settings "
                                      "(or set data.api_key / TRADING_BOT_API_KEY)")
        url = self.base_url + path
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProviderError(f"{self.name}: network error: {exc}") from exc
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            if resp.status_code == 429:
                raise RateLimitError(f"{self.name}: too many requests - {detail}")
            if resp.status_code in (401, 403):
                raise AuthenticationError(f"{self.name}: check your API key - {detail}")
            raise ProviderError(f"{self.name}: HTTP {resp.status_code}: {detail}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.name}: response was not JSON: {resp.text[:200]}") from exc

    def close(self) -> None:
        self.session.close()


def _error_detail(resp: HttpResponse) -> str:
    """Extract the provider's error message from an HTTP error response."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            for key in ("message", "Error Message", "Information", "Note", "error"):
                if body.get(key):
                    return str(body[key])[:200]
    except ValueError:
        pass
    return (resp.text or f"HTTP {resp.status_code}")[:200]


def _frame_from_records(records: list[dict[str, Any]], tz: str | None) -> pd.DataFrame:
    """Build a UTC-indexed OHLCV frame from parsed records."""
    if not records:
        return empty_frame()
    df = pd.DataFrame.from_records(records)
    times = pd.to_datetime(df.pop("time"), errors="coerce")
    df = df[times.notna().to_numpy()]
    index = pd.DatetimeIndex(times[times.notna()])
    if index.tz is None:
        index = index.tz_localize(tz or "UTC", ambiguous="NaT", nonexistent="shift_forward")
        keep = index.notna()
        df, index = df[keep], index[keep]
    df.index = index.tz_convert("UTC")
    df.index.name = "time"
    return validate_ohlcv(df)


def empty_frame() -> pd.DataFrame:
    df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
    df.index = pd.DatetimeIndex([], tz="UTC", name="time")
    return df


# ---------------------------------------------------------------------------
# Alpha Vantage
# ---------------------------------------------------------------------------


class AlphaVantageProvider(_HttpProvider):
    """https://www.alphavantage.co/documentation/"""

    name = "alphavantage"
    requires_api_key = True
    base_url = "https://www.alphavantage.co/query"
    INTRADAY = {"1min", "5min", "15min", "30min", "60min"}

    def get_bars(self, symbol: str, interval: str = "5min", limit: int = 300) -> pd.DataFrame:
        symbol = symbol.upper()
        if interval in self.INTRADAY:
            params = {"function": "TIME_SERIES_INTRADAY", "symbol": symbol, "interval": interval,
                      "outputsize": "compact" if limit <= 100 else "full", "apikey": self.api_key}
            key = f"Time Series ({interval})"
        elif interval == "1day":
            params = {"function": "TIME_SERIES_DAILY", "symbol": symbol,
                      "outputsize": "compact" if limit <= 100 else "full", "apikey": self.api_key}
            key = "Time Series (Daily)"
        else:
            raise ProviderError(f"Alpha Vantage does not support interval {interval!r}")
        data = self._get_json(params)
        self._raise_for_api_error(data, symbol)
        series = data.get(key)
        if not isinstance(series, dict):
            raise ProviderError(f"Alpha Vantage: unexpected response for {symbol}: {list(data)[:3]}")
        tz = data.get("Meta Data", {}).get("6. Time Zone") or data.get("Meta Data", {}).get("5. Time Zone") or "US/Eastern"
        records = []
        for ts, row in series.items():
            try:
                records.append({
                    "time": ts,
                    "open": float(row["1. open"]), "high": float(row["2. high"]),
                    "low": float(row["3. low"]), "close": float(row["4. close"]),
                    "volume": float(row["5. volume"]),
                })
            except (KeyError, ValueError) as exc:
                logger.debug("Skipping malformed bar %s: %s", ts, exc)
        df = _frame_from_records(records, tz)
        return df.tail(limit)

    def get_quote(self, symbol: str) -> Quote:
        data = self._get_json({"function": "GLOBAL_QUOTE", "symbol": symbol.upper(), "apikey": self.api_key})
        self._raise_for_api_error(data, symbol)
        q = data.get("Global Quote") or {}
        if not q:
            raise SymbolNotFoundError(f"Alpha Vantage: no quote for {symbol}")
        try:
            price = float(q["05. price"])
            prev = float(q.get("08. previous close", price))
            change = float(q.get("09. change", price - prev))
            pct = float(str(q.get("10. change percent", "0")).rstrip("%"))
            volume = float(q.get("06. volume", 0))
            day = pd.Timestamp(q.get("07. latest trading day", datetime.now(UTC).date())).tz_localize("UTC")
        except (KeyError, ValueError) as exc:
            raise ProviderError(f"Alpha Vantage: malformed quote for {symbol}: {exc}") from exc
        return Quote(symbol.upper(), price, change, pct, volume, day.to_pydatetime(), prev)

    def search(self, query: str) -> list[SymbolInfo]:
        data = self._get_json({"function": "SYMBOL_SEARCH", "keywords": query, "apikey": self.api_key})
        self._raise_for_api_error(data, query)
        out = []
        for m in data.get("bestMatches", []) or []:
            out.append(SymbolInfo(m.get("1. symbol", ""), m.get("2. name", ""), m.get("4. region", ""), m.get("3. type", "")))
        return out

    @staticmethod
    def _raise_for_api_error(data: Any, symbol: str) -> None:
        if not isinstance(data, dict):
            raise ProviderError("Alpha Vantage: unexpected response type")
        if "Error Message" in data:
            raise SymbolNotFoundError(f"Alpha Vantage: {data['Error Message']}")
        for key in ("Note", "Information"):
            if key in data:
                text = str(data[key])
                lowered = text.lower()
                if "api key" in lowered and "invalid" in lowered:
                    raise AuthenticationError(f"Alpha Vantage: {text}")
                if "premium" in lowered and "rate limit" not in lowered and "requests" not in lowered:
                    raise ProviderError(f"Alpha Vantage: {text}")
                raise RateLimitError(f"Alpha Vantage: {text}")


# ---------------------------------------------------------------------------
# Twelve Data
# ---------------------------------------------------------------------------


class TwelveDataProvider(_HttpProvider):
    """https://twelvedata.com/docs"""

    name = "twelvedata"
    requires_api_key = True
    base_url = "https://api.twelvedata.com"
    INTERVALS = {"1min": "1min", "5min": "5min", "15min": "15min", "30min": "30min", "60min": "1h", "1day": "1day"}

    def get_bars(self, symbol: str, interval: str = "5min", limit: int = 300) -> pd.DataFrame:
        if interval not in self.INTERVALS:
            raise ProviderError(f"Twelve Data does not support interval {interval!r}")
        data = self._get_json({
            "symbol": symbol.upper(), "interval": self.INTERVALS[interval],
            "outputsize": max(1, min(limit, 5000)), "apikey": self.api_key, "format": "JSON",
        }, "/time_series")
        self._raise_for_api_error(data, symbol)
        tz = (data.get("meta") or {}).get("exchange_timezone") or "UTC"
        records = []
        for row in data.get("values", []) or []:
            try:
                records.append({
                    "time": row["datetime"], "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]), "volume": float(row.get("volume", 0) or 0),
                })
            except (KeyError, ValueError) as exc:
                logger.debug("Skipping malformed bar %s: %s", row, exc)
        return _frame_from_records(records, tz).tail(limit)

    def get_quote(self, symbol: str) -> Quote:
        data = self._get_json({"symbol": symbol.upper(), "apikey": self.api_key}, "/quote")
        self._raise_for_api_error(data, symbol)
        try:
            price = float(data["close"])
            prev = float(data.get("previous_close", price))
            ts = pd.Timestamp(data.get("datetime"))
            tz = data.get("exchange_timezone") or "UTC"
            ts = ts.tz_localize(tz) if ts.tzinfo is None else ts
            return Quote(symbol.upper(), price, float(data.get("change", price - prev)),
                         float(data.get("percent_change", 0)), float(data.get("volume", 0) or 0),
                         ts.tz_convert("UTC").to_pydatetime(), prev)
        except (KeyError, ValueError) as exc:
            raise ProviderError(f"Twelve Data: malformed quote for {symbol}: {exc}") from exc

    def search(self, query: str) -> list[SymbolInfo]:
        data = self._get_json({"symbol": query, "apikey": self.api_key}, "/symbol_search")
        self._raise_for_api_error(data, query)
        return [SymbolInfo(m.get("symbol", ""), m.get("instrument_name", ""), m.get("country", ""),
                           m.get("instrument_type", "")) for m in data.get("data", []) or []]

    @staticmethod
    def _raise_for_api_error(data: Any, symbol: str) -> None:
        if not isinstance(data, dict):
            raise ProviderError("Twelve Data: unexpected response type")
        if data.get("status") == "error" or ("code" in data and "message" in data and "values" not in data):
            code = int(data.get("code", 0) or 0)
            message = str(data.get("message", "unknown error"))
            if code == 429:
                raise RateLimitError(f"Twelve Data: {message}")
            if code in (401, 403):
                raise AuthenticationError(f"Twelve Data: {message}")
            if code == 400 or "symbol" in message.lower():
                raise SymbolNotFoundError(f"Twelve Data: {message}")
            raise ProviderError(f"Twelve Data: {message}")


# ---------------------------------------------------------------------------
# Offline providers
# ---------------------------------------------------------------------------


class CSVProvider(MarketDataProvider):
    """Reads ``<directory>/<SYMBOL>.csv`` files with ``time,open,high,low,close,volume`` columns.

    Useful for backtesting on downloaded history or for running the bot against
    files refreshed by an external process.
    """

    name = "csv"

    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory)

    def get_bars(self, symbol: str, interval: str = "5min", limit: int = 300) -> pd.DataFrame:
        path = self.directory / f"{symbol.upper()}.csv"
        if not path.exists():
            raise SymbolNotFoundError(f"No CSV file for {symbol} at {path}")
        records = []
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise ProviderError(f"{path} is empty")
            fields = {name.lower().strip(): name for name in reader.fieldnames}
            time_col = next((fields[c] for c in ("time", "timestamp", "date", "datetime") if c in fields), None)
            if time_col is None:
                raise ProviderError(f"{path} has no time/date column")
            try:
                cols = {c: fields[c] for c in ("open", "high", "low", "close", "volume")}
            except KeyError as exc:
                raise ProviderError(f"{path} is missing column {exc}") from exc
            for row in reader:
                try:
                    records.append({"time": row[time_col], **{c: float(row[src] or 0) for c, src in cols.items()}})
                except ValueError:
                    continue
        return _frame_from_records(records, "UTC").tail(limit)


class DemoProvider(MarketDataProvider):
    """Deterministic synthetic market data for demos and tests (no network).

    Every bar is a pure function of ``(symbol, time slot)`` so successive calls
    return identical history and new bars simply appear as time moves on.
    """

    name = "demo"
    _SECONDS = {"1min": 60, "5min": 300, "15min": 900, "30min": 1800, "60min": 3600, "1day": 86400}
    _ORIGIN = 1_704_067_200  # 2024-01-01T00:00:00Z

    def __init__(self, seed: int = 42, clock=None):
        self.seed = seed
        self._clock = clock or (lambda: datetime.now(UTC))
        self.calls = 0

    @staticmethod
    def _hash(values: np.ndarray, salt: int) -> np.ndarray:
        """SplitMix64-style hash -> uniform floats in [0, 1)."""
        x = (values.astype(np.uint64) + np.uint64(salt)) * np.uint64(0x9E3779B97F4A7C15)
        x ^= x >> np.uint64(30)
        x *= np.uint64(0xBF58476D1CE4E5B9)
        x ^= x >> np.uint64(27)
        x *= np.uint64(0x94D049BB133111EB)
        x ^= x >> np.uint64(31)
        return (x >> np.uint64(11)).astype(np.float64) / float(1 << 53)

    def _normals(self, slots: np.ndarray, salt: int) -> np.ndarray:
        u1 = np.clip(self._hash(slots, salt), 1e-12, 1.0)
        u2 = self._hash(slots, salt + 7919)
        return np.sqrt(-2.0 * np.log(u1)) * np.cos(2.0 * np.pi * u2)

    def get_bars(self, symbol: str, interval: str = "5min", limit: int = 300) -> pd.DataFrame:
        self.calls += 1
        step = self._SECONDS.get(interval)
        if step is None:
            raise ProviderError(f"Unsupported interval {interval!r}")
        now = self._clock()
        end_slot = int(now.timestamp() // step)
        origin_slot = min(self._ORIGIN // step, end_slot - max(limit, 1))
        symbol_salt = self.seed * 1_000_003 + sum(ord(c) * 31 ** i for i, c in enumerate(symbol.upper())) * step
        base = 20.0 + (sum(ord(c) for c in symbol.upper()) % 400)

        all_slots = np.arange(origin_slot, end_slot + 1, dtype=np.int64)
        vol = min(0.002 * math.sqrt(step / 60.0), 0.02)
        # Ornstein-Uhlenbeck process: x_t = (1 - theta) x_{t-1} + vol * eps_t, implemented with an
        # exponentially weighted mean.  Mean reversion keeps prices near ``base`` over long histories.
        theta = step / 6_000_000.0  # ~70 day reversion horizon regardless of bar size
        noise = self._normals(all_slots, symbol_salt) * (vol / theta)
        noise[0] = 0.0
        log_price = np.log(base) + pd.Series(noise).ewm(alpha=theta, adjust=False).mean().to_numpy()
        window = slice(max(0, len(all_slots) - limit), len(all_slots))
        slots = all_slots[window]
        closes = np.exp(log_price[window])
        prev_close = np.exp(log_price[window.start - 1]) if window.start > 0 else closes[0]
        opens = np.concatenate([[prev_close], closes[:-1]]) * (1 + self._normals(slots, symbol_salt + 1) * 0.001)
        highs = np.maximum(opens, closes) * (1 + np.abs(self._normals(slots, symbol_salt + 2)) * 0.003)
        lows = np.minimum(opens, closes) * (1 - np.abs(self._normals(slots, symbol_salt + 3)) * 0.003)
        volumes = np.floor(50_000 + 450_000 * self._hash(slots, symbol_salt + 4))
        times = pd.to_datetime(slots * step, unit="s", utc=True)
        df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
                          index=pd.DatetimeIndex(times, name="time"))
        return df


def create_provider(provider: str, api_key: str = "", csv_directory: str | os.PathLike[str] = "data",
                    **kwargs: Any) -> MarketDataProvider:
    """Factory used by the engine and CLI."""
    provider = provider.lower()
    if provider == "alphavantage":
        return AlphaVantageProvider(api_key, **kwargs)
    if provider == "twelvedata":
        return TwelveDataProvider(api_key, **kwargs)
    if provider == "csv":
        return CSVProvider(csv_directory)
    if provider == "demo":
        return DemoProvider()
    raise ProviderError(f"Unknown provider {provider!r}")
