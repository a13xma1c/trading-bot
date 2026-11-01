"""Alpaca broker adapter against a fake Alpaca API (httpx.MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from trading_bot.broker import BrokerError
from trading_bot.brokers import BROKER_PROVIDERS, create_broker
from trading_bot.brokers.alpaca import LIVE_URL, PAPER_URL, AlpacaBroker


class FakeAlpaca:
    """Minimal stateful stand-in for the paper trading API."""

    def __init__(self) -> None:
        self.cash = 10_000.0
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.fill_after_polls = 0     # how many GETs an order stays "accepted" before filling
        self.reject_next = False
        self.key = "key"
        self._polls: dict[str, int] = {}
        self._seq = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.headers.get("APCA-API-KEY-ID") != self.key or request.headers.get("APCA-API-SECRET-KEY") != "secret":
            return httpx.Response(401, json={"message": "unauthorized"})
        path = request.url.path
        if path == "/v2/account":
            return httpx.Response(200, json={"account_number": "PA123", "status": "ACTIVE", "currency": "USD",
                                             "cash": str(self.cash), "equity": str(self.cash + 500), "buying_power": str(self.cash * 2),
                                             "trading_blocked": False})
        if path == "/v2/positions" and request.method == "GET":
            return httpx.Response(200, json=list(self.positions.values()))
        if path.startswith("/v2/positions/") and request.method == "DELETE":
            symbol = path.rsplit("/", 1)[1]
            if symbol not in self.positions:
                return httpx.Response(404, json={"message": "position does not exist"})
            qty = float(self.positions.pop(symbol)["qty"])
            return httpx.Response(200, json=self._order(symbol, qty, "sell", "market", filled=True, price=95.0))
        if path == "/v2/orders" and request.method == "POST":
            assert body is not None
            if self.reject_next:
                self.reject_next = False
                return httpx.Response(422, json={"message": "insufficient buying power"})
            filled = body["type"] == "market" and self.fill_after_polls == 0
            created = self._order(body["symbol"], float(body["qty"]), body["side"], body["type"], filled=filled,
                                  price=101.5, limit_price=body.get("limit_price"))
            if body["type"] == "market" and filled:
                self._apply(created)
            return httpx.Response(200, json=created)
        if path == "/v2/orders" and request.method == "GET":
            return httpx.Response(200, json=[o for o in self.orders.values() if o["status"] in ("new", "accepted")])
        if path.startswith("/v2/orders/") and request.method == "GET":
            polled = self.orders[path.rsplit("/", 1)[1]]
            self._polls[polled["id"]] = self._polls.get(polled["id"], 0) + 1
            if polled["status"] != "filled" and self._polls[polled["id"]] >= self.fill_after_polls:
                polled.update(status="filled", filled_qty=polled["qty"], filled_avg_price="102.25", filled_at="2024-03-04T15:00:00Z")
                self._apply(polled)
            return httpx.Response(200, json=polled)
        if path.startswith("/v2/orders/") and request.method == "DELETE":
            existing = self.orders.get(path.rsplit("/", 1)[1])
            if existing is None:
                return httpx.Response(404, json={"message": "order not found"})
            existing["status"] = "canceled"
            return httpx.Response(204)
        return httpx.Response(500, text="unexpected")

    def _order(self, symbol: str, qty: float, side: str, kind: str, *, filled: bool, price: float,
               limit_price: str | None = None) -> dict[str, Any]:
        self._seq += 1
        order: dict[str, Any] = {"id": f"ord-{self._seq}", "symbol": symbol, "qty": str(qty), "side": side, "type": kind,
                 "status": "filled" if filled else "accepted", "filled_qty": str(qty) if filled else "0",
                 "filled_avg_price": str(price) if filled else None, "submitted_at": "2024-03-04T14:59:00Z",
                 "filled_at": "2024-03-04T15:00:00Z" if filled else None, "limit_price": limit_price}
        self.orders[str(order["id"])] = order
        return order

    def _apply(self, order: dict[str, Any]) -> None:
        qty, price = float(order["qty"]), float(order["filled_avg_price"])
        if order["side"] == "buy":
            self.cash -= qty * price
            self.positions[order["symbol"]] = {"symbol": order["symbol"], "qty": str(qty), "avg_entry_price": str(price),
                                               "side": "long"}
        else:
            self.cash += qty * price
            self.positions.pop(order["symbol"], None)


@pytest.fixture
def alpaca() -> tuple[AlpacaBroker, FakeAlpaca]:
    fake = FakeAlpaca()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    broker = AlpacaBroker("key", "secret", paper=True, client=client, fill_timeout=5.0, poll_interval=0.0, sleep=lambda _: None)
    return broker, fake


def test_account_positions_and_describe(alpaca: tuple[AlpacaBroker, FakeAlpaca]) -> None:
    broker, fake = alpaca
    assert broker.base_url == PAPER_URL and broker.is_paper and not broker.supports_deposits
    assert broker.describe() == {"name": "alpaca", "paper": True, "label": "Alpaca (paper)"}
    assert AlpacaBroker("k", "s", paper=False, client=httpx.Client()).describe()["label"] == "Alpaca (LIVE)"
    assert AlpacaBroker("k", "s", paper=False, client=httpx.Client()).base_url == LIVE_URL
    status = broker.test_connection()
    assert status["account_number"] == "PA123" and status["cash"] == 10_000 and status["equity"] == 10_500
    assert broker.cash() == 10_000 and broker.equity({}) == 10_500
    assert broker.positions() == {}
    fake.positions["AAPL"] = {"symbol": "AAPL", "qty": "3", "avg_entry_price": "150.5", "side": "long"}
    fake.positions["TSLA"] = {"symbol": "TSLA", "qty": "2", "avg_entry_price": "200", "side": "short"}
    positions = broker.positions()
    assert positions["AAPL"].quantity == 3 and positions["AAPL"].avg_price == 150.5
    assert positions["TSLA"].quantity == -2  # shorts are negative
    assert broker.position("aapl") is not None


def test_market_order_fills_immediately_and_bracket_payload(alpaca: tuple[AlpacaBroker, FakeAlpaca]) -> None:
    broker, fake = alpaca
    fill = broker.submit_market_order("AAPL", "BUY", 4, 100.0, strategy="MA", stop_loss=95.0, take_profit=110.0)
    assert fill.symbol == "AAPL" and fill.side == "BUY" and fill.quantity == 4 and fill.price == 101.5
    assert fill.strategy == "MA" and fill.id == "ord-1"[:12] and fill.timestamp.isoformat().startswith("2024-03-04T15:00")
    method, path, body = fake.requests[-1]
    assert (method, path) == ("POST", "/v2/orders")
    assert body == {"symbol": "AAPL", "qty": "4", "side": "buy", "type": "market", "time_in_force": "day",
                    "order_class": "bracket", "stop_loss": {"stop_price": "95.00"}, "take_profit": {"limit_price": "110.00"}}
    # Selling reports the realized P&L against the broker's average entry price.
    sell = broker.submit_market_order("AAPL", "SELL", 4, 100.0)
    assert sell.side == "SELL" and sell.realized_pnl == pytest.approx((101.5 - 101.5) * 4)
    assert broker.positions() == {}
    # Fractional quantities are sent with four decimals.
    broker.submit_market_order("AAPL", "BUY", 1.5, 100.0)
    assert fake.requests[-1][2]["qty"] == "1.5000"


def test_market_order_polls_until_filled(alpaca: tuple[AlpacaBroker, FakeAlpaca]) -> None:
    broker, fake = alpaca
    fake.fill_after_polls = 3
    fill = broker.submit_market_order("MSFT", "BUY", 2, 300.0)
    assert fill.price == 102.25 and fill.quantity == 2
    polls = [r for r in fake.requests if r[0] == "GET" and r[1].startswith("/v2/orders/")]
    assert len(polls) == 3
    # Timeout: the order is recorded with the requested quantity at the reference price (and logged).
    fake.fill_after_polls = 10_000
    broker.fill_timeout = 0.0
    pending = broker.submit_market_order("MSFT", "BUY", 1, 300.0)
    assert pending.price == 300.0 and pending.quantity == 1


def test_limit_orders_cancel_and_close(alpaca: tuple[AlpacaBroker, FakeAlpaca]) -> None:
    broker, fake = alpaca
    order = broker.submit_limit_order("AAPL", "BUY", 2, 99.5, strategy="manual", stop_loss=90.0, take_profit=120.0)
    assert order.symbol == "AAPL" and order.limit_price == 99.5 and order.id == "ord-1" and order.strategy == "manual"
    assert fake.requests[-1][2]["type"] == "limit" and fake.requests[-1][2]["limit_price"] == "99.50"
    assert fake.requests[-1][2]["order_class"] == "bracket"
    pending = broker.pending_orders()
    assert [o.id for o in pending] == ["ord-1"] and pending[0].side == "BUY"
    assert broker.cancel_order("ord-1") is True
    assert broker.cancel_order("nope") is False
    assert broker.pending_orders() == []
    fake.positions["AAPL"] = {"symbol": "AAPL", "qty": "3", "avg_entry_price": "90", "side": "long"}
    closed = broker.close_position("AAPL", 94.0)
    assert closed.side == "SELL" and closed.quantity == 3 and closed.price == 95.0
    assert closed.realized_pnl == pytest.approx((95.0 - 90.0) * 3)
    with pytest.raises(BrokerError, match="No open position"):
        broker.close_position("AAPL", 94.0)


def test_error_mapping(alpaca: tuple[AlpacaBroker, FakeAlpaca]) -> None:
    broker, fake = alpaca
    fake.reject_next = True
    with pytest.raises(BrokerError, match="422: insufficient buying power"):
        broker.submit_market_order("AAPL", "BUY", 1, 1.0)
    bad = AlpacaBroker("wrong", "secret", client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    with pytest.raises(BrokerError, match="rejected the API credentials"):
        bad.test_connection()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    offline = AlpacaBroker("key", "secret", client=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(BrokerError, match="unreachable"):
        offline.cash()
    garbage = AlpacaBroker("key", "secret", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))))
    with pytest.raises(BrokerError, match="unreadable"):
        garbage.account()
    broker.close()


def test_broker_factory() -> None:
    assert set(BROKER_PROVIDERS) == {"paper", "alpaca"}
    assert create_broker("paper") is None
    alpaca = create_broker("alpaca", "k", "s", paper=False)
    assert isinstance(alpaca, AlpacaBroker) and not alpaca.is_paper
    alpaca.close()
    with pytest.raises(BrokerError, match="key and secret"):
        create_broker("alpaca", "", "")
    with pytest.raises(BrokerError, match="Unknown broker"):
        create_broker("robinhood")
