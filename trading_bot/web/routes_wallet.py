"""Wallet API: balance, deposits (payment provider checkout), withdrawals, transaction history, webhooks."""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.requests import Request

from ..payments import MockPaymentProvider, PaymentError
from ..storage import TRANSACTION_KINDS, TRANSACTION_STATUSES
from ..wallet import WalletError
from .context import WebContext
from .paths import STATIC_DIR
from .users import User

logger = logging.getLogger(__name__)


class DepositBody(BaseModel):
    amount: float = Field(gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class WithdrawBody(BaseModel):
    amount: float = Field(gt=0)
    destination: str = Field(default="", max_length=120)


def build_wallet_router(ctx: WebContext) -> APIRouter:
    router = APIRouter(tags=["wallet"])
    require_auth = ctx.require_auth
    wallet = ctx.wallet

    def _wallet_error(exc: WalletError) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=str(exc))

    @router.get("/api/wallet")
    def get_wallet(user: User = Depends(require_auth)) -> dict[str, Any]:
        broker = ctx.account_of(user)
        data = wallet.summary(user.username, broker, ctx.engine.latest_prices())
        data["broker"] = broker.describe()
        data["recent"] = ctx.engine.storage.transactions(user.username, limit=10)
        data["currencies"] = sorted(ctx.fx.currencies()) if ctx.fx is not None else [wallet.currency]
        return data

    @router.get("/api/wallet/quote")
    def quote_deposit(amount: float = Query(gt=0), currency: str = Query(default="", max_length=3),
                      user: User = Depends(require_auth)) -> dict[str, Any]:
        try:
            return wallet.quote_deposit(amount, currency or wallet.currency)
        except WalletError as exc:
            raise _wallet_error(exc) from exc

    @router.post("/api/wallet/deposit", status_code=201)
    def start_deposit(body: DepositBody, request: Request, user: User = Depends(require_auth)) -> dict[str, Any]:
        """Create a pending deposit and the provider checkout session; the client redirects to ``checkout_url``."""
        try:
            return wallet.start_deposit(user.username, ctx.account_of(user), body.amount, body.currency,
                                        ctx.external_base(request))
        except WalletError as exc:
            raise _wallet_error(exc) from exc

    @router.post("/api/wallet/deposits/{transaction_id}/sync")
    def sync_deposit(transaction_id: str, user: User = Depends(require_auth)) -> dict[str, Any]:
        """Verify a deposit with the provider (called when the browser returns from the checkout page)."""
        try:
            return wallet.sync_deposit(transaction_id, account=user.username)
        except WalletError as exc:
            raise _wallet_error(exc) from exc

    @router.post("/api/wallet/withdraw", status_code=201)
    def withdraw(body: WithdrawBody, user: User = Depends(require_auth)) -> dict[str, Any]:
        try:
            return wallet.withdraw(user.username, ctx.account_of(user), body.amount, body.destination.strip())
        except WalletError as exc:
            raise _wallet_error(exc) from exc

    # --------------------------------------------------------- transactions
    @router.get("/api/transactions")
    def list_transactions(user: User = Depends(require_auth), kind: str | None = Query(default=None),
                          status: str | None = Query(default=None), days: int | None = Query(default=None, ge=1, le=3660),
                          limit: int = Query(default=500, ge=1, le=5000), format: str = Query(default="json")) -> Any:
        """The account ledger (deposits, withdrawals, trades); ``format=csv`` downloads it."""
        if kind is not None and kind not in TRANSACTION_KINDS:
            raise HTTPException(status_code=422, detail=f"kind must be one of {', '.join(TRANSACTION_KINDS)}")
        if status is not None and status not in TRANSACTION_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(TRANSACTION_STATUSES)}")
        since = datetime.now(UTC) - timedelta(days=days) if days else None
        rows = ctx.engine.storage.transactions(user.username, kind=kind, status=status, limit=limit, since=since)
        if format == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["id", "timestamp", "kind", "status", "amount", "currency", "fee", "symbol", "quantity", "price",
                             "balance_after", "description", "reference"])
            for row in rows:
                writer.writerow([row["id"], row["timestamp"], row["kind"], row["status"], row["amount"], row["currency"],
                                 row["fee"], row["symbol"] or "", row["quantity"] or "", row["price"] or "",
                                 row["balance_after"] if row["balance_after"] is not None else "", row["description"],
                                 row["reference"] or ""])
            filename = f"transactions-{user.username}-{datetime.now(UTC):%Y%m%d}.csv"
            return PlainTextResponse(buffer.getvalue(), media_type="text/csv",
                                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        return {"account": user.username, "currency": wallet.currency, "transactions": rows,
                "totals": ctx.engine.storage.transaction_totals(user.username)}

    # ------------------------------------------------------------- webhooks
    @router.post("/api/payments/webhook")
    async def payment_webhook(request: Request) -> dict[str, Any]:
        """Provider callbacks (Stripe); authenticated by the provider's signature, not by a session."""
        body = await request.body()
        try:
            results = wallet.handle_webhook(body, request.headers)
        except WalletError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        except PaymentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"received": True, "results": results}

    # ---------------------------------------------------------- mock checkout
    def _mock() -> MockPaymentProvider:
        if not isinstance(ctx.payments, MockPaymentProvider):
            raise HTTPException(status_code=404, detail="The sandbox checkout is only available with payments.provider=mock")
        return ctx.payments

    @router.get("/api/payments/mock/{session_id}")
    def mock_session(session_id: str, user: User = Depends(require_auth)) -> dict[str, Any]:
        provider = _mock()
        try:
            details = provider.session_details(session_id)
        except PaymentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if details["account"] != user.username and not user.is_admin:
            raise HTTPException(status_code=403, detail="This checkout belongs to another account")
        return details

    @router.post("/api/payments/mock/{session_id}/{action}")
    def mock_action(session_id: str, action: str, user: User = Depends(require_auth)) -> dict[str, Any]:
        """Sandbox: ``pay`` completes the deposit, ``cancel`` abandons it."""
        provider = _mock()
        try:
            details = provider.session_details(session_id)
        except PaymentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if details["account"] != user.username and not user.is_admin:
            raise HTTPException(status_code=403, detail="This checkout belongs to another account")
        if action == "pay":
            session = provider.complete(session_id)
            transaction = wallet.complete_deposit(session.transaction_id or "", detail="sandbox payment")
            redirect = details["success_url"]
        elif action == "cancel":
            session = provider.cancel(session_id)
            transaction = wallet.cancel_deposit(session.transaction_id or "")
            redirect = details["cancel_url"]
        else:
            raise HTTPException(status_code=404, detail="Unknown action")
        return {"session": session.to_dict(), "transaction": transaction, "redirect": redirect}

    @router.get("/pay/mock/{session_id}", include_in_schema=False)
    def mock_checkout_page(session_id: str) -> Response:
        _mock()
        page = STATIC_DIR / "pay.html"
        if not page.exists():  # pragma: no cover
            return RedirectResponse("../../", status_code=302)
        return FileResponse(page, headers={"Cache-Control": "no-store"})

    return router


__all__ = ["build_wallet_router"]
