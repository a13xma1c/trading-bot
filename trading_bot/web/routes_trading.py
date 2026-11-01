"""Manual trading API: order ticket (simulate / place), positions, pending orders, broker connection."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..broker import Broker, BrokerError
from ..brokers import BROKER_PROVIDERS, create_broker
from ..engine import PositionsChanged
from ..execution import OrderRequest
from ..trading import OrderError
from .context import WebContext
from .users import User, UserError

logger = logging.getLogger(__name__)


class OrderBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    side: str = Field(min_length=3, max_length=4)
    quantity: float | None = Field(default=None, gt=0)
    notional: float | None = Field(default=None, gt=0)   # alternative to quantity: amount of cash to spend
    order_type: str = "market"
    limit_price: float | None = Field(default=None, gt=0)
    stop_loss_pct: float | None = Field(default=None, ge=0, lt=100)
    take_profit_pct: float | None = Field(default=None, ge=0, lt=100)


class BrokerConnectionBody(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    key: str | None = Field(default=None, max_length=256)
    secret: str | None = Field(default=None, max_length=256)
    paper: bool = True


def pending_payload(broker: Broker) -> list[dict[str, Any]]:
    try:
        return [o.to_dict() for o in broker.pending_orders()]
    except BrokerError as exc:
        logger.warning("pending orders unavailable: %s", exc)
        return []


def build_trading_router(ctx: WebContext) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["trading"])
    require_auth = ctx.require_auth

    def _request(body: OrderBody, broker: Broker) -> OrderRequest:
        quantity = body.quantity
        if quantity is None:
            if body.notional is None:
                raise HTTPException(status_code=422, detail="Provide a quantity or a notional amount")
            snapshot = ctx.orders.snapshot(body.symbol)
            price = body.limit_price if body.order_type == "limit" and body.limit_price else snapshot.price
            quantity = float(int(body.notional // price)) if price > 0 else 0.0
            if quantity < 1:
                raise HTTPException(status_code=422, detail=f"{body.notional:,.2f} does not buy one share at {price:,.2f}")
        return OrderRequest(body.symbol, body.side, quantity, body.order_type, body.limit_price, body.stop_loss_pct,
                            body.take_profit_pct, "manual")

    def _order_error(exc: OrderError) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=str(exc))

    @router.post("/orders/simulate")
    def simulate_order(body: OrderBody, user: User = Depends(require_auth)) -> dict[str, Any]:
        """Dry-run an order: expected fill price, slippage, commission, cash and position afterwards."""
        broker = ctx.account_of(user)
        try:
            request = _request(body, broker)
            result = ctx.orders.simulate(broker, request)
        except OrderError as exc:
            raise _order_error(exc) from exc
        data = result.to_dict()
        data["broker"] = broker.describe()
        return data

    @router.post("/orders", status_code=201)
    def place_order(body: OrderBody, user: User = Depends(require_auth)) -> dict[str, Any]:
        """Execute an order in the user's account (market: immediately; limit: rests until marketable)."""
        broker = ctx.account_of(user)
        try:
            request = _request(body, broker)
            result = ctx.orders.place(user.username, broker, request)
        except OrderError as exc:
            raise _order_error(exc) from exc
        data = result.to_dict()
        data["pending_orders"] = pending_payload(broker)
        return data

    @router.get("/orders")
    def list_orders(user: User = Depends(require_auth)) -> list[dict[str, Any]]:
        return pending_payload(ctx.account_of(user))

    @router.delete("/orders/{order_id}")
    def cancel_order(order_id: str, user: User = Depends(require_auth)) -> dict[str, Any]:
        broker = ctx.account_of(user)
        try:
            order = ctx.orders.cancel_order(user.username, broker, order_id)
        except OrderError as exc:
            raise _order_error(exc) from exc
        return {"cancelled": order.to_dict(), "pending_orders": pending_payload(broker)}

    @router.delete("/positions/{symbol}")
    def close_position(symbol: str, user: User = Depends(require_auth)) -> dict[str, Any]:
        """Sell the whole position at the current price."""
        broker = ctx.account_of(user)
        try:
            result = ctx.orders.close_position(user.username, broker, symbol)
        except OrderError as exc:
            raise _order_error(exc) from exc
        return result.to_dict()

    # ------------------------------------------------------------ broker
    @router.get("/broker/providers")
    def broker_providers(user: User = Depends(require_auth)) -> dict[str, Any]:
        return {"providers": [{"name": name, **info} for name, info in BROKER_PROVIDERS.items()]}

    @router.get("/broker/connection")
    def broker_connection(user: User = Depends(require_auth), check: bool = Query(default=False)) -> dict[str, Any]:
        """The user's execution venue; ``check=true`` also verifies the credentials with the brokerage."""
        broker = ctx.account_of(user)
        data: dict[str, Any] = {**user.broker_public(), "active": broker.describe(), "status": None, "error": None}
        if check and user.broker_connected:
            try:
                data["status"] = getattr(broker, "test_connection", lambda: {"ok": True})()
            except BrokerError as exc:
                data["error"] = str(exc)
        return data

    @router.put("/broker/connection")
    def set_broker_connection(body: BrokerConnectionBody, user: User = Depends(require_auth)) -> dict[str, Any]:
        """Connect (or disconnect with provider=paper) a brokerage; credentials are verified first."""
        if user.id == 0:
            raise HTTPException(status_code=403, detail="Not available for API-token access")
        try:
            updated = ctx.users.set_broker_connection(user, body.provider, body.key, body.secret, body.paper)
        except UserError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        try:
            broker = create_broker(updated.broker_provider, updated.broker_key or "", updated.broker_secret or "",
                                   updated.broker_paper)
            status = None
            if broker is not None:
                status = getattr(broker, "test_connection", lambda: {"ok": True})()
        except BrokerError as exc:
            # Roll back to the previous connection so a typo never leaves the account without a venue.
            ctx.users.set_broker_connection(updated, user.broker_provider, user.broker_key, user.broker_secret, user.broker_paper)
            raise HTTPException(status_code=502, detail=f"Could not connect: {exc}") from exc
        active = ctx.engine.accounts.set_broker(updated.username, broker)
        ctx.engine.bus.publish(PositionsChanged(updated.username))
        logger.info("User %r now trades through %s", updated.username, active.describe()["label"])
        return {**updated.broker_public(), "active": active.describe(), "status": status, "error": None}

    return router


__all__ = ["build_trading_router", "pending_payload"]
