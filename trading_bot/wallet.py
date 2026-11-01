"""Funding of paper accounts: deposits through a payment provider, withdrawals, ledger.

Flow of a deposit::

    start_deposit()  ->  ledger entry "pending"  ->  provider checkout (redirect)
    customer pays    ->  webhook / return-URL sync  ->  complete_deposit(): cash credited, entry "completed"

Deposits in a foreign currency are converted into the account currency at the current
exchange rate (:mod:`trading_bot.fx`); the original amount and rate are kept in the
ledger.  Withdrawals debit the account immediately and create a payout; a failed payout
refunds the cash.  Accounts connected to a real brokerage cannot be funded here - their
cash lives at the broker.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .broker import AccountManager, Broker, BrokerError, PaperBroker
from .config import PaymentsConfig
from .engine import EventBus, WalletChanged
from .fx import FxError, FxService
from .payments import PaymentError, PaymentEvent, PaymentProvider
from .storage import Storage

logger = logging.getLogger(__name__)


class WalletError(ValueError):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


class WalletService:
    def __init__(self, storage: Storage, accounts: AccountManager, cfg: PaymentsConfig,
                 provider: PaymentProvider | None, fx: FxService | None = None, bus: EventBus | None = None) -> None:
        self.storage = storage
        self.accounts = accounts
        self.cfg = cfg
        self.provider = provider
        self.fx = fx
        self.bus = bus

    # ------------------------------------------------------------- queries
    @property
    def currency(self) -> str:
        return self.cfg.currency.upper()

    @property
    def enabled(self) -> bool:
        return self.provider is not None and self.provider.configured

    def summary(self, account: str, broker: Broker, prices: Mapping[str, float] | None = None) -> dict[str, Any]:
        pending = self.storage.transactions(account, status="pending", limit=50)
        totals = self.storage.transaction_totals(account)
        reserved = 0.0
        if isinstance(broker, PaperBroker):
            reserved = sum(o.quantity * o.limit_price for o in broker.pending_orders() if o.side == "BUY")
        try:
            cash = broker.cash()
            equity = broker.equity(dict(prices or {}))
        except BrokerError as exc:
            cash = equity = 0.0
            logger.warning("[%s] broker balance unavailable: %s", account, exc)
        return {
            "account": account, "currency": self.currency, "cash": cash, "equity": equity,
            "available": max(0.0, cash - reserved), "reserved": reserved,
            "funding_enabled": self.enabled and broker.supports_deposits,
            "funding_disabled_reason": None if self.enabled and broker.supports_deposits else (
                "Deposits and withdrawals are handled by your brokerage" if not broker.supports_deposits
                else "No payment provider configured (payments.provider)"),
            "provider": self.provider.name if self.provider else None,
            "min_amount": self.cfg.min_amount, "max_amount": self.cfg.max_amount,
            "pending": pending, "totals": totals,
            "net_deposits": totals["deposits"] - totals["withdrawals"],
        }

    # ------------------------------------------------------------ deposits
    def _check_amount(self, amount: float) -> None:
        if not amount or amount != amount or amount <= 0:
            raise WalletError("Amount must be positive")
        if amount < self.cfg.min_amount:
            raise WalletError(f"Minimum amount is {self.cfg.min_amount:,.2f} {self.currency}")
        if amount > self.cfg.max_amount:
            raise WalletError(f"Maximum amount is {self.cfg.max_amount:,.2f} {self.currency}")

    def _require_funding(self, broker: Broker) -> PaymentProvider:
        if not broker.supports_deposits:
            raise WalletError("This account is connected to a brokerage; fund it there", 409)
        if self.provider is None or not self.provider.configured:
            raise WalletError("No payment provider is configured", 503)
        return self.provider

    def quote_deposit(self, amount: float, currency: str) -> dict[str, Any]:
        """How much a deposit of ``amount`` ``currency`` credits in the account currency."""
        currency = (currency or self.currency).upper()
        if currency == self.currency:
            return {"source_amount": float(amount), "source_currency": currency, "amount": float(amount),
                    "currency": self.currency, "rate": 1.0, "rate_date": None}
        if self.fx is None:
            raise WalletError(f"Deposits in {currency} need exchange rates (fx.provider)", 503)
        try:
            conversion = self.fx.convert(amount, currency, self.currency)
        except FxError as exc:
            raise WalletError(f"Cannot convert {currency}: {exc}", 503) from exc
        return {"source_amount": float(amount), "source_currency": currency, "amount": round(conversion["result"], 2),
                "currency": self.currency, "rate": conversion["rate"], "rate_date": conversion["date"]}

    def start_deposit(self, account: str, broker: Broker, amount: float, currency: str | None, return_base: str,
                      description: str = "") -> dict[str, Any]:
        provider = self._require_funding(broker)
        quote = self.quote_deposit(amount, currency or self.currency)
        self._check_amount(quote["amount"])
        tx = self.storage.record_transaction(
            account, "deposit", quote["amount"], currency=self.currency, status="pending",
            description=description or f"Deposit via {provider.name}", source_amount=quote["source_amount"],
            source_currency=quote["source_currency"],
            metadata={"provider": provider.name, "rate": quote["rate"], "rate_date": quote["rate_date"]})
        base = return_base.rstrip("/")
        try:
            session = provider.create_checkout(
                amount=quote["source_amount"], currency=quote["source_currency"], transaction_id=tx["id"], account=account,
                success_url=f"{base}/?deposit={tx['id']}&result=success#wallet",
                cancel_url=f"{base}/?deposit={tx['id']}&result=cancelled#wallet",
                description=f"Trading Bot deposit for {account}")
        except PaymentError as exc:
            self.storage.update_transaction(tx["id"], status="failed", description=f"{tx['description']} - {exc}")
            raise WalletError(f"Payment provider error: {exc}", 502) from exc
        metadata = {**tx["metadata"], "session_url": session.url}
        tx = self.storage.update_transaction(tx["id"], reference=session.id, metadata=metadata) or tx
        logger.info("[%s] deposit %s started: %.2f %s -> %.2f %s via %s", account, tx["id"], quote["source_amount"],
                    quote["source_currency"], quote["amount"], self.currency, provider.name)
        return {"transaction": tx, "checkout_url": session.url, "session": session.to_dict()}

    def _paper(self, account: str) -> PaperBroker:
        return self.accounts.paper_account(account)

    def complete_deposit(self, transaction_id: str, detail: str = "") -> dict[str, Any]:
        tx = self.storage.transaction(transaction_id)
        if tx is None or tx["kind"] != "deposit":
            raise WalletError("Unknown deposit", 404)
        if tx["status"] == "completed":
            return tx
        if tx["status"] != "pending":
            raise WalletError(f"Deposit is {tx['status']}", 409)
        balance = self._paper(tx["account"]).deposit(float(tx["amount"]))
        updated = self.storage.update_transaction(tx["id"], status="completed", balance_after=balance,
                                                  metadata={**tx["metadata"], "detail": detail} if detail else tx["metadata"])
        assert updated is not None
        logger.info("[%s] deposit %s completed: +%.2f %s (balance %.2f)", tx["account"], tx["id"], tx["amount"],
                    tx["currency"], balance)
        self._publish(updated)
        return updated

    def cancel_deposit(self, transaction_id: str, reason: str = "cancelled") -> dict[str, Any]:
        tx = self.storage.transaction(transaction_id)
        if tx is None or tx["kind"] != "deposit":
            raise WalletError("Unknown deposit", 404)
        if tx["status"] != "pending":
            return tx
        updated = self.storage.update_transaction(tx["id"], status="cancelled" if reason == "cancelled" else "failed",
                                                  description=f"{tx['description']} - {reason}")
        assert updated is not None
        self._publish(updated)
        return updated

    def sync_deposit(self, transaction_id: str, account: str | None = None) -> dict[str, Any]:
        """Ask the provider for the checkout state (used when returning from the checkout page)."""
        tx = self.storage.transaction(transaction_id)
        if tx is None or tx["kind"] != "deposit" or (account is not None and tx["account"] != account):
            raise WalletError("Unknown deposit", 404)
        if tx["status"] != "pending" or not tx["reference"] or self.provider is None:
            return tx
        try:
            session = self.provider.checkout_status(tx["reference"])
        except PaymentError as exc:
            logger.warning("Could not verify deposit %s: %s", tx["id"], exc)
            return tx
        if session.status == "paid":
            return self.complete_deposit(tx["id"], detail="confirmed on return")
        if session.status in ("cancelled", "expired"):
            return self.cancel_deposit(tx["id"], session.status)
        return tx

    # --------------------------------------------------------- withdrawals
    def withdraw(self, account: str, broker: Broker, amount: float, destination: str = "") -> dict[str, Any]:
        provider = self._require_funding(broker)
        self._check_amount(amount)
        paper = self._paper(account)
        try:
            balance = paper.withdraw(amount)
        except BrokerError as exc:
            raise WalletError(str(exc), 409) from exc
        tx = self.storage.record_transaction(
            account, "withdrawal", -float(amount), currency=self.currency, status="pending",
            description=f"Withdrawal via {provider.name}" + (f" to {destination}" if destination else ""),
            balance_after=balance, metadata={"provider": provider.name, "destination": destination})
        try:
            payout = provider.create_payout(amount=amount, currency=self.currency, transaction_id=tx["id"],
                                            account=account, destination=destination)
        except PaymentError as exc:
            paper.deposit(amount)  # refund
            failed = self.storage.update_transaction(tx["id"], status="failed", balance_after=paper.cash(),
                                                     description=f"{tx['description']} - {exc}")
            assert failed is not None
            self._publish(failed)
            raise WalletError(f"Payment provider error: {exc}", 502) from exc
        status = "completed" if payout.status == "paid" else "pending"
        if payout.status == "failed":
            paper.deposit(amount)
            status = "failed"
        updated = self.storage.update_transaction(tx["id"], status=status, reference=payout.id, balance_after=paper.cash(),
                                                  metadata={**tx["metadata"], "payout_status": payout.status,
                                                            "detail": payout.detail})
        assert updated is not None
        logger.info("[%s] withdrawal %s %s: %.2f %s", account, tx["id"], status, amount, self.currency)
        self._publish(updated)
        return updated

    # ------------------------------------------------------------- webhooks
    def handle_webhook(self, body: bytes, headers: Mapping[str, str]) -> list[dict[str, Any]]:
        if self.provider is None:
            raise WalletError("No payment provider configured", 503)
        events = self.provider.parse_webhook(body, headers)
        return [self.apply_event(event) for event in events]

    def apply_event(self, event: PaymentEvent) -> dict[str, Any]:
        tx = (self.storage.transaction(event.transaction_id) if event.transaction_id else None) or \
            (self.storage.transaction_by_reference(event.reference) if event.reference else None)
        if event.kind == "ignored":
            return {"event": event.raw_type, "handled": False}
        if tx is None:
            logger.warning("Payment event %s for unknown transaction (%s / %s)", event.raw_type, event.transaction_id,
                           event.reference)
            return {"event": event.raw_type, "handled": False, "reason": "unknown transaction"}
        if event.kind == "deposit_paid":
            if event.amount is not None and tx["source_amount"] is not None and abs(event.amount - float(tx["source_amount"])) > 0.01:
                logger.error("Deposit %s amount mismatch: paid %.2f, expected %.2f", tx["id"], event.amount, tx["source_amount"])
                return {"event": event.raw_type, "handled": False, "reason": "amount mismatch", "transaction": tx["id"]}
            result = self.complete_deposit(tx["id"], detail=event.raw_type)
        elif event.kind == "deposit_cancelled":
            result = self.cancel_deposit(tx["id"], "expired")
        elif event.kind == "payout_paid":
            result = self.storage.update_transaction(tx["id"], status="completed") or tx if tx["status"] == "pending" else tx
            self._publish(result)
        elif event.kind == "payout_failed":
            if tx["status"] == "pending":
                paper = self._paper(tx["account"])
                paper.deposit(-float(tx["amount"]))
                result = self.storage.update_transaction(tx["id"], status="failed", balance_after=paper.cash(),
                                                         description=f"{tx['description']} - {event.detail or 'payout failed'}") or tx
                self._publish(result)
            else:
                result = tx
        else:  # pragma: no cover - exhaustive above
            return {"event": event.raw_type, "handled": False}
        return {"event": event.raw_type, "handled": True, "transaction": result["id"], "status": result["status"]}

    def _publish(self, tx: dict[str, Any]) -> None:
        if self.bus is not None:
            self.bus.publish(WalletChanged(tx["account"], tx["kind"], float(tx["amount"]), tx["status"]))


__all__ = ["WalletError", "WalletService"]
