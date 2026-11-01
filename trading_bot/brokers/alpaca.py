"""Alpaca Trading API adapter (https://docs.alpaca.markets/reference).

Works against the free paper-trading environment (``paper=True``,
``https://paper-api.alpaca.markets``) or live trading.  Only the endpoints the engine
needs are used: account, positions, orders (market / limit / bracket), close position.
All failures surface as :class:`BrokerError` so the engine treats them like any other
rejected order.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from ..broker import Broker, BrokerError, Fill, PendingOrder, Position

logger = logging.getLogger(__name__)

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
FILLED = "filled"
TERMINAL = {"filled", "canceled", "expired", "rejected", "suspended", "stopped"}


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, key: str, secret: str, paper: bool = True, client: httpx.Client | None = None,
                 base_url: str | None = None, fill_timeout: float = 10.0, poll_interval: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.paper = paper
        self.base_url = (base_url or (PAPER_URL if paper else LIVE_URL)).rstrip("/")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
        self._client = client or httpx.Client(timeout=15.0)
        self._owns_client = client is None
        self.fill_timeout = fill_timeout
        self.poll_interval = poll_interval
        self._sleep = sleep

    # ------------------------------------------------------------- plumbing
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, self.base_url + path, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise BrokerError(f"Alpaca unreachable: {exc}") from exc
        if response.status_code == 401 or response.status_code == 403:
            raise BrokerError("Alpaca rejected the API credentials")
        if response.status_code >= 400:
            try:
                detail = response.json().get("message") or response.text
            except ValueError:
                detail = response.text
            raise BrokerError(f"Alpaca error {response.status_code}: {str(detail)[:200]}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise BrokerError("Alpaca returned an unreadable response") from exc

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -------------------------------------------------------------- account
    @property
    def is_paper(self) -> bool:
        return self.paper

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "paper": self.paper, "label": f"Alpaca ({'paper' if self.paper else 'LIVE'})"}

    def account(self) -> dict[str, Any]:
        data = self._request("GET", "/v2/account")
        if not isinstance(data, dict):
            raise BrokerError("Alpaca returned no account data")
        return data

    def test_connection(self) -> dict[str, Any]:
        """Connectivity / credential check; returns a summary for the settings page."""
        acct = self.account()
        return {"account_number": acct.get("account_number"), "status": acct.get("status"),
                "currency": acct.get("currency", "USD"), "cash": _num(acct.get("cash")),
                "equity": _num(acct.get("equity")), "buying_power": _num(acct.get("buying_power")),
                "paper": self.paper, "trading_blocked": bool(acct.get("trading_blocked"))}

    def cash(self) -> float:
        return _num(self.account().get("cash"))

    def equity(self, prices: dict[str, float]) -> float:
        return _num(self.account().get("equity"))

    def positions(self) -> dict[str, Position]:
        rows = self._request("GET", "/v2/positions") or []
        out: dict[str, Position] = {}
        for row in rows:
            qty = _num(row.get("qty"))
            if row.get("side") == "short":
                qty = -abs(qty)
            symbol = str(row.get("symbol", "")).upper()
            out[symbol] = Position(symbol, qty, _num(row.get("avg_entry_price")), datetime.now(UTC))
        return out

    # --------------------------------------------------------------- orders
    def submit_market_order(self, symbol, side, quantity, price, strategy="", stop_loss=None, take_profit=None) -> Fill:
        symbol = symbol.upper()
        body: dict[str, Any] = {"symbol": symbol, "qty": _qty(quantity), "side": side.lower(), "type": "market",
                                "time_in_force": "day"}
        if side.upper() == "BUY" and (stop_loss or take_profit):
            body["order_class"] = "bracket" if stop_loss and take_profit else "oto"
            if stop_loss:
                body["stop_loss"] = {"stop_price": f"{stop_loss:.2f}"}
            if take_profit:
                body["take_profit"] = {"limit_price": f"{take_profit:.2f}"}
        before = self.position(symbol) if side.upper() == "SELL" else None
        order = self._request("POST", "/v2/orders", json=body)
        return self._fill_from_order(order, price, strategy, before)

    def submit_limit_order(self, symbol, side, quantity, limit_price, strategy="", stop_loss=None, take_profit=None) -> PendingOrder:
        body: dict[str, Any] = {"symbol": symbol.upper(), "qty": _qty(quantity), "side": side.lower(), "type": "limit",
                                "time_in_force": "gtc", "limit_price": f"{limit_price:.2f}"}
        if side.upper() == "BUY" and stop_loss and take_profit:
            body.update(order_class="bracket", stop_loss={"stop_price": f"{stop_loss:.2f}"},
                        take_profit={"limit_price": f"{take_profit:.2f}"})
        order = self._request("POST", "/v2/orders", json=body)
        return _pending_from_order(order, strategy)

    def pending_orders(self) -> list[PendingOrder]:
        rows = self._request("GET", "/v2/orders", params={"status": "open", "limit": 100}) or []
        return [_pending_from_order(row, "") for row in rows if row.get("type") in ("limit", "stop", "stop_limit")]

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._request("DELETE", f"/v2/orders/{order_id}")
        except BrokerError as exc:
            if "404" in str(exc) or "422" in str(exc):
                return False
            raise
        return True

    def close_position(self, symbol: str, price: float, strategy: str = "") -> Fill:
        symbol = symbol.upper()
        before = self.position(symbol)
        if before is None:
            raise BrokerError(f"No open position in {symbol}")
        order = self._request("DELETE", f"/v2/positions/{symbol}")
        return self._fill_from_order(order, price, strategy, before)

    # -------------------------------------------------------------- helpers
    def _fill_from_order(self, order: Any, reference_price: float, strategy: str, before: Position | None) -> Fill:
        if not isinstance(order, dict) or "id" not in order:
            raise BrokerError("Alpaca did not return an order")
        order = self._wait_for_fill(order)
        status = str(order.get("status", ""))
        filled_qty = _num(order.get("filled_qty"))
        if status in TERMINAL and status != FILLED and filled_qty <= 0:
            raise BrokerError(f"Alpaca order {status}: {order.get('reject_reason') or ''}".strip())
        qty = filled_qty or _num(order.get("qty"))
        price = _num(order.get("filled_avg_price")) or float(reference_price)
        side = str(order.get("side", "")).upper()
        pnl = 0.0
        if side == "SELL" and before is not None:
            pnl = (price - before.avg_price) * qty
        when = _time(order.get("filled_at") or order.get("submitted_at"))
        if status != FILLED:
            logger.warning("Alpaca order %s still %s after %.0fs; recording requested quantity at %.2f",
                           order["id"], status, self.fill_timeout, price)
        return Fill(str(order.get("symbol", "")).upper(), side, qty, price, when, strategy, realized_pnl=pnl,
                    id=str(order["id"])[:12])

    def _wait_for_fill(self, order: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self.fill_timeout
        current = order
        while str(current.get("status")) not in TERMINAL and time.monotonic() < deadline:
            self._sleep(self.poll_interval)
            refreshed = self._request("GET", f"/v2/orders/{order['id']}")
            if isinstance(refreshed, dict):
                current = refreshed
        return current


def _num(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _qty(quantity: float) -> str:
    return str(int(quantity)) if float(quantity).is_integer() else f"{quantity:.4f}"


def _time(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def _pending_from_order(order: Any, strategy: str) -> PendingOrder:
    if not isinstance(order, dict) or "id" not in order:
        raise BrokerError("Alpaca did not return an order")
    return PendingOrder(str(order.get("symbol", "")).upper(), str(order.get("side", "")).upper(), _num(order.get("qty")),
                        _num(order.get("limit_price") or order.get("stop_price")), _time(order.get("submitted_at")),
                        strategy, id=str(order["id"]))
