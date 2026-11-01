"""Exchange rates: demo and Frankfurter providers, the caching service and conversions."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from trading_bot.config import FxConfig
from trading_bot.fx import DEMO_CURRENCIES, DemoFxProvider, FrankfurterProvider, FxError, FxService, create_fx_provider


def test_demo_provider_is_deterministic_and_consistent() -> None:
    clock = lambda: datetime(2024, 3, 4, 12, tzinfo=UTC)  # noqa: E731
    provider = DemoFxProvider(clock=clock)
    table = provider.latest("USD", ["EUR", "JPY"])
    again = DemoFxProvider(clock=clock).latest("USD", ["EUR", "JPY"])
    assert table.rates == again.rates and table.date == "2024-03-04" and set(table.rates) == {"EUR", "JPY"}
    assert 0.85 < table.rates["EUR"] < 1.0 and 130 < table.rates["JPY"] < 170  # near the anchors
    # Cross rates are consistent: EUR->JPY equals (USD->JPY) / (USD->EUR).
    eur = provider.latest("EUR", ["JPY"])
    assert eur.rates["JPY"] == pytest.approx(table.rates["JPY"] / table.rates["EUR"], rel=1e-4)
    assert provider.latest("USD", []).rates.keys() == set(DEMO_CURRENCIES) - {"USD"}
    history = provider.history("USD", "EUR", date(2024, 2, 1), date(2024, 3, 4))
    assert len(history) == 23 and all(date.fromisoformat(d).weekday() < 5 for d, _ in history)  # working days only
    assert history[-1][1] == pytest.approx(table.rates["EUR"])
    assert provider.currencies()["EUR"] == "Euro"
    with pytest.raises(FxError, match="unknown currency"):
        provider.latest("USD", ["XXX"])


def frankfurter_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    params = dict(request.url.params)
    if path == "/latest":
        if params.get("from") == "XXX":
            return httpx.Response(404, json={"message": "not found"})
        rates = {"EUR": 0.92, "GBP": 0.79}
        if "to" in params:
            rates = {k: v for k, v in rates.items() if k in params["to"].split(",")}
        return httpx.Response(200, json={"amount": 1.0, "base": params.get("from", "EUR"), "date": "2024-03-01", "rates": rates})
    if path == "/currencies":
        return httpx.Response(200, json={"EUR": "Euro", "USD": "United States Dollar", "GBP": "British Pound"})
    if ".." in path:
        return httpx.Response(200, json={"amount": 1.0, "base": "USD", "start_date": "2024-02-28", "end_date": "2024-03-01",
                                         "rates": {"2024-03-01": {"EUR": 0.92}, "2024-02-28": {"EUR": 0.91}, "2024-02-29": {}}})
    return httpx.Response(500, text="boom")


def test_frankfurter_provider() -> None:
    provider = FrankfurterProvider(client=httpx.Client(transport=httpx.MockTransport(frankfurter_handler)))
    table = provider.latest("usd", ["eur", "gbp", "usd"])
    assert table.base == "USD" and table.date == "2024-03-01" and table.rates == {"EUR": 0.92, "GBP": 0.79}
    only = provider.latest("USD", ["EUR"])
    assert only.rates == {"EUR": 0.92}
    history = provider.history("USD", "EUR", date(2024, 2, 28), date(2024, 3, 1))
    assert history == [("2024-02-28", 0.91), ("2024-03-01", 0.92)]  # sorted, days without the quote skipped
    assert provider.currencies()["USD"] == "United States Dollar"
    with pytest.raises(FxError, match="unknown currency"):
        provider.latest("XXX", ["EUR"])

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/latest":
            return httpx.Response(200, json={"rates": {}})
        if request.url.path == "/currencies":
            return httpx.Response(200, text="not json")
        raise httpx.ConnectError("offline")

    bad = FrankfurterProvider(client=httpx.Client(transport=httpx.MockTransport(broken)))
    with pytest.raises(FxError, match="empty rate table"):
        bad.latest("USD", ["EUR"])
    with pytest.raises(FxError, match="invalid JSON"):
        bad.currencies()
    with pytest.raises(FxError, match="network error"):
        bad.history("USD", "EUR", date(2024, 1, 1), date(2024, 1, 2))
    provider.close()
    assert isinstance(create_fx_provider("frankfurter"), FrankfurterProvider)
    assert isinstance(create_fx_provider("demo"), DemoFxProvider)
    with pytest.raises(FxError):
        create_fx_provider("nope")


class CountingProvider(DemoFxProvider):
    def __init__(self) -> None:
        super().__init__(clock=lambda: datetime(2024, 3, 4, tzinfo=UTC))
        self.calls = 0
        self.fail = False

    def latest(self, base: str, quotes: list[str]):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.fail:
            raise FxError("service down")
        return super().latest(base, quotes)


def test_fx_service_caching_stale_fallback_and_convert() -> None:
    now = [datetime(2024, 3, 4, 12, tzinfo=UTC).timestamp()]
    provider = CountingProvider()
    service = FxService(provider, FxConfig(provider="demo", base_currency="USD", quote_currencies=["EUR", "GBP"], cache_ttl_seconds=60),
                        clock=lambda: now[0])
    first = service.latest()
    assert first.base == "USD" and set(first.rates) == {"EUR", "GBP"} and provider.calls == 1
    assert service.latest() is first and provider.calls == 1          # cached
    assert service.latest("USD", ["EUR"]) is first                    # subset served from cache
    service.latest("USD", ["JPY"])                                    # new quote -> refetch
    assert provider.calls == 2
    now[0] += 61
    service.latest()
    assert provider.calls == 3                                        # TTL expired
    provider.fail = True
    now[0] += 61
    stale = service.latest()
    assert stale.stale is True and stale.error == "service down" and stale.rates  # old table kept
    with pytest.raises(FxError):
        service.latest("EUR")                                         # nothing cached for this base
    provider.fail = False
    rate = service.rate("EUR", "USD")
    assert rate == pytest.approx(1 / service.rate("USD", "EUR"), rel=1e-3)
    assert service.rate("USD", "usd") == 1.0
    conversion = service.convert(100, "EUR", "USD")
    assert conversion["result"] == pytest.approx(100 * rate) and conversion["source"] == "demo" and conversion["date"] == "2024-03-04"
    assert service.convert(5, "USD", "USD") == {"amount": 5.0, "from": "USD", "to": "USD", "rate": 1.0, "result": 5.0,
                                                "date": None, "source": "identity"}
    history = service.history("USD", "EUR", 10)
    assert len(history) == 7 and history[0]["date"] == "2024-02-23" and history[-1]["date"] == "2024-03-04"
    assert service.history("USD", "EUR", 10) is history  # cached
    assert service.history("USD", "USD", 5) == [{"date": "2024-02-28", "rate": 1.0}, {"date": "2024-03-04", "rate": 1.0}]
    assert service.currencies()["EUR"] == "Euro" and service.currencies() is service.currencies()
    json.dumps(stale.to_dict())
    service.close()
