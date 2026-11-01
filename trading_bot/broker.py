"""Order execution.

The bot's primary job is to *notify* when a trade should be executed.  Actual
execution goes through the :class:`Broker` interface; the bundled
:class:`PaperBroker` simulates fills so strategies can be evaluated with real
market data and zero risk.  A live broker adapter implements the same
interface.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BrokerError(RuntimeError):
    pass


@dataclass
class Position:
    symbol: str
    quantity: float
    avg_price: float
    opened_at: datetime
    strategy: str = ""
    stop_loss: float | None = None
    take_profit: float | None = None

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_price) * self.quantity

    def unrealized_pnl_pct(self, price: float) -> float:
        return (price / self.avg_price - 1.0) * 100.0 if self.avg_price else 0.0


@dataclass
class Fill:
    symbol: str
    side: str            # BUY / SELL
    quantity: float
    price: float
    timestamp: datetime
    strategy: str = ""
    realized_pnl: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    commission: float = 0.0


@dataclass
class PendingOrder:
    """A resting limit order of the paper broker (filled when a later bar trades through the limit)."""

    symbol: str
    side: str
    quantity: float
    limit_price: float
    created_at: datetime
    strategy: str = ""
    stop_loss: float | None = None
    take_profit: float | None = None
    expires_after_bars: int = 0     # 0 = good till cancelled
    bars_seen: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


class Broker(ABC):
    """Execution venue of one account.

    Only market orders, positions and cash are mandatory; the other operations have
    conservative defaults (``BrokerError`` / empty) so adapters can implement them
    incrementally.
    """

    name: str = "broker"

    @abstractmethod
    def submit_market_order(self, symbol: str, side: str, quantity: float, price: float,
                            strategy: str = "", stop_loss: float | None = None,
                            take_profit: float | None = None) -> Fill: ...

    @abstractmethod
    def positions(self) -> dict[str, Position]: ...

    @abstractmethod
    def cash(self) -> float: ...

    def position(self, symbol: str) -> Position | None:
        return self.positions().get(symbol.upper())

    def equity(self, prices: dict[str, float]) -> float:
        total = self.cash()
        for sym, pos in self.positions().items():
            total += pos.market_value(prices.get(sym, pos.avg_price))
        return total

    # ------------------------------------------------------------ optional
    @property
    def is_paper(self) -> bool:
        return True

    @property
    def supports_deposits(self) -> bool:
        """Whether cash can be added/withdrawn through the app (paper accounts only)."""
        return False

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "paper": self.is_paper, "label": self.name}

    def submit_limit_order(self, symbol: str, side: str, quantity: float, limit_price: float, strategy: str = "",
                           stop_loss: float | None = None, take_profit: float | None = None) -> PendingOrder:
        raise BrokerError(f"{self.name} does not support limit orders")

    def pending_orders(self) -> list[PendingOrder]:
        return []

    def cancel_order(self, order_id: str) -> bool:
        return False

    def process_pending(self, symbol: str, high: float, low: float, when: datetime) -> list[Fill]:
        """Fill resting orders that a new bar traded through (paper accounts)."""
        return []

    def close_position(self, symbol: str, price: float, strategy: str = "") -> Fill:
        pos = self.position(symbol)
        if pos is None:
            raise BrokerError(f"No open position in {symbol.upper()}")
        return self.submit_market_order(symbol, "SELL", pos.quantity, price, strategy or pos.strategy)


class PaperBroker(Broker):
    """In-memory simulated broker with optional JSON persistence.

    Supports market orders, resting limit orders (filled by :meth:`process_pending` when a
    later bar trades through the limit), deposits/withdrawals and per-order commissions.
    """

    name = "paper"

    def __init__(self, initial_cash: float = 100_000.0, state_file: str | os.PathLike[str] | None = None,
                 commission: float = 0.0):
        self._cash = float(initial_cash)
        self.initial_cash = float(initial_cash)
        self.commission = float(commission)
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._pending: list[PendingOrder] = []
        self._lock = threading.RLock()
        self._state_file = Path(state_file) if state_file else None
        self._load()

    @property
    def supports_deposits(self) -> bool:
        return True

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "paper": True, "label": "Paper account (simulated)"}

    # ------------------------------------------------------------ Broker API
    def submit_market_order(self, symbol, side, quantity, price, strategy="", stop_loss=None, take_profit=None,
                            commission: float | None = None, when: datetime | None = None) -> Fill:
        symbol = symbol.upper()
        side = side.upper()
        if quantity <= 0:
            raise BrokerError("quantity must be positive")
        if price <= 0:
            raise BrokerError("price must be positive")
        fee = self.commission if commission is None else max(0.0, float(commission))
        with self._lock:
            now = when or datetime.now(UTC)
            if side == "BUY":
                cost = quantity * price + fee
                if cost > self._cash + 1e-9:
                    raise BrokerError(f"Insufficient cash: need {cost:.2f}, have {self._cash:.2f}")
                self._cash -= cost
                pos = self._positions.get(symbol)
                if pos is None:
                    self._positions[symbol] = Position(symbol, quantity, price, now, strategy, stop_loss, take_profit)
                else:
                    total_qty = pos.quantity + quantity
                    pos.avg_price = (pos.avg_price * pos.quantity + price * quantity) / total_qty
                    pos.quantity = total_qty
                    pos.stop_loss = stop_loss if stop_loss is not None else pos.stop_loss
                    pos.take_profit = take_profit if take_profit is not None else pos.take_profit
                fill = Fill(symbol, side, quantity, price, now, strategy, commission=fee)
            elif side == "SELL":
                pos = self._positions.get(symbol)
                if pos is None or pos.quantity < quantity - 1e-9:
                    raise BrokerError(f"Cannot sell {quantity} {symbol}: no such position (short selling disabled)")
                pnl = (price - pos.avg_price) * quantity - fee
                self._cash += quantity * price - fee
                pos.quantity -= quantity
                if pos.quantity <= 1e-9:
                    del self._positions[symbol]
                fill = Fill(symbol, side, quantity, price, now, strategy, realized_pnl=pnl, commission=fee)
            else:
                raise BrokerError(f"Unknown side {side!r}")
            self._fills.append(fill)
            self._save()
        logger.info("Paper fill: %s %s %.4f @ %.2f (pnl %.2f)", side, symbol, quantity, price, fill.realized_pnl)
        return fill

    def submit_limit_order(self, symbol, side, quantity, limit_price, strategy="", stop_loss=None, take_profit=None,
                           expires_after_bars: int = 0) -> PendingOrder:
        symbol = symbol.upper()
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise BrokerError(f"Unknown side {side!r}")
        if quantity <= 0 or limit_price <= 0:
            raise BrokerError("quantity and limit price must be positive")
        with self._lock:
            if side == "BUY":
                reserved = sum(o.quantity * o.limit_price for o in self._pending if o.side == "BUY")
                if quantity * limit_price + self.commission + reserved > self._cash + 1e-9:
                    raise BrokerError(f"Insufficient cash to place the order: need {quantity * limit_price:.2f} "
                                      f"(plus {reserved:.2f} reserved by other orders), have {self._cash:.2f}")
            else:
                pos = self._positions.get(symbol)
                resting = sum(o.quantity for o in self._pending if o.side == "SELL" and o.symbol == symbol)
                if pos is None or pos.quantity < quantity + resting - 1e-9:
                    raise BrokerError(f"Cannot sell {quantity} {symbol}: position too small (short selling disabled)")
            order = PendingOrder(symbol, side, quantity, limit_price, datetime.now(UTC), strategy, stop_loss,
                                 take_profit, expires_after_bars)
            self._pending.append(order)
            self._save()
        logger.info("Paper limit order: %s %s %.4f @ %.2f", side, symbol, quantity, limit_price)
        return order

    def pending_orders(self) -> list[PendingOrder]:
        with self._lock:
            return list(self._pending)

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            before = len(self._pending)
            self._pending = [o for o in self._pending if o.id != order_id]
            if len(self._pending) != before:
                self._save()
                return True
        return False

    def process_pending(self, symbol: str, high: float, low: float, when: datetime) -> list[Fill]:
        """Fill resting orders of ``symbol`` that the bar (high/low) traded through; expire stale ones."""
        symbol = symbol.upper()
        fills: list[Fill] = []
        with self._lock:
            remaining: list[PendingOrder] = []
            for order in self._pending:
                if order.symbol != symbol:
                    remaining.append(order)
                    continue
                order.bars_seen += 1
                marketable = (order.side == "BUY" and low <= order.limit_price) or \
                             (order.side == "SELL" and high >= order.limit_price)
                if marketable:
                    try:
                        fills.append(self.submit_market_order(symbol, order.side, order.quantity, order.limit_price,
                                                              order.strategy, order.stop_loss, order.take_profit,
                                                              when=when))
                    except BrokerError as exc:
                        logger.warning("Pending %s %s cancelled: %s", order.side, symbol, exc)
                    continue
                if order.expires_after_bars and order.bars_seen >= order.expires_after_bars:
                    logger.info("Pending %s %s @ %.2f expired", order.side, symbol, order.limit_price)
                    continue
                remaining.append(order)
            if len(remaining) != len(self._pending) or fills:
                self._pending = remaining
                self._save()
            elif any(o.symbol == symbol for o in remaining):
                self._save()  # bars_seen changed
        return fills

    def positions(self) -> dict[str, Position]:
        with self._lock:
            return dict(self._positions)

    def cash(self) -> float:
        with self._lock:
            return self._cash

    def fills(self) -> list[Fill]:
        with self._lock:
            return list(self._fills)

    def realized_pnl(self) -> float:
        with self._lock:
            return sum(f.realized_pnl for f in self._fills)

    # ------------------------------------------------------------ cash flows
    def deposit(self, amount: float) -> float:
        """Add cash; returns the new balance."""
        if amount <= 0:
            raise BrokerError("deposit amount must be positive")
        with self._lock:
            self._cash += float(amount)
            self._save()
            return self._cash

    def withdraw(self, amount: float) -> float:
        """Remove cash (only what is not invested or reserved); returns the new balance."""
        if amount <= 0:
            raise BrokerError("withdrawal amount must be positive")
        with self._lock:
            reserved = sum(o.quantity * o.limit_price for o in self._pending if o.side == "BUY")
            available = self._cash - reserved
            if amount > available + 1e-9:
                raise BrokerError(f"Insufficient cash: {available:.2f} available for withdrawal")
            self._cash -= float(amount)
            self._save()
            return self._cash

    def reset(self) -> None:
        with self._lock:
            self._cash = self.initial_cash
            self._positions.clear()
            self._fills.clear()
            self._pending.clear()
            self._save()

    # ------------------------------------------------------------ persistence
    def _save(self) -> None:
        if not self._state_file:
            return
        data = {
            "cash": self._cash,
            "initial_cash": self.initial_cash,
            "positions": [_serialize(p) for p in self._positions.values()],
            "fills": [_serialize(f) for f in self._fills[-500:]],
            "pending_orders": [_serialize(o) for o in self._pending],
        }
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self._state_file)
        except OSError as exc:  # pragma: no cover
            logger.warning("Could not persist broker state: %s", exc)

    def _load(self) -> None:
        if not self._state_file or not self._state_file.exists():
            return
        try:
            with self._state_file.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._cash = float(data.get("cash", self._cash))
            self.initial_cash = float(data.get("initial_cash", self.initial_cash))
            self._positions = {}
            for raw in data.get("positions", []):
                raw["opened_at"] = datetime.fromisoformat(raw["opened_at"])
                pos = Position(**raw)
                self._positions[pos.symbol] = pos
            self._fills = []
            for raw in data.get("fills", []):
                raw["timestamp"] = datetime.fromisoformat(raw["timestamp"])
                self._fills.append(Fill(**raw))
            self._pending = []
            for raw in data.get("pending_orders", []):
                raw["created_at"] = datetime.fromisoformat(raw["created_at"])
                self._pending.append(PendingOrder(**raw))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Could not load broker state from %s: %s", self._state_file, exc)


def _serialize(obj) -> dict[str, Any]:
    d = asdict(obj)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


class AccountManager:
    """One account per user.

    Every user owns a paper account persisted as ``<data_dir>/accounts/<name>.json``; a
    user may *connect* an external broker (e.g. Alpaca), which then replaces the paper
    account as the execution venue until it is disconnected.  The engine executes each
    signal in every account whose auto-trading is enabled, so users share the strategies
    but keep independent positions, cash and trade history.  ``default`` is the account
    used by the command line tools (it belongs to the administrator).
    """

    def __init__(self, initial_cash: float = 100_000.0, data_dir: str | os.PathLike[str] | None = None,
                 default_name: str = "admin", commission: float = 0.0) -> None:
        self.initial_cash = float(initial_cash)
        self.data_dir = Path(data_dir) / "accounts" if data_dir is not None else None
        self.default_name = default_name
        self.commission = commission
        self._accounts: dict[str, Broker] = {}     # the active venue per user
        self._paper: dict[str, PaperBroker] = {}   # every user's paper account (kept while an external broker is active)
        self._enabled: dict[str, bool] = {}
        self._lock = threading.RLock()

    def _paper_for(self, name: str, broker: PaperBroker | None = None) -> PaperBroker:
        paper = self._paper.get(name)
        if paper is None:
            if broker is not None:
                paper = broker
            else:
                state = self.data_dir / f"{_safe_name(name)}.json" if self.data_dir else None
                paper = PaperBroker(self.initial_cash, state_file=state, commission=self.commission)
            self._paper[name] = paper
        return paper

    def ensure(self, name: str, enabled: bool | None = None, broker: Broker | None = None) -> Broker:
        """Return the account ``name``, creating it if needed (``broker`` seeds a new account)."""
        with self._lock:
            account = self._accounts.get(name)
            if account is None:
                if isinstance(broker, PaperBroker) or broker is None:
                    account = self._paper_for(name, broker)
                else:
                    self._paper_for(name)
                    account = broker
                self._accounts[name] = account
                self._enabled.setdefault(name, True if enabled is None else enabled)
            elif enabled is not None:
                self._enabled[name] = enabled
            return account

    def set_broker(self, name: str, broker: Broker | None) -> Broker:
        """Connect an external broker to account ``name`` (``None`` reverts to the paper account)."""
        with self._lock:
            self.ensure(name)
            account = self._paper_for(name) if broker is None else broker
            self._accounts[name] = account
            return account

    def paper_account(self, name: str) -> PaperBroker:
        with self._lock:
            self.ensure(name)
            return self._paper_for(name)

    def get(self, name: str) -> Broker | None:
        with self._lock:
            return self._accounts.get(name)

    @property
    def default(self) -> Broker:
        return self.ensure(self.default_name)

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            self.ensure(name)
            self._enabled[name] = enabled

    def is_enabled(self, name: str) -> bool:
        with self._lock:
            return self._enabled.get(name, False)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._accounts)

    def enabled(self) -> list[tuple[str, Broker]]:
        with self._lock:
            return [(name, broker) for name, broker in self._accounts.items() if self._enabled.get(name)]

    def all(self) -> list[tuple[str, Broker]]:
        with self._lock:
            return list(self._accounts.items())

    def remove(self, name: str) -> None:
        with self._lock:
            self._accounts.pop(name, None)
            self._paper.pop(name, None)
            self._enabled.pop(name, None)


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name) or "account"
