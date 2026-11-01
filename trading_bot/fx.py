"""Foreign-exchange rates.

Providers implement :class:`FxProvider`; :class:`FxService` adds caching, stale
fallback and conversion helpers.  The default provider is Frankfurter
(https://www.frankfurter.app - European Central Bank reference rates, free, no API
key, updated once per working day around 16:00 CET); ``demo`` produces
deterministic synthetic rates for offline use and tests.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from .config import FxConfig

logger = logging.getLogger(__name__)


class FxError(RuntimeError):
    pass


@dataclass
class RateTable:
    base: str
    date: str                       # ISO date of the rates
    rates: dict[str, float]         # quote currency -> units per 1 base
    source: str
    fetched_at: float = field(default_factory=time.time)
    stale: bool = False
    error: str | None = None

    def rate(self, quote: str) -> float:
        quote = quote.upper()
        if quote == self.base:
            return 1.0
        try:
            return self.rates[quote]
        except KeyError as exc:
            raise FxError(f"No rate for {quote} against {self.base}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {"base": self.base, "date": self.date, "rates": dict(sorted(self.rates.items())), "source": self.source,
                "fetched_at": self.fetched_at, "stale": self.stale, "error": self.error}


class FxProvider(ABC):
    name = "fx"

    @abstractmethod
    def latest(self, base: str, quotes: list[str]) -> RateTable: ...

    @abstractmethod
    def history(self, base: str, quote: str, start: date, end: date) -> list[tuple[str, float]]: ...

    @abstractmethod
    def currencies(self) -> dict[str, str]: ...

    def close(self) -> None:  # noqa: B027 - optional hook for providers that own network clients
        return None


# ---------------------------------------------------------------------------
# Frankfurter (ECB)
# ---------------------------------------------------------------------------


class FrankfurterProvider(FxProvider):
    name = "frankfurter"

    def __init__(self, base_url: str = "https://api.frankfurter.app", client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=10.0)
        self._owns_client = client is None

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        try:
            response = self._client.get(self.base_url + path, params=params)
        except httpx.HTTPError as exc:
            raise FxError(f"frankfurter: network error: {exc}") from exc
        if response.status_code == 404:
            raise FxError("frankfurter: unknown currency")
        if response.status_code >= 400:
            raise FxError(f"frankfurter: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise FxError("frankfurter: invalid JSON") from exc

    def latest(self, base: str, quotes: list[str]) -> RateTable:
        params = {"from": base.upper()}
        wanted = [q.upper() for q in quotes if q.upper() != base.upper()]
        if wanted:
            params["to"] = ",".join(wanted)
        data = self._get("/latest", params)
        rates = {k.upper(): float(v) for k, v in (data.get("rates") or {}).items()}
        if not rates:
            raise FxError("frankfurter: empty rate table")
        return RateTable(base.upper(), str(data.get("date", "")), rates, self.name)

    def history(self, base: str, quote: str, start: date, end: date) -> list[tuple[str, float]]:
        data = self._get(f"/{start.isoformat()}..{end.isoformat()}", {"from": base.upper(), "to": quote.upper()})
        series = data.get("rates") or {}
        points = [(day, float(values[quote.upper()])) for day, values in sorted(series.items()) if quote.upper() in values]
        return points

    def currencies(self) -> dict[str, str]:
        data = self._get("/currencies")
        return {str(k).upper(): str(v) for k, v in data.items()}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


# ---------------------------------------------------------------------------
# Demo provider (deterministic, offline)
# ---------------------------------------------------------------------------

DEMO_CURRENCIES: dict[str, tuple[str, float]] = {  # code -> (name, units per USD)
    "USD": ("US Dollar", 1.0), "EUR": ("Euro", 0.92), "GBP": ("British Pound", 0.79), "JPY": ("Japanese Yen", 149.5),
    "CHF": ("Swiss Franc", 0.88), "CAD": ("Canadian Dollar", 1.36), "AUD": ("Australian Dollar", 1.52),
    "CNY": ("Chinese Renminbi Yuan", 7.24), "SEK": ("Swedish Krona", 10.6), "NOK": ("Norwegian Krone", 10.8),
    "DKK": ("Danish Krone", 6.87), "PLN": ("Polish Zloty", 4.0), "CZK": ("Czech Koruna", 23.2),
    "HUF": ("Hungarian Forint", 360.0), "MXN": ("Mexican Peso", 17.1), "BRL": ("Brazilian Real", 4.97),
    "INR": ("Indian Rupee", 83.2), "KRW": ("South Korean Won", 1330.0), "SGD": ("Singapore Dollar", 1.34),
    "HKD": ("Hong Kong Dollar", 7.82), "NZD": ("New Zealand Dollar", 1.64), "ZAR": ("South African Rand", 18.7),
    "TRY": ("Turkish Lira", 31.5), "ILS": ("Israeli New Shekel", 3.7), "THB": ("Thai Baht", 35.8),
    "IDR": ("Indonesian Rupiah", 15700.0), "MYR": ("Malaysian Ringgit", 4.72), "PHP": ("Philippine Peso", 56.0),
    "RON": ("Romanian Leu", 4.57), "BGN": ("Bulgarian Lev", 1.80), "ISK": ("Icelandic Króna", 137.0),
}


class DemoFxProvider(FxProvider):
    """Synthetic but plausible rates: a slow cycle plus deterministic daily noise per currency."""

    name = "demo"

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _usd_rate(code: str, day: date) -> float:
        if code == "USD":
            return 1.0
        anchor = DEMO_CURRENCIES[code][1]
        ordinal = day.toordinal()
        cycle = 0.03 * math.sin(ordinal / 23.0 + len(code)) + 0.015 * math.sin(ordinal / 7.0)
        digest = hashlib.sha256(f"{code}:{ordinal}".encode()).digest()
        noise = (int.from_bytes(digest[:4], "big") / 2**32 - 0.5) * 0.006
        return anchor * (1 + cycle + noise)

    def _rate(self, base: str, quote: str, day: date) -> float:
        base, quote = base.upper(), quote.upper()
        for code in (base, quote):
            if code not in DEMO_CURRENCIES:
                raise FxError(f"demo: unknown currency {code}")
        return self._usd_rate(quote, day) / self._usd_rate(base, day)

    def _today(self) -> date:
        return self._clock().date()

    def latest(self, base: str, quotes: list[str]) -> RateTable:
        day = self._today()
        wanted = [q.upper() for q in quotes if q.upper() != base.upper()] or [c for c in DEMO_CURRENCIES if c != base.upper()]
        return RateTable(base.upper(), day.isoformat(), {q: round(self._rate(base, q, day), 6) for q in wanted}, self.name)

    def history(self, base: str, quote: str, start: date, end: date) -> list[tuple[str, float]]:
        points = []
        day = start
        while day <= end:
            if day.weekday() < 5:  # ECB publishes on working days only
                points.append((day.isoformat(), round(self._rate(base, quote, day), 6)))
            day += timedelta(days=1)
        return points

    def currencies(self) -> dict[str, str]:
        return {code: name for code, (name, _) in DEMO_CURRENCIES.items()}


def create_fx_provider(name: str, client: httpx.Client | None = None) -> FxProvider:
    if name == "frankfurter":
        return FrankfurterProvider(client=client)
    if name == "demo":
        return DemoFxProvider()
    raise FxError(f"Unknown FX provider {name!r}")


# ---------------------------------------------------------------------------
# Service: cache + conversion
# ---------------------------------------------------------------------------


class FxService:
    def __init__(self, provider: FxProvider, cfg: FxConfig, clock: Callable[[], float] = time.time) -> None:
        self.provider = provider
        self.cfg = cfg
        self._clock = clock
        self._lock = threading.RLock()
        self._latest: dict[str, RateTable] = {}
        self._history: dict[tuple[str, str, int], tuple[float, list[dict[str, Any]]]] = {}
        self._currencies: tuple[float, dict[str, str]] | None = None

    @property
    def base_currency(self) -> str:
        return self.cfg.base_currency.upper()

    def latest(self, base: str | None = None, quotes: list[str] | None = None, max_age: float | None = None) -> RateTable:
        """Rate table for ``base`` (cached for ``fx.cache_ttl_seconds``; stale data is served on errors)."""
        base = (base or self.base_currency).upper()
        wanted = sorted({q.upper() for q in (quotes or self.cfg.quote_currencies)} | {self.base_currency}) if quotes is None \
            else [q.upper() for q in quotes]
        ttl = self.cfg.cache_ttl_seconds if max_age is None else max_age
        with self._lock:
            cached = self._latest.get(base)
            if cached is not None and self._clock() - cached.fetched_at < ttl and set(wanted) - {base} <= set(cached.rates):
                return cached
        try:
            table = self.provider.latest(base, wanted)
            table.fetched_at = self._clock()
        except FxError as exc:
            logger.warning("FX rates for %s unavailable: %s", base, exc)
            with self._lock:
                cached = self._latest.get(base)
            if cached is None:
                raise
            cached.stale, cached.error = True, str(exc)
            return cached
        with self._lock:
            self._latest[base] = table
        return table

    def history(self, base: str, quote: str, days: int = 90) -> list[dict[str, Any]]:
        base, quote = base.upper(), quote.upper()
        days = max(2, min(int(days), 3660))
        key = (base, quote, days)
        with self._lock:
            cached = self._history.get(key)
            if cached is not None and self._clock() - cached[0] < self.cfg.cache_ttl_seconds:
                return cached[1]
        end = datetime.fromtimestamp(self._clock(), UTC).date()
        start = end - timedelta(days=days)
        if base == quote:
            points = [{"date": start.isoformat(), "rate": 1.0}, {"date": end.isoformat(), "rate": 1.0}]
        else:
            points = [{"date": d, "rate": r} for d, r in self.provider.history(base, quote, start, end)]
        with self._lock:
            self._history[key] = (self._clock(), points)
        return points

    def currencies(self) -> dict[str, str]:
        with self._lock:
            if self._currencies and self._clock() - self._currencies[0] < 86400:
                return self._currencies[1]
        try:
            names = self.provider.currencies()
        except FxError as exc:
            logger.warning("Currency list unavailable: %s", exc)
            with self._lock:
                if self._currencies:
                    return self._currencies[1]
            names = {code: name for code, (name, _) in DEMO_CURRENCIES.items()}
        with self._lock:
            self._currencies = (self._clock(), names)
        return names

    def rate(self, from_currency: str, to_currency: str) -> float:
        """Units of ``to_currency`` per one ``from_currency``."""
        src, dst = from_currency.upper(), to_currency.upper()
        if src == dst:
            return 1.0
        table = self.latest(src, [dst])
        return table.rate(dst)

    def convert(self, amount: float, from_currency: str, to_currency: str) -> dict[str, Any]:
        rate = self.rate(from_currency, to_currency)
        table = self.latest(from_currency.upper(), [to_currency.upper()]) if from_currency.upper() != to_currency.upper() else None
        return {"amount": float(amount), "from": from_currency.upper(), "to": to_currency.upper(), "rate": rate,
                "result": round(float(amount) * rate, 6), "date": table.date if table else None,
                "source": table.source if table else "identity"}

    def close(self) -> None:
        self.provider.close()


__all__ = ["DEMO_CURRENCIES", "DemoFxProvider", "FrankfurterProvider", "FxError", "FxProvider", "FxService", "RateTable",
           "create_fx_provider"]
