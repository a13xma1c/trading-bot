"""Payment providers for funding / withdrawing from paper accounts.

* :class:`MockPaymentProvider` - built-in sandbox.  A deposit redirects to a local checkout
  page (``/pay/mock/<session>``) where the payment is confirmed or cancelled; payouts are
  paid instantly.  Used by the demo configuration and the tests.
* :class:`StripeProvider` - Stripe Checkout Sessions for deposits (the account is credited
  when the ``checkout.session.completed`` webhook arrives or when the session is found paid
  on return), Stripe Payouts for withdrawals.  Implemented with ``httpx`` against the REST
  API; no SDK needed.

The :class:`WalletService` in :mod:`trading_bot.wallet` drives these and keeps the ledger.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .config import PaymentsConfig

logger = logging.getLogger(__name__)

ZERO_DECIMAL_CURRENCIES = {"BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV",
                           "XAF", "XOF", "XPF"}


class PaymentError(RuntimeError):
    """Provider rejected the request or is misconfigured (reported as HTTP 502 / 503)."""


@dataclass
class CheckoutSession:
    id: str
    url: str
    status: str          # open | paid | cancelled | expired
    amount: float
    currency: str
    provider: str
    transaction_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Payout:
    id: str
    status: str          # pending | paid | failed
    amount: float
    currency: str
    provider: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaymentEvent:
    kind: str                     # deposit_paid | deposit_cancelled | payout_paid | payout_failed | ignored
    reference: str                # checkout session / payout id
    transaction_id: str | None
    amount: float | None = None
    currency: str | None = None
    raw_type: str = ""
    detail: str = ""


class PaymentProvider(ABC):
    name = "payments"

    @property
    def configured(self) -> bool:
        return True

    @property
    def supports_webhooks(self) -> bool:
        return False

    @abstractmethod
    def create_checkout(self, *, amount: float, currency: str, transaction_id: str, account: str,
                        success_url: str, cancel_url: str, description: str = "") -> CheckoutSession: ...

    @abstractmethod
    def checkout_status(self, session_id: str) -> CheckoutSession: ...

    @abstractmethod
    def create_payout(self, *, amount: float, currency: str, transaction_id: str, account: str,
                      destination: str = "") -> Payout: ...

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> list[PaymentEvent]:
        raise PaymentError(f"{self.name} does not deliver webhooks")

    def close(self) -> None:  # noqa: B027 - optional hook for providers that own network clients
        return None


# ---------------------------------------------------------------------------
# Mock sandbox
# ---------------------------------------------------------------------------


class MockPaymentProvider(PaymentProvider):
    """In-memory sandbox: checkout pages are served by the app itself."""

    name = "mock"

    def __init__(self, checkout_base: str = "") -> None:
        self.checkout_base = checkout_base  # external base URL; "" = relative to the app
        self._sessions: dict[str, CheckoutSession] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._payouts: dict[str, Payout] = {}
        self._lock = threading.Lock()

    def create_checkout(self, *, amount, currency, transaction_id, account, success_url, cancel_url, description="") -> CheckoutSession:
        session_id = "mock_" + secrets.token_urlsafe(12)
        session = CheckoutSession(session_id, f"{self.checkout_base}pay/mock/{session_id}", "open", float(amount),
                                  currency.upper(), self.name, transaction_id)
        with self._lock:
            self._sessions[session_id] = session
            self._meta[session_id] = {"account": account, "success_url": success_url, "cancel_url": cancel_url,
                                      "description": description}
        return session

    def session_details(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            meta = self._meta.get(session_id)
        if session is None or meta is None:
            raise PaymentError("Unknown checkout session")
        return {**session.to_dict(), **meta}

    def checkout_status(self, session_id: str) -> CheckoutSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise PaymentError("Unknown checkout session")
        return session

    def complete(self, session_id: str) -> CheckoutSession:
        """Simulate the customer paying."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise PaymentError("Unknown checkout session")
            if session.status == "open":
                session.status = "paid"
            return session

    def cancel(self, session_id: str) -> CheckoutSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise PaymentError("Unknown checkout session")
            if session.status == "open":
                session.status = "cancelled"
            return session

    def create_payout(self, *, amount, currency, transaction_id, account, destination="") -> Payout:
        payout = Payout("mockpo_" + secrets.token_urlsafe(8), "paid", float(amount), currency.upper(), self.name,
                        detail=f"Instant sandbox payout to {destination or 'default account'}")
        with self._lock:
            self._payouts[payout.id] = payout
        return payout


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------


def to_minor_units(amount: float, currency: str) -> int:
    return int(round(amount)) if currency.upper() in ZERO_DECIMAL_CURRENCIES else int(round(amount * 100))


def from_minor_units(amount: int | float | None, currency: str) -> float:
    value = float(amount or 0)
    return value if currency.upper() in ZERO_DECIMAL_CURRENCIES else value / 100.0


def verify_stripe_signature(body: bytes, header: str, secret: str, tolerance: int = 300,
                            now: float | None = None) -> bool:
    """Check a ``Stripe-Signature`` header (``t=<ts>,v1=<hmac>[,v1=...]``)."""
    if not header or not secret:
        return False
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    timestamp = parts.get("t")
    signatures = [value for key, value in (item.split("=", 1) for item in header.split(",") if "=" in item) if key == "v1"]
    if not timestamp or not signatures:
        return False
    try:
        age = abs((now if now is not None else time.time()) - int(timestamp))
    except ValueError:
        return False
    if age > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate.strip()) for candidate in signatures)


