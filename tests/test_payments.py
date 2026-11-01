"""Payments (sandbox + Stripe) and the wallet service (deposits, withdrawals, ledger, webhooks)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from trading_bot.broker import AccountManager, Broker, Fill, Position
from trading_bot.config import FxConfig, PaymentsConfig, StripeConfig
from trading_bot.engine import Event, EventBus, WalletChanged
from trading_bot.fx import DemoFxProvider, FxService
from trading_bot.payments import (
    MockPaymentProvider,
    PaymentError,
    PaymentEvent,
    StripeProvider,
    create_payment_provider,
    from_minor_units,
    sign_stripe_payload,
    to_minor_units,
    verify_stripe_signature,
)
from trading_bot.storage import Storage
from trading_bot.wallet import WalletError, WalletService

# ------------------------------------------------------------------ providers


def test_mock_provider_flow() -> None:
    provider = MockPaymentProvider(checkout_base="https://bot.example/")
    assert provider.configured and not provider.supports_webhooks
    session = provider.create_checkout(amount=100, currency="eur", transaction_id="tx1", account="bob",
                                       success_url="https://bot.example/?ok", cancel_url="https://bot.example/?no")
    assert session.url == f"https://bot.example/pay/mock/{session.id}" and session.status == "open"
    assert session.currency == "EUR" and session.transaction_id == "tx1"
    details = provider.session_details(session.id)
    assert details["account"] == "bob" and details["success_url"].endswith("?ok")
    assert provider.checkout_status(session.id).status == "open"
    assert provider.complete(session.id).status == "paid"
    assert provider.cancel(session.id).status == "paid"  # terminal states do not change
    other = provider.create_checkout(amount=1, currency="USD", transaction_id="tx2", account="bob", success_url="s", cancel_url="c")
    assert provider.cancel(other.id).status == "cancelled"
    with pytest.raises(PaymentError):
        provider.checkout_status("nope")
    with pytest.raises(PaymentError):
        provider.session_details("nope")
    payout = provider.create_payout(amount=50, currency="USD", transaction_id="tx3", account="bob", destination="IBAN")
    assert payout.status == "paid" and "IBAN" in payout.detail
    with pytest.raises(PaymentError, match="does not deliver webhooks"):
        provider.parse_webhook(b"{}", {})


def test_minor_units_and_signatures() -> None:
    assert to_minor_units(12.34, "usd") == 1234 and from_minor_units(1234, "USD") == 12.34
    assert to_minor_units(1500, "JPY") == 1500 and from_minor_units(1500, "jpy") == 1500.0
    body = b'{"id": "evt_1"}'
    header = sign_stripe_payload(body, "whsec_test", timestamp=1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert verify_stripe_signature(body, header, "whsec_test", now=1_700_000_100)
    assert not verify_stripe_signature(body, header, "whsec_test", now=1_700_001_000)   # too old
    assert not verify_stripe_signature(body, header, "other-secret", now=1_700_000_100)
    assert not verify_stripe_signature(b"tampered", header, "whsec_test", now=1_700_000_100)
    assert not verify_stripe_signature(body, "garbage", "whsec_test") and not verify_stripe_signature(body, header, "")
    assert not verify_stripe_signature(body, "t=abc,v1=def", "whsec_test")


class FakeStripe:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, list[str]]]] = []
        self.paid = False
        self.payout_status = "pending"

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode()) if request.content else {}
        self.requests.append((request.method, request.url.path, form))
        if request.headers.get("authorization") != "Basic c2tfdGVzdF94eXo6":  # base64("sk_test_xyz:")
            return httpx.Response(401, json={"error": {"message": "Invalid API Key provided"}})
        if request.url.path == "/v1/checkout/sessions" and request.method == "POST":
            return httpx.Response(200, json={"id": "cs_123", "url": "https://checkout.stripe.com/c/pay/cs_123", "payment_status": "unpaid",
                                             "status": "open", "amount_total": form["line_items[0][price_data][unit_amount]"][0],
                                             "currency": form["line_items[0][price_data][currency]"][0],
                                             "client_reference_id": form["client_reference_id"][0]})
        if request.url.path == "/v1/checkout/sessions/cs_123":
            return httpx.Response(200, json={"id": "cs_123", "payment_status": "paid" if self.paid else "unpaid",
                                             "status": "complete" if self.paid else "open", "amount_total": 10000, "currency": "eur",
                                             "client_reference_id": "tx1"})
        if request.url.path == "/v1/checkout/sessions/cs_gone":
            return httpx.Response(200, json={"id": "cs_gone", "payment_status": "unpaid", "status": "expired", "amount_total": 1,
                                             "currency": "usd"})
        if request.url.path == "/v1/payouts":
            return httpx.Response(200, json={"id": "po_1", "status": self.payout_status, "failure_message": "no bank" if self.payout_status == "failed" else None})
        return httpx.Response(400, json={"error": {"message": "bad request"}})


def stripe_provider(fake: FakeStripe, **kwargs: Any) -> StripeProvider:
    cfg = PaymentsConfig(provider="stripe", stripe=StripeConfig(secret_key="sk_test_xyz", webhook_secret="whsec_1", **kwargs))
    return StripeProvider(cfg, client=httpx.Client(transport=httpx.MockTransport(fake.handler)))


def test_stripe_provider() -> None:
    fake = FakeStripe()
    provider = stripe_provider(fake)
    assert provider.configured and provider.supports_webhooks
    session = provider.create_checkout(amount=100.0, currency="EUR", transaction_id="tx1", account="bob",
                                       success_url="https://bot/?s", cancel_url="https://bot/?c", description="Deposit")
    assert session.id == "cs_123" and session.url.startswith("https://checkout.stripe.com/") and session.status == "open"
    assert session.amount == 100.0 and session.currency == "EUR" and session.transaction_id == "tx1"
    method, path, form = fake.requests[-1]
    assert form["mode"] == ["payment"] and form["line_items[0][price_data][unit_amount]"] == ["10000"]
    assert form["metadata[transaction_id]"] == ["tx1"] and form["metadata[account]"] == ["bob"]
    assert form["line_items[0][price_data][product_data][name]"] == ["Deposit"]
    assert provider.checkout_status("cs_123").status == "open"
    fake.paid = True
    status = provider.checkout_status("cs_123")
    assert status.status == "paid" and status.amount == 100.0 and status.transaction_id == "tx1"
    assert provider.checkout_status("cs_gone").status == "expired"
    payout = provider.create_payout(amount=25.5, currency="USD", transaction_id="tx2", account="bob", destination="ba_1")
    assert payout.id == "po_1" and payout.status == "pending" and fake.requests[-1][2]["amount"] == ["2550"]
    assert fake.requests[-1][2]["destination"] == ["ba_1"]
    fake.payout_status = "failed"
    assert provider.create_payout(amount=1, currency="USD", transaction_id="tx3", account="bob").detail == "no bank"
    with pytest.raises(PaymentError, match="Stripe error 400"):
        provider._request("POST", "/v1/unknown")
    # Webhooks: signature required, event types mapped.
    event = {"type": "checkout.session.completed", "data": {"object": {"id": "cs_123", "client_reference_id": "tx1",
                                                                       "amount_total": 10000, "currency": "eur", "metadata": {}}}}
    body = json.dumps(event).encode()
    parsed = provider.parse_webhook(body, {"stripe-signature": sign_stripe_payload(body, "whsec_1")})
    assert parsed == [PaymentEvent("deposit_paid", "cs_123", "tx1", 100.0, "EUR", "checkout.session.completed", "")]
    with pytest.raises(PaymentError, match="Invalid Stripe webhook signature"):
        provider.parse_webhook(body, {"stripe-signature": "t=1,v1=bad"})
    payout_event = json.dumps({"type": "payout.failed", "data": {"object": {"id": "po_1", "amount": 2550, "currency": "usd",
                                                                           "metadata": {"transaction_id": "tx2"}, "failure_message": "closed"}}}).encode()
    parsed = provider.parse_webhook(payout_event, {"Stripe-Signature": sign_stripe_payload(payout_event, "whsec_1")})
    assert parsed[0].kind == "payout_failed" and parsed[0].transaction_id == "tx2" and parsed[0].amount == 25.5 and parsed[0].detail == "closed"
    unknown = json.dumps({"type": "charge.refunded", "data": {"object": {"id": "ch_1"}}}).encode()
    assert provider.parse_webhook(unknown, {"stripe-signature": sign_stripe_payload(unknown, "whsec_1")})[0].kind == "ignored"
    not_json = b"nope"
    with pytest.raises(PaymentError, match="not JSON"):
        provider.parse_webhook(not_json, {"stripe-signature": sign_stripe_payload(not_json, "whsec_1")})

    unconfigured = StripeProvider(PaymentsConfig(provider="stripe"))
    assert not unconfigured.configured
    with pytest.raises(PaymentError, match="not configured"):
        unconfigured.create_checkout(amount=1, currency="USD", transaction_id="t", account="a", success_url="s", cancel_url="c")
    with pytest.raises(PaymentError, match="webhook secret"):
        unconfigured.parse_webhook(b"{}", {})
    wrong_key = StripeProvider(PaymentsConfig(provider="stripe", stripe=StripeConfig(secret_key="sk_wrong")),
                               client=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    with pytest.raises(PaymentError, match="Invalid API Key"):
        wrong_key.checkout_status("cs_123")

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    with pytest.raises(PaymentError, match="unreachable"):
        StripeProvider(PaymentsConfig(provider="stripe", stripe=StripeConfig(secret_key="k")),
                       client=httpx.Client(transport=httpx.MockTransport(offline))).checkout_status("cs_123")
    provider.close()


def test_create_payment_provider() -> None:
    assert create_payment_provider(PaymentsConfig(provider="none")) is None
    assert isinstance(create_payment_provider(PaymentsConfig(provider="mock")), MockPaymentProvider)
    assert isinstance(create_payment_provider(PaymentsConfig(provider="stripe")), StripeProvider)
    with pytest.raises(PaymentError, match="Unknown payment provider"):
        create_payment_provider(PaymentsConfig(provider="paypal"))


# --------------------------------------------------------------------- wallet


class ExternalBroker(Broker):
    name = "external"

    def submit_market_order(self, symbol: str, side: str, quantity: float, price: float, strategy: str = "",
                            stop_loss: float | None = None, take_profit: float | None = None) -> Fill:
        return Fill(symbol, side, quantity, price, datetime.now(UTC))

    def positions(self) -> dict[str, Position]:
        return {}

    def cash(self) -> float:
        return 1.0


def make_wallet(provider: Any = None, fx: bool = True, **cfg: Any) -> tuple[WalletService, Storage, AccountManager, list[Event]]:
    storage = Storage(":memory:")
    accounts = AccountManager(1_000.0, default_name="admin")
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    fx_service = FxService(DemoFxProvider(clock=lambda: datetime(2024, 3, 4, tzinfo=UTC)), FxConfig(provider="demo")) if fx else None
    payments = PaymentsConfig(provider="mock", **cfg)
    wallet = WalletService(storage, accounts, payments, provider if provider is not None else MockPaymentProvider(), fx_service, bus)
    return wallet, storage, accounts, events


def test_deposit_flow_with_conversion() -> None:
    wallet, storage, accounts, events = make_wallet(min_amount=10, max_amount=5_000)
    broker = accounts.ensure("bob")
    summary = wallet.summary("bob", broker)
    assert summary["funding_enabled"] and summary["cash"] == 1_000 and summary["available"] == 1_000 and summary["pending"] == []
    quote = wallet.quote_deposit(100, "EUR")
    assert quote["source_currency"] == "EUR" and quote["currency"] == "USD" and 100 < quote["amount"] < 125 and quote["rate"] > 1
    assert wallet.quote_deposit(100, "usd") == {"source_amount": 100.0, "source_currency": "USD", "amount": 100.0, "currency": "USD",
                                                "rate": 1.0, "rate_date": None}
    with pytest.raises(WalletError, match="Minimum amount"):
        wallet.start_deposit("bob", broker, 5, "USD", "https://bot")
    with pytest.raises(WalletError, match="Maximum amount"):
        wallet.start_deposit("bob", broker, 50_000, "USD", "https://bot")
    with pytest.raises(WalletError, match="Amount must be positive"):
        wallet.start_deposit("bob", broker, -1, "USD", "https://bot")
    with pytest.raises(WalletError, match="Cannot convert"):
        wallet.start_deposit("bob", broker, 100, "XXX", "https://bot")

    started = wallet.start_deposit("bob", broker, 100, "EUR", "https://bot/app/")
    tx = started["transaction"]
    assert tx["status"] == "pending" and tx["kind"] == "deposit" and tx["amount"] == quote["amount"]
    assert tx["source_amount"] == 100 and tx["source_currency"] == "EUR" and tx["reference"] == started["session"]["id"]
    assert tx["metadata"]["rate"] == quote["rate"] and tx["metadata"]["session_url"] == started["checkout_url"]
    assert started["checkout_url"].startswith("pay/mock/")
    provider = wallet.provider
    assert isinstance(provider, MockPaymentProvider)
    details = provider.session_details(tx["reference"])
    assert details["success_url"] == f"https://bot/app/?deposit={tx['id']}&result=success#wallet"
    assert details["currency"] == "EUR" and details["amount"] == 100.0
    assert wallet.summary("bob", broker)["pending"][0]["id"] == tx["id"]
    # Not paid yet: sync keeps it pending and the balance is unchanged.
    assert wallet.sync_deposit(tx["id"], account="bob")["status"] == "pending" and broker.cash() == 1_000
    with pytest.raises(WalletError, match="Unknown deposit"):
        wallet.sync_deposit(tx["id"], account="someone-else")
    # The customer pays on the sandbox page -> completing credits the paper account exactly once.
    provider.complete(tx["reference"])
    done = wallet.sync_deposit(tx["id"], account="bob")
    assert done["status"] == "completed" and done["balance_after"] == pytest.approx(1_000 + quote["amount"])
    assert broker.cash() == pytest.approx(1_000 + quote["amount"])
    assert wallet.complete_deposit(tx["id"])["status"] == "completed" and broker.cash() == pytest.approx(1_000 + quote["amount"])
    assert [e for e in events if isinstance(e, WalletChanged)][-1] == WalletChanged("bob", "deposit", quote["amount"], "completed")
    totals = wallet.summary("bob", broker)["totals"]
    assert totals["deposits"] == pytest.approx(quote["amount"]) and totals["count"] == 1
    with pytest.raises(WalletError, match="Unknown deposit"):
        wallet.complete_deposit("nope")

    # Cancelled / expired checkouts never credit.
    second = wallet.start_deposit("bob", broker, 50, "USD", "https://bot")
    provider.cancel(second["transaction"]["reference"])
    cancelled = wallet.sync_deposit(second["transaction"]["id"])
    assert cancelled["status"] == "cancelled" and broker.cash() == pytest.approx(1_000 + quote["amount"])
    with pytest.raises(WalletError, match="Deposit is cancelled"):
        wallet.complete_deposit(second["transaction"]["id"])
    assert wallet.cancel_deposit(second["transaction"]["id"])["status"] == "cancelled"  # idempotent


def test_withdrawals_and_refunds() -> None:
    wallet, storage, accounts, events = make_wallet()
    broker = accounts.ensure("bob")
    with pytest.raises(WalletError, match="Insufficient cash") as exc:
        wallet.withdraw("bob", broker, 5_000)
    assert exc.value.status == 409
    tx = wallet.withdraw("bob", broker, 250, "IBAN DE12")
    assert tx["status"] == "completed" and tx["amount"] == -250 and tx["balance_after"] == 750 and broker.cash() == 750
    assert tx["metadata"]["destination"] == "IBAN DE12" and tx["reference"].startswith("mockpo_")
    assert wallet.summary("bob", broker)["net_deposits"] == -250

    class FailingPayouts(MockPaymentProvider):
        def create_payout(self, **kwargs: Any):  # type: ignore[no-untyped-def]
            raise PaymentError("payouts disabled")

    failing, _, failing_accounts, _ = make_wallet(provider=FailingPayouts())
    failing_broker = failing_accounts.ensure("bob")
    with pytest.raises(WalletError, match="Payment provider error") as failure:
        failing.withdraw("bob", failing_broker, 100)
    assert failure.value.status == 502 and failing_broker.cash() == 1_000  # refunded
    assert failing.storage.transactions("bob")[0]["status"] == "failed"

    class PendingPayouts(MockPaymentProvider):
        def create_payout(self, **kwargs: Any):  # type: ignore[no-untyped-def]
            payout = super().create_payout(**kwargs)
            payout.status = "pending"
            return payout

    pending, _, pending_accounts, _ = make_wallet(provider=PendingPayouts())
    pending_broker = pending_accounts.ensure("bob")
    tx = pending.withdraw("bob", pending_broker, 100)
    assert tx["status"] == "pending" and pending_broker.cash() == 900
    # A failed payout reported later refunds the money; a paid one completes the entry.
    result = pending.apply_event(PaymentEvent("payout_failed", tx["reference"], tx["id"], 100, "USD", "payout.failed", "bank closed"))
    assert result["status"] == "failed" and pending_broker.cash() == 1_000
    assert "bank closed" in (pending.storage.transaction(tx["id"]) or {})["description"]
    tx2 = pending.withdraw("bob", pending_broker, 100)
    assert pending.apply_event(PaymentEvent("payout_paid", tx2["reference"], None, 100, "USD", "payout.paid"))["status"] == "completed"
    assert pending_broker.cash() == 900


def test_wallet_policies() -> None:
    wallet, storage, accounts, _ = make_wallet()
    external = accounts.set_broker("bob", ExternalBroker())
    summary = wallet.summary("bob", external)
    assert summary["funding_enabled"] is False and "brokerage" in summary["funding_disabled_reason"]
    with pytest.raises(WalletError, match="connected to a brokerage") as exc:
        wallet.start_deposit("bob", external, 100, "USD", "https://bot")
    assert exc.value.status == 409
    disabled, _, disabled_accounts, _ = make_wallet()
    disabled.provider = None
    paper = disabled_accounts.ensure("bob")
    assert disabled.summary("bob", paper)["funding_disabled_reason"] == "No payment provider configured (payments.provider)"
    with pytest.raises(WalletError, match="No payment provider") as missing:
        disabled.withdraw("bob", paper, 10)
    assert missing.value.status == 503
    with pytest.raises(WalletError, match="No payment provider"):
        disabled.handle_webhook(b"{}", {})
    no_fx, _, no_fx_accounts, _ = make_wallet(fx=False)
    with pytest.raises(WalletError, match="exchange rates"):
        no_fx.quote_deposit(10, "EUR")
    assert no_fx.quote_deposit(10, "USD")["amount"] == 10.0


def test_webhook_events_end_to_end() -> None:
    fake = FakeStripe()
    wallet, storage, accounts, _ = make_wallet(provider=stripe_provider(fake))
    broker = accounts.ensure("bob")
    started = wallet.start_deposit("bob", broker, 100, "EUR", "https://bot")
    tx = started["transaction"]
    assert tx["reference"] == "cs_123" and started["checkout_url"].startswith("https://checkout.stripe.com/")
    event = {"type": "checkout.session.completed", "data": {"object": {"id": "cs_123", "client_reference_id": tx["id"],
                                                                       "amount_total": 10000, "currency": "eur", "metadata": {}}}}
    body = json.dumps(event).encode()
    results = wallet.handle_webhook(body, {"stripe-signature": sign_stripe_payload(body, "whsec_1")})
    assert results == [{"event": "checkout.session.completed", "handled": True, "transaction": tx["id"], "status": "completed"}]
    assert broker.cash() == pytest.approx(1_000 + tx["amount"])
    # Replays are harmless, unknown transactions and amount mismatches are reported but ignored.
    assert wallet.handle_webhook(body, {"stripe-signature": sign_stripe_payload(body, "whsec_1")})[0]["status"] == "completed"
    assert broker.cash() == pytest.approx(1_000 + tx["amount"])
    unknown = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_zzz", "amount_total": 1, "currency": "usd"}}}).encode()
    assert wallet.handle_webhook(unknown, {"stripe-signature": sign_stripe_payload(unknown, "whsec_1")})[0] == \
        {"event": "checkout.session.completed", "handled": False, "reason": "unknown transaction"}
    second = wallet.start_deposit("bob", broker, 100, "EUR", "https://bot")["transaction"]
    mismatch = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": "cs_123", "client_reference_id": second["id"],
                                                                                    "amount_total": 1, "currency": "eur"}}}).encode()
    result = wallet.handle_webhook(mismatch, {"stripe-signature": sign_stripe_payload(mismatch, "whsec_1")})[0]
    assert result["handled"] is False and result["reason"] == "amount mismatch"
    expired = json.dumps({"type": "checkout.session.expired", "data": {"object": {"id": "cs_123", "client_reference_id": second["id"],
                                                                                 "amount_total": 10000, "currency": "eur"}}}).encode()
    assert wallet.handle_webhook(expired, {"stripe-signature": sign_stripe_payload(expired, "whsec_1")})[0]["status"] == "failed"
    with pytest.raises(PaymentError, match="Invalid Stripe webhook signature"):
        wallet.handle_webhook(body, {"stripe-signature": f"t={int(time.time())},v1=bad"})
