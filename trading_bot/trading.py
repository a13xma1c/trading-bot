"""Manual trading on top of the engine: order ticket, simulation, closing positions.

The engine executes *signals*; this module lets a user add or remove positions
directly (market or limit orders) in their own account - the paper account or a
connected brokerage - with the same ledger, events and execution model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .broker import Broker, BrokerError, PaperBroker, PendingOrder
from .engine import OrderEvent, PositionsChanged, TradingEngine
from .execution import MarketSnapshot, OrderRequest, SimulationResult

logger = logging.getLogger(__name__)


class OrderError(ValueError):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class OrderResult:
    status: str                      # filled | pending
    simulation: SimulationResult
    fill: dict[str, Any] | None = None
    order: dict[str, Any] | None = None
    transaction: dict[str, Any] | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "simulation": self.simulation.to_dict(), "fill": self.fill, "order": self.order,
                "transaction": self.transaction, "message": self.message, "warnings": self.warnings}


class OrderService:
    def __init__(self, engine: TradingEngine) -> None:
        self.engine = engine

    # --------------------------------------------------------------- market
    def snapshot(self, symbol: str) -> MarketSnapshot:
        """Latest price / liquidity for ``symbol``; fetches once when the symbol is not cached."""
        symbol = symbol.upper()
        try:
            return self.engine.market_snapshot(symbol)
        except ValueError:
            pass
        result = self.engine.fetch_symbol(symbol)
        if result is None or result.empty:
            raise OrderError(f"No market data for {symbol}: {self.engine.last_error(symbol) or 'unknown symbol'}", 409)
        return MarketSnapshot.from_bars(result.bars)

    # ------------------------------------------------------------- simulate
    def simulate(self, broker: Broker, request: OrderRequest) -> SimulationResult:
        problem = request.validate()
        if problem:
            raise OrderError(problem)
        snapshot = self.snapshot(request.symbol)
        return self.engine.execution.simulate(request, snapshot, broker, self.engine.config.trading.max_positions)

    # ----------------------------------------------------------------- place
    def place(self, account: str, broker: Broker, request: OrderRequest) -> OrderResult:
        simulation = self.simulate(broker, request)
        if not simulation.accepted:
            raise OrderError(simulation.reason, 409)
        try:
            if request.order_type == "limit" and not simulation.marketable:
                assert request.limit_price is not None
                kwargs: dict[str, Any] = {}
                if isinstance(broker, PaperBroker):
                    kwargs["expires_after_bars"] = self.engine.config.execution.limit_order_expiry_bars
                order = broker.submit_limit_order(request.symbol, request.side, request.quantity, request.limit_price,
                                                  request.strategy, simulation.stop_loss, simulation.take_profit, **kwargs)
                message = f"Limit order placed: {request.side} {request.quantity:g} {request.symbol} @ {request.limit_price:.2f}"
                self.engine.bus.publish(OrderEvent(account, "placed", request.symbol, request.side, request.quantity,
                                                   request.limit_price, message))
                return OrderResult("pending", simulation, order=order.to_dict(), message=message,
                                   warnings=simulation.warnings)
            if isinstance(broker, PaperBroker):
                fill = broker.submit_market_order(request.symbol, request.side, request.quantity, simulation.fill_price,
                                                  request.strategy, simulation.stop_loss, simulation.take_profit,
                                                  commission=simulation.commission)
            else:
                fill = broker.submit_market_order(request.symbol, request.side, request.quantity, simulation.reference_price,
                                                  request.strategy, simulation.stop_loss, simulation.take_profit)
        except BrokerError as exc:
            self.engine.bus.publish(OrderEvent(account, "rejected", request.symbol, request.side, request.quantity,
                                               None, str(exc)))
            raise OrderError(str(exc), 409) from exc
        transaction = self.engine.record_fill(account, broker, fill, source="manual")
        message = f"{fill.side} {fill.quantity:g} {fill.symbol} filled @ {fill.price:.2f}"
        self.engine.bus.publish(OrderEvent(account, "filled", fill.symbol, fill.side, fill.quantity, fill.price, message))
        self.engine.bus.publish(PositionsChanged(account))
        return OrderResult("filled", simulation, fill=_fill_dict(fill), transaction=transaction, message=message,
                           warnings=simulation.warnings)

    def close_position(self, account: str, broker: Broker, symbol: str) -> OrderResult:
        symbol = symbol.upper()
        pos = broker.position(symbol)
        if pos is None:
            raise OrderError(f"No open position in {symbol}", 404)
        request = OrderRequest(symbol, "SELL", pos.quantity, strategy=pos.strategy or "manual")
        return self.place(account, broker, request)

    def cancel_order(self, account: str, broker: Broker, order_id: str) -> PendingOrder:
        match = next((o for o in broker.pending_orders() if o.id == order_id), None)
        if match is None:
            raise OrderError("No such pending order", 404)
        try:
            cancelled = broker.cancel_order(order_id)
        except BrokerError as exc:
            raise OrderError(str(exc), 409) from exc
        if not cancelled:
            raise OrderError("The order could not be cancelled (already filled?)", 409)
        self.engine.bus.publish(OrderEvent(account, "cancelled", match.symbol, match.side, match.quantity,
                                           match.limit_price, f"Order cancelled: {match.side} {match.quantity:g} {match.symbol}"))
        return match


def _fill_dict(fill: Any) -> dict[str, Any]:
    return {"id": fill.id, "symbol": fill.symbol, "side": fill.side, "quantity": fill.quantity, "price": fill.price,
            "timestamp": fill.timestamp.isoformat(), "strategy": fill.strategy, "realized_pnl": fill.realized_pnl,
            "commission": fill.commission}


__all__ = ["OrderError", "OrderResult", "OrderService"]