def sign_stripe_payload(body: bytes, secret: str, timestamp: int | None = None) -> str:
    """Produce a ``Stripe-Signature`` header (used by tests and the Stripe CLI-less local flow)."""
    ts = int(timestamp if timestamp is not None else time.time())
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


class StripeProvider(PaymentProvider):
    name = "stripe"

    def __init__(self, cfg: PaymentsConfig, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.cfg.stripe.secret_key)

    @property
    def supports_webhooks(self) -> bool:
        return bool(self.cfg.stripe.webhook_secret)

    def _request(self, method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise PaymentError("Stripe is not configured (payments.stripe.secret_key)")
        try:
            response = self._client.request(method, self.cfg.stripe.api_base.rstrip("/") + path, data=data,
                                            auth=(self.cfg.stripe.secret_key, ""),
                                            headers={"Stripe-Version": "2024-06-20"})
        except httpx.HTTPError as exc:
            raise PaymentError(f"Stripe unreachable: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaymentError("Stripe returned an unreadable response") from exc
        if response.status_code >= 400:
            message = (payload.get("error") or {}).get("message") if isinstance(payload, dict) else None
            raise PaymentError(f"Stripe error {response.status_code}: {message or response.text[:200]}")
        return payload if isinstance(payload, dict) else {}

    def create_checkout(self, *, amount, currency, transaction_id, account, success_url, cancel_url, description="") -> CheckoutSession:
        currency = currency.lower()
        data = {
            "mode": "payment", "success_url": success_url, "cancel_url": cancel_url,
            "client_reference_id": transaction_id,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency,
            "line_items[0][price_data][unit_amount]": str(to_minor_units(amount, currency)),
            "line_items[0][price_data][product_data][name]": description or "Trading Bot account deposit",
            "metadata[transaction_id]": transaction_id, "metadata[account]": account,
            "payment_intent_data[metadata][transaction_id]": transaction_id,
        }
        session = self._request("POST", "/v1/checkout/sessions", data)
        return self._session(session, transaction_id)

    def checkout_status(self, session_id: str) -> CheckoutSession:
        return self._session(self._request("GET", f"/v1/checkout/sessions/{session_id}"))

    def _session(self, raw: dict[str, Any], transaction_id: str | None = None) -> CheckoutSession:
        currency = str(raw.get("currency") or "usd").upper()
        payment_status = raw.get("payment_status")
        status = "paid" if payment_status in ("paid", "no_payment_required") else \
            "expired" if raw.get("status") == "expired" else "open"
        return CheckoutSession(str(raw.get("id", "")), str(raw.get("url") or ""), status,
                               from_minor_units(raw.get("amount_total"), currency), currency, self.name,
                               transaction_id or raw.get("client_reference_id") or (raw.get("metadata") or {}).get("transaction_id"))

    def create_payout(self, *, amount, currency, transaction_id, account, destination="") -> Payout:
        data: dict[str, Any] = {"amount": str(to_minor_units(amount, currency)), "currency": currency.lower(),
                                "metadata[transaction_id]": transaction_id, "metadata[account]": account,
                                "description": f"Trading Bot withdrawal {transaction_id}"}
        if destination:
            data["destination"] = destination
        raw = self._request("POST", "/v1/payouts", data)
        status = {"paid": "paid", "failed": "failed", "canceled": "failed"}.get(str(raw.get("status")), "pending")
        return Payout(str(raw.get("id", "")), status, amount, currency.upper(), self.name,
                      detail=str(raw.get("failure_message") or ""))

    def parse_webhook(self, body: bytes, headers: Mapping[str, str]) -> list[PaymentEvent]:
        signature = headers.get("stripe-signature") or headers.get("Stripe-Signature") or ""
        if not self.supports_webhooks:
            raise PaymentError("Stripe webhook secret is not configured (payments.stripe.webhook_secret)")
        if not verify_stripe_signature(body, signature, self.cfg.stripe.webhook_secret):
            raise PaymentError("Invalid Stripe webhook signature")
        try:
            event = json.loads(body)
        except ValueError as exc:
            raise PaymentError("Webhook body is not JSON") from exc
        kind_map = {"checkout.session.completed": "deposit_paid", "checkout.session.async_payment_succeeded": "deposit_paid",
                    "checkout.session.expired": "deposit_cancelled", "checkout.session.async_payment_failed": "deposit_cancelled",
                    "payout.paid": "payout_paid", "payout.failed": "payout_failed", "payout.canceled": "payout_failed"}
        raw_type = str(event.get("type", ""))
        obj = ((event.get("data") or {}).get("object") or {}) if isinstance(event, dict) else {}
        metadata = obj.get("metadata") or {}
        currency = str(obj.get("currency") or "usd").upper()
        amount = from_minor_units(obj.get("amount_total", obj.get("amount")), currency)
        return [PaymentEvent(kind_map.get(raw_type, "ignored"), str(obj.get("id", "")),
                             obj.get("client_reference_id") or metadata.get("transaction_id"), amount, currency, raw_type,
                             str(obj.get("failure_message") or ""))]

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def create_payment_provider(cfg: PaymentsConfig, client: httpx.Client | None = None,
                            checkout_base: str = "") -> PaymentProvider | None:
    if cfg.provider == "none":
        return None
    if cfg.provider == "mock":
        return MockPaymentProvider(checkout_base)
    if cfg.provider == "stripe":
        return StripeProvider(cfg, client=client)
    raise PaymentError(f"Unknown payment provider {cfg.provider!r}")


__all__ = ["CheckoutSession", "MockPaymentProvider", "PaymentError", "PaymentEvent", "PaymentProvider", "Payout",
           "StripeProvider", "create_payment_provider", "from_minor_units", "sign_stripe_payload", "to_minor_units",
           "verify_stripe_signature"]
