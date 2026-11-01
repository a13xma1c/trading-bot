"""REST API tests for manual trading, broker connections, wallet / payments, transactions and FX."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from trading_bot.app import build_engine
from trading_bot.brokers.alpaca import AlpacaBroker
from trading_bot.config import AppConfig
from trading_bot.engine import TradingEngine
from trading_bot.payments import sign_stripe_payload
from trading_bot.web import routes_trading
from trading_bot.web.server import create_app

from .test_alpaca import FakeAlpaca
from .test_web_api import make_config, sign_in
from .web_helpers import TEST_USER

Web = tuple[TestClient, TradingEngine, Path]


@pytest.fixture
def web(tmp_path: Path) -> Iterator[Web]:
    config_path = make_config(tmp_path, fx={"provider": "demo"},
                              execution={"slippage_model": "fixed", "slippage_bps": 10, "commission_per_order": 1.0})
    engine = build_engine(AppConfig.load(config_path))
    with TestClient(create_app(engine, str(config_path))) as client:
        sign_in(client)
        yield client, engine, config_path
    engine.close()


# ------------------------------------------------------------------ orders


def test_simulate_and_place_orders(web: Web) -> None:
    client, engine, _ = web
    assert client.post("/api/orders/simulate", json={"symbol": "AAPL", "side": "BUY"}).status_code == 422  # no size
    assert client.post("/api/orders/simulate", json={"symbol": "AAPL", "side": "HOLD", "quantity": 1}).status_code == 422
    sim = client.post("/api/orders/simulate", json={"symbol": "AAPL", "side": "BUY", "quantity": 10, "stop_loss_pct": 5}).json()
    assert sim["accepted"] and sim["fill_price"] > sim["reference_price"] and sim["commission"] == 1.0
    assert sim["broker"]["name"] == "paper" and sim["stop_loss"] == pytest.approx(sim["fill_price"] * 0.95)
    assert sim["cash_after"] == pytest.approx(50_000 - sim["total_cost"])
    assert engine.broker.position("AAPL") is None  # a simulation changes nothing

    # Notional sizing: spend about 5,000 of cash.
    placed = client.post("/api/orders", json={"symbol": "AAPL", "side": "BUY", "notional": 5000})
    assert placed.status_code == 201
    body = placed.json()
    assert body["status"] == "filled" and body["fill"]["quantity"] == int(5000 // sim["reference_price"])
    assert body["transaction"]["kind"] == "buy" and body["pending_orders"] == []
    assert "filled @" in body["message"]
    assert client.post("/api/orders", json={"symbol": "AAPL", "side": "BUY", "notional": 1}).status_code == 422
    positions = client.get("/api/positions").json()
    assert positions["positions"][0]["symbol"] == "AAPL" and positions["positions"][0]["strategy"] == "manual"
    assert positions["account"]["broker"]["label"].startswith("Paper")

    # Rejections come back as 409 with the reason.
    too_many = client.post("/api/orders", json={"symbol": "AAPL", "side": "SELL", "quantity": 10_000})
    assert too_many.status_code == 409 and "held" in too_many.json()["detail"]
    assert client.post("/api/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 1_000_000}).status_code == 409

    # Limit orders: resting until marketable; listed and cancellable.
    resting = client.post("/api/orders", json={"symbol": "MSFT", "side": "BUY", "quantity": 2, "order_type": "limit", "limit_price": 1.0})
    assert resting.status_code == 201 and resting.json()["status"] == "pending"
    order_id = resting.json()["order"]["id"]
    assert [o["id"] for o in client.get("/api/orders").json()] == [order_id]
    assert len(client.get("/api/positions").json()["pending_orders"]) == 1
    assert client.post("/api/orders", json={"symbol": "MSFT", "side": "BUY", "quantity": 2, "order_type": "limit"}).status_code == 422
    cancelled = client.delete(f"/api/orders/{order_id}")
    assert cancelled.status_code == 200 and cancelled.json()["cancelled"]["id"] == order_id and cancelled.json()["pending_orders"] == []
    assert client.delete(f"/api/orders/{order_id}").status_code == 404

    # Close the position with one call; the ledger has both legs.
    closed = client.delete("/api/positions/aapl")
    assert closed.status_code == 200 and closed.json()["fill"]["side"] == "SELL"
    assert client.get("/api/positions").json()["positions"] == []
    assert client.delete("/api/positions/AAPL").status_code == 404
    ledger = client.get("/api/transactions").json()
    assert [t["kind"] for t in ledger["transactions"]] == ["sell", "buy"]
    assert ledger["totals"]["fees"] == 2.0 and ledger["currency"] == "USD"
    history = client.get("/api/history").json()
    assert history["stats"]["trades"] == 1 and history["fills"][0]["commission"] == 1.0


def test_broker_connection_with_fake_alpaca(web: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    client, engine, _ = web
    fake = FakeAlpaca()
    fake.key = "PKTEST1234"

    def fake_create(provider: str, key: str = "", secret: str = "", paper: bool = True, **kwargs: Any) -> Any:
        if provider == "paper":
            return None
        return AlpacaBroker(key, secret, paper=paper, client=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                            poll_interval=0.0, sleep=lambda _: None)

    monkeypatch.setattr(routes_trading, "create_broker", fake_create)
    providers = client.get("/api/broker/providers").json()["providers"]
    assert [p["name"] for p in providers] == ["paper", "alpaca"] and providers[1]["needs_credentials"]
    current = client.get("/api/broker/connection").json()
    assert current["provider"] == "paper" and current["connected"] is False and current["active"]["paper"] is True

    assert client.put("/api/broker/connection", json={"provider": "alpaca"}).status_code == 422  # credentials required
    assert client.put("/api/broker/connection", json={"provider": "etrade", "key": "k", "secret": "s"}).status_code == 422
    bad = client.put("/api/broker/connection", json={"provider": "alpaca", "key": "wrong", "secret": "secret"})
    assert bad.status_code == 502 and "credentials" in bad.json()["detail"]
    assert client.get("/api/broker/connection").json()["provider"] == "paper"  # rolled back

    good = client.put("/api/broker/connection", json={"provider": "alpaca", "key": "PKTEST1234", "secret": "secret", "paper": True})
    assert good.status_code == 200
    data = good.json()
    assert data["connected"] and data["key_hint"] == "…1234"
    assert data["active"]["label"] == "Alpaca (paper)" and data["status"]["account_number"] == "PA123"
    me = client.get("/api/auth/me").json()
    assert me["broker"]["provider"] == "alpaca" and "PKTEST1234" not in json.dumps(me) and "secret" not in json.dumps(me)
    # Orders now go to the brokerage, positions/cash come from it, funding is disabled.
    placed = client.post("/api/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 2})
    assert placed.status_code == 201 and placed.json()["fill"]["price"] == 101.5
    assert fake.requests[-1][1] == "/v2/orders"
    positions = client.get("/api/positions").json()
    assert positions["account"]["paper"] is True and positions["account"]["broker"]["name"] == "alpaca"
    assert positions["positions"][0]["symbol"] == "AAPL" and positions["account"]["cash"] == fake.cash
    wallet = client.get("/api/wallet").json()
    assert wallet["funding_enabled"] is False and "brokerage" in wallet["funding_disabled_reason"]
    assert client.post("/api/wallet/deposit", json={"amount": 100}).status_code == 409
    check = client.get("/api/broker/connection?check=true").json()
    assert check["status"]["equity"] == fake.cash + 500 and check["error"] is None
    # Blank credentials keep the stored ones; switching back to paper restores the paper account.
    assert client.put("/api/broker/connection", json={"provider": "alpaca", "paper": False}).json()["active"]["label"] == "Alpaca (LIVE)"
    back = client.put("/api/broker/connection", json={"provider": "paper"}).json()
    assert back["connected"] is False and back["active"]["name"] == "paper"
    assert client.get("/api/positions").json()["account"]["cash"] == 50_000
    # The user's connection is restored on restart.
    client.put("/api/broker/connection", json={"provider": "alpaca", "key": "PKTEST1234", "secret": "secret"})
    with TestClient(create_app(engine, None)) as fresh:
        assert engine.accounts.get(TEST_USER) is not None and engine.accounts.get(TEST_USER).name == "alpaca"  # type: ignore[union-attr]
        fresh.close()


# ------------------------------------------------------------------ wallet


def test_wallet_deposit_withdraw_and_transactions(web: Web) -> None:
    client, engine, _ = web
    wallet = client.get("/api/wallet").json()
    assert wallet["cash"] == 50_000 and wallet["funding_enabled"] and wallet["provider"] == "mock" and "EUR" in wallet["currencies"]
    assert wallet["min_amount"] == 10 and wallet["recent"] == []
    quote = client.get("/api/wallet/quote?amount=100&currency=EUR").json()
    assert quote["currency"] == "USD" and quote["amount"] > 100
    assert client.get("/api/wallet/quote?amount=100&currency=XXX").status_code == 503
    assert client.post("/api/wallet/deposit", json={"amount": 1}).status_code == 422

    started = client.post("/api/wallet/deposit", json={"amount": 100, "currency": "EUR"})
    assert started.status_code == 201
    tx = started.json()["transaction"]
    session_id = started.json()["session"]["id"]
    assert started.json()["checkout_url"] == f"pay/mock/{session_id}" and tx["status"] == "pending"
    assert client.get("/api/wallet").json()["pending"][0]["id"] == tx["id"]
    # The sandbox checkout page and its API.
    assert client.get(f"/pay/mock/{session_id}").status_code == 200 and 'id="payButton"' in client.get(f"/pay/mock/{session_id}").text
    details = client.get(f"/api/payments/mock/{session_id}").json()
    assert details["amount"] == 100 and details["currency"] == "EUR" and details["account"] == TEST_USER
    assert client.get("/api/payments/mock/unknown").status_code == 404
    assert client.post(f"/api/payments/mock/{session_id}/refund").status_code == 404
    synced = client.post(f"/api/wallet/deposits/{tx['id']}/sync").json()
    assert synced["status"] == "pending"
    paid = client.post(f"/api/payments/mock/{session_id}/pay").json()
    assert paid["transaction"]["status"] == "completed" and paid["redirect"].endswith(f"?deposit={tx['id']}&result=success#wallet")
    assert paid["redirect"].startswith("http://testserver/")
    after = client.get("/api/wallet").json()
    assert after["cash"] == pytest.approx(50_000 + quote["amount"]) and after["totals"]["deposits"] == pytest.approx(quote["amount"])
    assert client.post(f"/api/wallet/deposits/{tx['id']}/sync").json()["status"] == "completed"  # idempotent on return
    assert client.post("/api/wallet/deposits/nope/sync").status_code == 404

    # Cancelled checkout.
    second = client.post("/api/wallet/deposit", json={"amount": 20}).json()
    cancelled = client.post(f"/api/payments/mock/{second['session']['id']}/cancel").json()
    assert cancelled["transaction"]["status"] == "cancelled" and cancelled["redirect"].endswith("result=cancelled#wallet")

    # Withdrawals.
    assert client.post("/api/wallet/withdraw", json={"amount": 1_000_000}).status_code == 409
    withdrawn = client.post("/api/wallet/withdraw", json={"amount": 250.5, "destination": "DE12 3456"})
    assert withdrawn.status_code == 201 and withdrawn.json()["status"] == "completed" and withdrawn.json()["amount"] == -250.5
    assert client.get("/api/wallet").json()["cash"] == pytest.approx(50_000 + quote["amount"] - 250.5)

    # Ledger with filters and CSV export.
    ledger = client.get("/api/transactions").json()
    assert [t["kind"] for t in ledger["transactions"]] == ["withdrawal", "deposit", "deposit"]
    assert [t["status"] for t in ledger["transactions"]] == ["completed", "cancelled", "completed"]
    assert [t["kind"] for t in client.get("/api/transactions?kind=deposit&status=completed").json()["transactions"]] == ["deposit"]
    assert client.get("/api/transactions?kind=refund").status_code == 422
    assert client.get("/api/transactions?status=lost").status_code == 422
    assert len(client.get("/api/transactions?days=1&limit=2").json()["transactions"]) == 2
    csv_response = client.get("/api/transactions?format=csv")
    assert csv_response.status_code == 200 and csv_response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in csv_response.headers["content-disposition"]
    lines = csv_response.text.strip().splitlines()
    assert lines[0].startswith("id,timestamp,kind,status,amount") and len(lines) == 4 and "withdrawal" in lines[1]


def test_mock_checkout_is_private_per_user(web: Web) -> None:
    client, engine, _ = web
    session_id = client.post("/api/wallet/deposit", json={"amount": 50}).json()["session"]["id"]
    client.post("/api/auth/logout")
    assert client.get(f"/api/payments/mock/{session_id}").status_code == 401
    client.post("/api/auth/signup", json={"username": "eve", "password": "Sunny-Trail-42!"})
    assert client.get("/api/auth/me").json()["username"] == "eve"
    assert client.get(f"/api/payments/mock/{session_id}").status_code == 403
    assert client.post(f"/api/payments/mock/{session_id}/pay").status_code == 403
    assert client.get("/api/wallet").json()["cash"] == 50_000  # eve's own paper account


def test_stripe_webhook_endpoint(tmp_path: Path) -> None:
    config_path = make_config(tmp_path, payments={"provider": "stripe", "stripe": {"secret_key": "sk_test", "webhook_secret": "whsec_1"}})
    engine = build_engine(AppConfig.load(config_path))
    try:
        with TestClient(create_app(engine, str(config_path))) as client:
            sign_in(client)
            assert client.get("/pay/mock/x").status_code == 404  # sandbox pages only exist for the mock provider
            assert client.get("/api/wallet").json()["provider"] == "stripe"
            # A pending deposit as created by start_deposit (the checkout call itself would need the network).
            tx = engine.storage.record_transaction(TEST_USER, "deposit", 100.0, status="pending", reference="cs_abc",
                                                   source_amount=100.0, source_currency="USD")
            event = json.dumps({"type": "checkout.session.completed", "data": {"object": {
                "id": "cs_abc", "client_reference_id": tx["id"], "amount_total": 10000, "currency": "usd"}}}).encode()
            unsigned = client.post("/api/payments/webhook", content=event, headers={"content-type": "application/json"})
            assert unsigned.status_code == 400 and "signature" in unsigned.json()["detail"]
            signed = client.post("/api/payments/webhook", content=event,
                                 headers={"content-type": "application/json", "stripe-signature": sign_stripe_payload(event, "whsec_1")})
            assert signed.status_code == 200 and signed.json()["results"][0]["status"] == "completed"
            assert client.get("/api/wallet").json()["cash"] == 50_100
            settings = client.get("/api/settings").json()["payments"]
            assert settings["stripe"]["secret_key"] == "" and settings["stripe"]["secret_key_set"] is True and settings["configured"]
    finally:
        engine.close()


# ---------------------------------------------------------------------- fx


def test_fx_endpoints(web: Web) -> None:
    client, engine, _ = web
    latest = client.get("/api/fx/latest").json()
    assert latest["base"] == "USD" and latest["provider"] == "demo" and latest["account_currency"] == "USD"
    assert {r["currency"] for r in latest["rows"]} >= {"EUR", "GBP", "JPY"}
    eur = next(r for r in latest["rows"] if r["currency"] == "EUR")
    assert eur["name"] == "Euro" and eur["inverse"] == pytest.approx(1 / eur["rate"])
    assert client.get("/api/fx/latest?base=EUR").json()["base"] == "EUR"
    assert client.get("/api/fx/latest?base=EURO").status_code == 422
    assert client.get("/api/fx/latest?base=XXX").status_code == 502
    history = client.get("/api/fx/history?quote=EUR&days=30").json()
    assert history["base"] == "USD" and history["quote"] == "EUR" and 18 <= len(history["points"]) <= 24
    assert history["min"] <= history["points"][-1]["rate"] <= history["max"] and history["change_pct"] is not None
    assert client.get("/api/fx/history?quote=EUR&days=1").status_code == 422
    convert = client.get("/api/fx/convert?amount=100&from=EUR&to=USD").json()
    assert convert["result"] == pytest.approx(100 * convert["rate"]) and convert["source"] == "demo"
    assert client.get("/api/fx/convert?amount=0&from=EUR&to=USD").status_code == 422
    currencies = client.get("/api/fx/currencies").json()
    assert currencies["base"] == "USD" and {"code": "EUR", "name": "Euro"} in currencies["currencies"]
    # Admin settings switch providers live (and reject bad values).
    assert client.put("/api/settings", json={"fx": {"provider": "carrier-pigeon"}}).status_code == 422
    assert client.put("/api/settings", json={"fx": {"base_currency": "euro"}}).status_code == 422
    assert client.put("/api/settings", json={"fx": {"base_currency": "EUR", "quote_currencies": ["USD", "CHF"]}}).status_code == 200
    assert set(client.get("/api/fx/latest").json()["rates"]) == {"USD", "CHF"}
    assert client.put("/api/settings", json={"payments": {"provider": "none"}}).status_code == 200
    assert client.get("/api/wallet").json()["funding_enabled"] is False
    assert client.put("/api/settings", json={"payments": {"currency": "us"}}).status_code == 422
    assert client.put("/api/settings", json={"execution": {"slippage_model": "random"}}).status_code == 422
    assert client.put("/api/settings", json={"execution": {"slippage_model": "volume", "commission_pct": 0.1}}).status_code == 200
    assert engine.execution.cfg.slippage_model == "volume" and engine.config.execution.commission_pct == 0.1
