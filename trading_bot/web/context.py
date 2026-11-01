"""Shared state handed to the API router modules by :func:`trading_bot.web.server.create_app`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request

from ..broker import Broker
from ..config import AppConfig
from ..engine import TradingEngine
from ..fx import FxService
from ..payments import PaymentProvider
from ..trading import OrderService
from ..wallet import WalletService
from .users import User, UserService


@dataclass
class WebContext:
    engine: TradingEngine
    config: AppConfig
    users: UserService
    require_auth: Callable[..., Any]          # FastAPI dependency resolving the signed-in user
    account_of: Callable[[User], Broker]       # the user's execution venue (paper or connected broker)
    external_base: Callable[[Request], str]    # public URL of the app (for redirects / payment return URLs)
    save_config: Callable[[], None]
    orders: OrderService
    wallet: WalletService
    fx: FxService | None
    payments: PaymentProvider | None
