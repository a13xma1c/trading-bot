"""External broker adapters.

``paper`` is the built-in simulated account (``trading_bot.broker.PaperBroker``); the other
providers connect a user's account to a real brokerage API.  Users connect their own
credentials under *Settings -> Broker connection*; the engine then routes that user's
signals and manual orders to the brokerage instead of the paper account.
"""

from __future__ import annotations

from typing import Any

from ..broker import Broker, BrokerError
from .alpaca import AlpacaBroker

BROKER_PROVIDERS: dict[str, dict[str, Any]] = {
    "paper": {"label": "Paper account (simulated)", "needs_credentials": False, "supports_live": False,
              "docs": ""},
    "alpaca": {"label": "Alpaca", "needs_credentials": True, "supports_live": True,
               "docs": "https://alpaca.markets/docs/trading/"},
}


def create_broker(provider: str, key: str = "", secret: str = "", paper: bool = True, **kwargs: Any) -> Broker | None:
    """Instantiate the adapter for ``provider`` (``None`` for the paper account)."""
    provider = (provider or "paper").lower()
    if provider == "paper":
        return None
    if provider == "alpaca":
        if not key or not secret:
            raise BrokerError("Alpaca needs an API key and secret")
        return AlpacaBroker(key, secret, paper=paper, **kwargs)
    raise BrokerError(f"Unknown broker provider {provider!r}")


__all__ = ["AlpacaBroker", "BROKER_PROVIDERS", "create_broker"]
