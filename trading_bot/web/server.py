"""FastAPI application exposing the trading engine to the web UI.

The engine is GUI-agnostic and publishes events on its :class:`EventBus`; the
:class:`Broadcaster` forwards them to every connected WebSocket client.  All
endpoints live under ``/api``; the single-page application is served from
``/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.requests import Request

from .. import __version__
from ..analytics import AIInsightsClient, build_report
from ..backtest import run_backtest
from ..broker import BrokerError, PaperBroker
from ..brokers import create_broker
from ..config import PROVIDER_BUDGETS, VALID_INTERVALS, AppConfig, ConfigError
from ..data import ProviderError, RateLimitError
from ..data.symbols import SymbolDirectory
from ..engine import (
    BarsUpdated,
    EngineStateChanged,
    Event,
    FetchFailed,
    OrderEvent,
    PositionsChanged,
    ScanCompleted,
    SignalEvent,
    StatusEvent,
    TradingEngine,
    WalletChanged,
)
from ..fx import FxService, create_fx_provider
from ..notify import WebhookChannel
from ..payments import PaymentError, create_payment_provider
from ..strategy import RuleStrategy, Signal, Strategy, StrategyDefinition, function_help
from ..trading import OrderService
from ..wallet import WalletService
from .auth import (
    LoginThrottle,
    SessionSigner,
    load_or_create_secret,
    password_rules,
    password_score,
)
from .charting import bars_payload, marker_indices, overlay_payload
from .context import WebContext
from .mail import Mailer
from .paths import STATIC_DIR
from .routes_fx import build_fx_router
from .routes_trading import build_trading_router, pending_payload
from .routes_wallet import build_wallet_router
from .users import User, UserError, UserService

logger = logging.getLogger(__name__)
SESSION_COOKIE = "tb_session"


# ---------------------------------------------------------------------------
# Event broadcasting
# ---------------------------------------------------------------------------


class Broadcaster:
    """Fan engine events (published on worker threads) out to WebSocket clients."""

    def __init__(self, engine: TradingEngine) -> None:
        self.engine = engine
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()
        self.history: list[dict[str, Any]] = []
        self.dropped = 0  # messages dropped for slow clients (bounded queues keep memory flat)

    @property
    def is_started(self) -> bool:
        return self._loop is not None

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._queues)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.engine.bus.subscribe(self._on_event)

    def stop(self) -> None:
        self.engine.bus.unsubscribe(self._on_event)
        self._loop = None

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        with self._lock:
            self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._queues.discard(queue)

    def _on_event(self, event: Event) -> None:
        message = serialize_event(event, self.engine)
        if message is None:
            return
        self.history.append(message)
        del self.history[:-200]
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._fanout, message)

    def _fanout(self, message: dict[str, Any]) -> None:
        with self._lock:
            queues = list(self._queues)
        for queue in queues:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self.dropped += 1
                if self.dropped in (1, 100, 1000) or self.dropped % 10_000 == 0:
                    logger.warning("Dropped %d events for slow WebSocket clients", self.dropped)


def serialize_signal(signal: Signal, executed: bool | None = None) -> dict[str, Any]:
    data = signal.to_dict()
    data["headline"] = signal.headline()
    data["description"] = signal.describe()
    if executed is not None:
        data["executed"] = executed
    return data


def serialize_event(event: Event, engine: TradingEngine) -> dict[str, Any] | None:
    if isinstance(event, BarsUpdated):
        result = event.result
        quote: dict[str, Any] | None = None
        if not result.empty:
            last = result.bars.iloc[-1]
            prev = float(result.bars["close"].iloc[-2]) if len(result.bars) > 1 else float(last["open"])
            quote = {
                "price": float(last["close"]),
                "change_pct": (float(last["close"]) / prev - 1) * 100 if prev else 0.0,
                "time": result.bars.index[-1].isoformat(),
            }
        return {"type": "bars", "symbol": event.symbol, "quote": quote, "stale": result.stale,
                "error": result.error, "fetched_at": result.fetched_at}
    if isinstance(event, SignalEvent):
        return {"type": "signal", "signal": serialize_signal(event.signal, event.executed)}
    if isinstance(event, StatusEvent):
        return {"type": "status", "message": event.message, "level": event.level}
    if isinstance(event, ScanCompleted):
        return {"type": "scan", "started_at": event.started_at.isoformat(), "duration": event.duration,
                "symbols_scanned": event.symbols_scanned, "signals": event.signals, "refreshed": event.refreshed,
                "market_open": event.market_open, "budget": event.budget, "status": status_payload(engine)}
    if isinstance(event, FetchFailed):
        return {"type": "fetch_failed", "symbol": event.symbol, "message": event.message, "has_cache": event.has_cache}
    if isinstance(event, PositionsChanged):
        return {"type": "positions", "account": event.account}
    if isinstance(event, OrderEvent):
        return {"type": "order", "account": event.account, "kind": event.kind, "symbol": event.symbol, "side": event.side,
                "quantity": event.quantity, "price": event.price, "message": event.message}
    if isinstance(event, WalletChanged):
        return {"type": "wallet", "account": event.account, "kind": event.kind, "amount": event.amount,
                "status": event.status}
    if isinstance(event, EngineStateChanged):
        return {"type": "engine", "running": event.running, "status": status_payload(engine)}
    return None


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def status_payload(engine: TradingEngine) -> dict[str, Any]:
    budget = engine.data.budget()
    market_open = engine.market_is_open()
    cfg = engine.config
    return {
        "version": __version__,
        "running": engine.running,
        "provider": cfg.data.provider,
        "interval": cfg.data.interval,
        "scan_interval": cfg.scan_interval,
        "watchlist": engine.watchlist,
        "budget": {"remaining_minute": budget.remaining_minute, "per_minute": budget.per_minute,
                   "remaining_day": budget.remaining_day, "per_day": budget.per_day, "text": budget.describe()},
        "refresh_interval": engine.effective_refresh_interval(),
        "market_open": market_open,
        "next_market_open": None if market_open else engine.next_market_open().isoformat(),
        "market_timezone": cfg.market_hours.timezone,
        "trading_enabled": cfg.trading.trading_enabled,
        "scans": engine.stats.scans,
        "signals": engine.stats.signals,
        "errors": engine.stats.errors,
        "last_error": engine.stats.last_error,
        "errors_by_symbol": engine.snapshot()["errors_by_symbol"],
        "requires_api_key": cfg.requires_api_key() and not cfg.data.api_key,
        "api_key_set": bool(cfg.data.api_key),
        "account_currency": cfg.payments.currency,
    }


def watchlist_payload(engine: TradingEngine, directory: SymbolDirectory | None = None) -> list[dict[str, Any]]:
    rows = []
    for symbol in engine.watchlist:
        result = engine.latest_bars(symbol) or engine.data.cached_bars(symbol, engine.config.data.interval)
        row: dict[str, Any] = {"symbol": symbol, "name": directory.name(symbol) if directory else None,
                               "price": None, "change_pct": None, "time": None,
                               "stale": False, "error": engine.last_error(symbol)}
        if result is not None and not result.empty:
            last = result.bars.iloc[-1]
            prev = float(result.bars["close"].iloc[-2]) if len(result.bars) > 1 else float(last["open"])
            row.update(price=float(last["close"]), change_pct=(float(last["close"]) / prev - 1) * 100 if prev else 0.0,
                       time=result.bars.index[-1].isoformat(), stale=result.stale)
        rows.append(row)
    return rows


def strategy_payload(definition: StrategyDefinition) -> dict[str, Any]:
    data = definition.to_dict()
    data["kind"] = "rule"
    data["valid"] = not definition.validate()
    return data


def plugin_payload(strategy: Strategy) -> dict[str, Any]:
    return {"name": strategy.name, "description": strategy.description, "kind": "plugin", "enabled": strategy.enabled,
            "symbols": list(strategy.symbols), "stop_loss_pct": strategy.stop_loss_pct,
            "take_profit_pct": strategy.take_profit_pct, "cooldown_bars": strategy.cooldown_bars, "valid": True,
            "buy_rule": "", "sell_rule": "", "params": {}}


def positions_payload(engine: TradingEngine, account: str | None = None, broker: Any = None) -> dict[str, Any]:
    broker = broker if broker is not None else engine.broker
    account = account if account is not None else engine.accounts.default_name
    prices = engine.latest_prices()
    rows = []
    error: str | None = None
    try:
        for symbol, pos in sorted(broker.positions().items()):
            last = prices.get(symbol, pos.avg_price)
            rows.append({"symbol": symbol, "quantity": pos.quantity, "avg_price": pos.avg_price, "last": last,
                         "pnl": pos.unrealized_pnl(last), "pnl_pct": pos.unrealized_pnl_pct(last),
                         "stop_loss": pos.stop_loss, "take_profit": pos.take_profit, "strategy": pos.strategy,
                         "opened_at": pos.opened_at.isoformat()})
        cash, equity = broker.cash(), broker.equity(prices)
    except BrokerError as exc:  # an external brokerage that is down must not break the page
        logger.warning("[%s] broker unavailable: %s", account, exc)
        error, cash, equity, rows = str(exc), None, None, []
    summary = {"name": account, "cash": cash, "equity": equity, "open_positions": len(rows),
               "paper": broker.is_paper, "broker": broker.describe(), "error": error,
               "realized_pnl": broker.realized_pnl() if isinstance(broker, PaperBroker) else None,
               "initial_cash": broker.initial_cash if isinstance(broker, PaperBroker) else None,
               "auto_trade": engine.accounts.is_enabled(account), "currency": engine.config.payments.currency}
    fills = engine.storage.recent_fills(100, account=account)
    return {"account": summary, "positions": rows, "fills": fills, "pending_orders": pending_payload(broker)}


def _finite(value: float) -> float | None:
    return None if value is None or not math.isfinite(value) else float(value)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SymbolBody(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)


class StrategyBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    buy_rule: str = ""
    sell_rule: str = ""
    description: str = ""
    symbols: list[str] = Field(default_factory=list)
    params: dict[str, float] = Field(default_factory=dict)
    enabled: bool = True
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    cooldown_bars: int = 0
    previous_name: str | None = None

    def definition(self) -> StrategyDefinition:
        return StrategyDefinition(
            name=self.name, buy_rule=self.buy_rule, sell_rule=self.sell_rule, description=self.description,
            symbols=self.symbols, params=self.params, enabled=self.enabled, stop_loss_pct=self.stop_loss_pct,
            take_profit_pct=self.take_profit_pct, cooldown_bars=self.cooldown_bars,
        )


class ValidateBody(StrategyBody):
    symbol: str | None = None


class EnabledBody(BaseModel):
    enabled: bool


class BacktestBody(BaseModel):
    symbol: str
    strategy: str | None = None           # name of a stored strategy ...
    definition: StrategyBody | None = None  # ... or an unsaved definition from the editor
    cash: float = Field(default=10_000.0, gt=0)
    commission: float = Field(default=0.0, ge=0)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=254, description="username or e-mail address")
    password: str = Field(min_length=1, max_length=256)


class SetupBody(LoginBody):
    email: str | None = Field(default=None, max_length=254)
    provider: str | None = None   # optional: choose the market-data source during account creation
    api_key: str | None = None    # optional: API key for that provider (can be added later in Settings)


class SignupBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class ResendBody(BaseModel):
    identifier: str = Field(min_length=1, max_length=254)


class PasswordCheckBody(BaseModel):
    password: str = Field(max_length=512)
    username: str | None = Field(default=None, max_length=64)


class PasswordChangeBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ProfileBody(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    auto_trade: bool | None = None


class ProviderBody(BaseModel):
    provider: str
    api_key: str | None = None


class SettingsBody(BaseModel):
    scan_interval: int | None = None
    data: dict[str, Any] | None = None
    market_hours: dict[str, Any] | None = None
    notifications: dict[str, Any] | None = None
    trading: dict[str, Any] | None = None
    ai: dict[str, Any] | None = None
    web: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    payments: dict[str, Any] | None = None
    fx: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(engine: TradingEngine, config_path: str | None = None, token: str | None = None,
               sessions: SessionSigner | None = None, throttle: LoginThrottle | None = None) -> FastAPI:
    config: AppConfig = engine.config
    api_token = token if token is not None else config.web.token
    broadcaster = Broadcaster(engine)
    secret = config.web.secret_key or load_or_create_secret(config.data_dir_path / "session.secret")
    sessions = sessions or SessionSigner(secret, config.web.session_hours * 3600)
    throttle = throttle or LoginThrottle()
    base_path = config.web.base_path
    symbols = SymbolDirectory.load(config.symbols_file_path)
    mailer = Mailer(config.notifications.email, outbox_dir=config.data_dir_path / "outbox")
    users = UserService(engine.storage, config, mailer)
    if config.web.password_hash:
        users.migrate_legacy_admin()
        if config_path:
            config.save(config_path)  # the hash now lives in the user store only
    for existing in users.list():  # every user has a paper account; signals execute where auto-trade is on
        engine.accounts.ensure(existing.username, enabled=existing.auto_trade)
        if existing.broker_connected:
            try:
                engine.accounts.set_broker(existing.username, create_broker(
                    existing.broker_provider, existing.broker_key or "", existing.broker_secret or "", existing.broker_paper))
            except BrokerError as exc:
                logger.error("User %r: could not connect %s (%s); using the paper account", existing.username,
                             existing.broker_provider, exc)
    ai = AIInsightsClient(config.ai)

    def _build_fx() -> FxService | None:
        try:
            return FxService(create_fx_provider(config.fx.provider), config.fx)
        except Exception as exc:  # noqa: BLE001 - never block startup on an optional feature
            logger.error("Exchange rates disabled: %s", exc)
            return None

    def _build_payments() -> Any:
        try:
            return create_payment_provider(config.payments, checkout_base="")
        except PaymentError as exc:
            logger.error("Payments disabled: %s", exc)
            return None

    fx_service = _build_fx()
    payments = _build_payments()
    wallet = WalletService(engine.storage, engine.accounts, config.payments, payments, fx_service, engine.bus)
    orders = OrderService(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        import anyio

        anyio.to_thread.current_default_thread_limiter().total_tokens = config.web.worker_threads
        broadcaster.start(asyncio.get_running_loop())
        try:
            yield
        finally:
            broadcaster.stop()
            engine.stop()  # graceful shutdown: finish the current scan, then stop the loop

    app = FastAPI(title="Trading Bot", version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.state.engine = engine
    app.state.broadcaster = broadcaster
    app.state.sessions = sessions
    app.state.symbols = symbols
    app.state.users = users
    app.state.mailer = mailer
    app.state.ai = ai

    def _bars_for(symbol: str) -> Any:
        result = engine.latest_bars(symbol) or engine.data.cached_bars(symbol, config.data.interval)
        return None if result is None else result

    def _strategy_by_name(name: str) -> Strategy:
        for strategy in engine.strategies.all_strategies():
            if strategy.name == name:
                return strategy
        raise HTTPException(status_code=404, detail=f"No strategy named {name!r}")

    def _save_config() -> None:
        if config_path:
            config.save(config_path)

    def _apply_data_settings() -> None:
        """Hot-swap the market-data service after provider / key / budget changes (no restart)."""
        try:
            engine.reconfigure_data()
        except ProviderError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        for symbol in engine.watchlist:
            engine.fetch_in_background(symbol)

    # ------------------------------------------------------------- auth
    def _setup_required() -> bool:
        return users.setup_required()

    def _token_ok(provided: str | None) -> bool:
        return bool(api_token and provided and secrets.compare_digest(provided, api_token))

    def _bearer(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        return header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token")

    def _user_from_cookie(token: str | None) -> User | None:
        username = sessions.peek_username(token)
        if not username:
            return None
        user = users.find(username)
        if user is None or sessions.verify(token, user.password_hash) is None:
            return None
        return user

    def _authenticated(request: Request) -> User | None:
        """The signed-in user (session cookie), or a synthetic user for the API bearer token."""
        user = _user_from_cookie(request.cookies.get(SESSION_COOKIE))
        if user is not None:
            return user
        if _token_ok(_bearer(request)):
            admin = next((u for u in users.list() if u.is_admin), None)
            return admin or User(0, "api-token", None, "", "admin", True, False, "")
        return None

    async def require_auth(request: Request) -> User:
        user = _authenticated(request)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return user

    def _client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _cookie_path() -> str:
        return (base_path or "") + "/"

    def _external_base(request: Request) -> str:
        if config.web.public_url:
            return config.web.public_url.rstrip("/") + base_path
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
        return f"{proto}://{host}{base_path}"

    def _set_session_cookie(response: Response, request: Request, token: str) -> None:
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
        response.set_cookie(SESSION_COOKIE, token, max_age=int(config.web.session_hours * 3600), httponly=True,
                            samesite="lax", secure=secure, path=_cookie_path())

    def _issue_session(response: Response, request: Request, user: User) -> str:
        token = sessions.issue(user.username, user.password_hash)
        _set_session_cookie(response, request, token)
        return token

    def _user_error(exc: UserError) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=str(exc))

    def _account_of(user: User) -> Any:
        return engine.accounts.ensure(user.username, enabled=user.auto_trade)

    auth: list[Any] = [Depends(require_auth)]

    ctx = WebContext(engine=engine, config=config, users=users, require_auth=require_auth, account_of=_account_of,
                     external_base=_external_base, save_config=_save_config, orders=orders, wallet=wallet, fx=fx_service,
                     payments=payments)
    app.state.context = ctx
    app.include_router(build_trading_router(ctx))
    app.include_router(build_wallet_router(ctx))
    app.include_router(build_fx_router(ctx))

    def _provider_options() -> dict[str, Any]:
        return {"providers": [{"name": name, "requires_api_key": name in ("alphavantage", "twelvedata"),
                               "per_minute": budget[0], "per_day": budget[1]}
                              for name, budget in PROVIDER_BUDGETS.items()],
                "provider": config.data.provider, "api_key_set": bool(config.data.api_key)}

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        user = _authenticated(request)
        return {"authenticated": user is not None, "username": user.username if user else None,
                "user": user.public() if user else None, "setup_required": _setup_required(),
                "allow_signup": config.web.allow_signup and not _setup_required(),
                "verification_required": users.verification_required,
                "email_configured": mailer.configured, **_provider_options()}

    @app.post("/api/auth/password-check")
    def auth_password_check(body: PasswordCheckBody) -> dict[str, Any]:
        """Per-rule evaluation for live feedback while typing (the same rules gate setup and changes)."""
        rules = password_rules(body.password, body.username)
        return {"rules": rules, "passed": all(r["passed"] for r in rules), "score": password_score(body.password, body.username)}

    @app.post("/api/auth/setup", status_code=201)
    def auth_setup(body: SetupBody, request: Request, response: Response) -> dict[str, Any]:
        """First run only: create the administrator account (and optionally the data provider)."""
        if not _setup_required():
            raise HTTPException(status_code=409, detail="An administrator account already exists")
        if body.provider is not None and body.provider not in PROVIDER_BUDGETS:
            raise HTTPException(status_code=422, detail=f"Unknown provider {body.provider!r}")
        try:
            user, _pending = users.register(body.username, body.email, body.password, role="admin")
        except UserError as exc:
            raise _user_error(exc) from exc
        config.web.username = user.username
        config.web.password_hash = ""  # credentials now live in the user store
        data_changed = False
        if body.provider is not None and body.provider != config.data.provider:
            config.data.provider = body.provider
            data_changed = True
        if body.api_key is not None and body.api_key.strip() and body.api_key.strip() != config.data.api_key:
            config.data.api_key = body.api_key.strip()
            data_changed = True
        _save_config()
        if data_changed:
            _apply_data_settings()
        engine.accounts.default_name = user.username
        _account_of(user)
        _issue_session(response, request, user)
        logger.info("Administrator account %r created (provider %s)", user.username, config.data.provider)
        return {"authenticated": True, "username": user.username, "user": user.public(), "status": status_payload(engine)}

    @app.post("/api/auth/signup", status_code=201)
    def auth_signup(body: SignupBody, request: Request, response: Response) -> dict[str, Any]:
        """Create a user account; sends a verification e-mail when verification is enabled."""
        if _setup_required():
            raise HTTPException(status_code=409, detail="Complete the initial setup first")
        if not config.web.allow_signup:
            raise HTTPException(status_code=403, detail="Sign-up is disabled on this server")
        key = _client_key(request)
        if throttle.retry_after(key) > 0:
            raise HTTPException(status_code=429, detail="Too many attempts; try again later")
        try:
            user, pending = users.register(body.username, body.email, body.password, base_url=_external_base(request))
        except UserError as exc:
            raise _user_error(exc) from exc
        except RuntimeError as exc:  # mail delivery failed
            raise HTTPException(status_code=503, detail=f"Could not send the verification e-mail: {exc}") from exc
        _account_of(user)
        if pending:
            return {"authenticated": False, "verification_required": True, "email": user.email, "username": user.username}
        _issue_session(response, request, user)
        return {"authenticated": True, "verification_required": False, "username": user.username, "user": user.public()}

    @app.post("/api/auth/resend-verification")
    def auth_resend(body: ResendBody, request: Request) -> dict[str, Any]:
        key = _client_key(request)
        if throttle.retry_after(key) > 0:
            raise HTTPException(status_code=429, detail="Too many attempts; try again later")
        try:
            sent = users.resend_verification(body.identifier, base_url=_external_base(request))
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=f"Could not send the verification e-mail: {exc}") from exc
        if not sent:
            throttle.record_failure(key)
        # Do not reveal whether an account exists.
        return {"ok": True, "message": "If the account exists and is not verified yet, a new link has been sent."}

    @app.get("/verify", include_in_schema=False)
    def verify_email(token: str = Query(default="")) -> Response:
        try:
            users.verify(token)
        except UserError as exc:
            return RedirectResponse(f"login?verify_error={quote(str(exc))}", status_code=302)
        return RedirectResponse("login?verified=1", status_code=302)

    @app.post("/api/auth/login")
    def auth_login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        if _setup_required():
            raise HTTPException(status_code=409, detail="No account configured yet; complete the setup first")
        key = _client_key(request)
        wait = throttle.retry_after(key)
        if wait > 0:
            raise HTTPException(status_code=429, detail=f"Too many failed attempts; try again in {int(wait) + 1}s",
                                headers={"Retry-After": str(int(wait) + 1)})
        try:
            user = users.authenticate(body.username, body.password)
        except UserError as exc:
            if exc.status == 401:
                locked = throttle.record_failure(key)
                logger.warning("Failed login for %r from %s", body.username, key)
                if locked:
                    raise HTTPException(status_code=429, detail=f"Too many failed attempts; try again in {int(locked)}s",
                                        headers={"Retry-After": str(int(locked))}) from exc
            raise _user_error(exc) from exc
        throttle.record_success(key)
        _account_of(user)
        _issue_session(response, request, user)
        logger.info("User %r logged in from %s", user.username, key)
        return {"authenticated": True, "username": user.username, "user": user.public()}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response) -> dict[str, Any]:
        sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path=_cookie_path())
        return {"authenticated": False}

    @app.post("/api/auth/password")
    def auth_change_password(body: PasswordChangeBody, request: Request, response: Response,
                             user: User = Depends(require_auth)) -> dict[str, Any]:
        if user.id == 0:
            raise HTTPException(status_code=403, detail="Not available for API-token access")
        try:
            updated = users.change_password(user, body.current_password, body.new_password)
        except UserError as exc:
            raise _user_error(exc) from exc
        _issue_session(response, request, updated)  # tokens are bound to the password hash: all others are now invalid
        return {"ok": True, "username": updated.username, "user": updated.public()}

    @app.get("/api/auth/me", dependencies=auth)
    def auth_me(user: User = Depends(require_auth)) -> dict[str, Any]:
        return user.public()

    @app.patch("/api/auth/profile")
    def auth_profile(body: ProfileBody, user: User = Depends(require_auth)) -> dict[str, Any]:
        if user.id == 0:
            raise HTTPException(status_code=403, detail="Not available for API-token access")
        try:
            updated = users.update_profile(user, email=body.email, auto_trade=body.auto_trade)
        except UserError as exc:
            raise _user_error(exc) from exc
        engine.accounts.ensure(updated.username, enabled=updated.auto_trade)
        return updated.public()

    @app.get("/api/users", dependencies=auth)
    def list_users(user: User = Depends(require_auth)) -> list[dict[str, Any]]:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Administrators only")
        return [u.public() for u in users.list()]

    # ----------------------------------------------------------- status
    @app.get("/api/status", dependencies=auth)
    def get_status() -> dict[str, Any]:
        return status_payload(engine)

    @app.post("/api/engine/start", dependencies=auth)
    def start_engine() -> dict[str, Any]:
        if config.requires_api_key() and not config.data.api_key:
            raise HTTPException(status_code=409, detail=f"The {config.data.provider} provider needs an API key; "
                                                        "set it in Settings or TRADING_BOT_API_KEY")
        engine.start()
        return status_payload(engine)

    @app.post("/api/engine/stop", dependencies=auth)
    def stop_engine() -> dict[str, Any]:
        engine.stop()
        return status_payload(engine)

    @app.post("/api/engine/scan", dependencies=auth)
    def scan_now() -> dict[str, Any]:
        if engine.running:
            engine.request_scan(force=True)
        else:
            engine.scan_in_background(force=True)
        return {"ok": True}

    # -------------------------------------------------------- watchlist
    @app.get("/api/watchlist", dependencies=auth)
    def get_watchlist() -> list[dict[str, Any]]:
        return watchlist_payload(engine, symbols)

    @app.get("/api/symbols/search", dependencies=auth)
    def search_symbols(q: str = Query(default="", max_length=40), limit: int = Query(default=8, ge=1, le=25),
                       remote: bool = Query(default=False)) -> dict[str, Any]:
        """Ticker suggestions with company names.

        Local directory matches are free and instant.  ``remote=true`` additionally asks the
        data provider (spending one request of its budget) - the UI offers that explicitly.
        """
        query = q.strip()
        local = [e.to_dict() for e in symbols.search(query, limit)]
        remote_results: list[dict[str, Any]] = []
        remote_error: str | None = None
        provider = engine.data.provider
        supports_remote = provider.name in ("alphavantage", "twelvedata")
        if remote and query and supports_remote:
            if not engine.data.limiter.try_acquire():
                remote_error = "API request budget exhausted"
            else:
                try:
                    seen = {row["symbol"] for row in local}
                    remote_results = [{"symbol": info.symbol, "name": info.name, "type": info.type or "stock",
                                       "source": provider.name, "region": info.region}
                                      for info in provider.search(query)[:limit] if info.symbol not in seen]
                except ProviderError as exc:
                    remote_error = str(exc)
        return {"query": query, "results": local + remote_results, "remote_supported": supports_remote,
                "remote_error": remote_error, "provider": provider.name}

    @app.get("/api/symbols/{symbol}", dependencies=auth)
    def lookup_symbol(symbol: str) -> dict[str, Any]:
        entry = symbols.get(symbol)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Unknown symbol {symbol.upper()}")
        return entry.to_dict()

    @app.post("/api/watchlist", dependencies=auth, status_code=201)
    def add_symbol(body: SymbolBody) -> dict[str, Any]:
        symbol = body.symbol.strip().upper()
        if not symbol.replace(".", "").replace("-", "").isalnum():
            raise HTTPException(status_code=422, detail=f"Invalid symbol {body.symbol!r}")
        added = engine.add_symbol(symbol)
        if added:
            _save_config()
            engine.fetch_in_background(symbol)
        return {"symbol": symbol, "added": added, "watchlist": engine.watchlist}

    @app.delete("/api/watchlist/{symbol}", dependencies=auth)
    def remove_symbol(symbol: str) -> dict[str, Any]:
        if not engine.remove_symbol(symbol):
            raise HTTPException(status_code=404, detail=f"{symbol.upper()} is not in the watchlist")
        _save_config()
        return {"symbol": symbol.upper(), "watchlist": engine.watchlist}

    # ------------------------------------------------------------- bars
    @app.get("/api/bars/{symbol}", dependencies=auth)
    def get_bars(symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        result = _bars_for(symbol)
        if result is None:
            try:
                result = engine.fetch_symbol(symbol)
            except (RateLimitError, ProviderError) as exc:  # pragma: no cover - engine reports via events
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=503, detail=engine.last_error(symbol) or f"No data for {symbol}")
        if result.payload is None:
            result.payload = bars_payload(result.bars)  # computed once per fetched frame, shared by all clients
        return {"symbol": symbol, "interval": config.data.interval, "bars": result.payload,
                "fetched_at": result.fetched_at, "stale": result.stale, "error": engine.last_error(symbol)}

    @app.post("/api/bars/{symbol}/refresh", dependencies=auth)
    def refresh_bars(symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        if symbol not in engine.watchlist:
            raise HTTPException(status_code=404, detail=f"{symbol} is not in the watchlist")
        engine.fetch_in_background(symbol, force=True)
        return {"ok": True, "symbol": symbol}

    @app.get("/api/bars/{symbol}/overlay/{strategy_name}", dependencies=auth)
    def get_overlay(symbol: str, strategy_name: str) -> dict[str, Any]:
        result = _bars_for(symbol.upper())
        strategy = _strategy_by_name(strategy_name)
        if result is None or result.empty:
            return overlay_payload(strategy, None)
        try:
            return overlay_payload(strategy, result.bars)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"Could not evaluate {strategy.name}: {exc}") from exc

    # ------------------------------------------------------- strategies
    @app.get("/api/strategies", dependencies=auth)
    def list_strategies() -> list[dict[str, Any]]:
        rows = [strategy_payload(d) for d in engine.strategies.definitions()]
        rows += [plugin_payload(p) for p in engine.strategies.plugins()]
        return rows

    @app.get("/api/strategies/help", dependencies=auth)
    def rules_help() -> dict[str, Any]:
        return {"functions": [{"signature": sig, "description": desc} for sig, desc in function_help()],
                "series": ["open", "high", "low", "close", "volume"],
                "operators": ["and", "or", "not", "<", "<=", ">", ">=", "==", "!=", "+", "-", "*", "/", "%", "**"]}

    @app.post("/api/strategies/validate", dependencies=auth)
    def validate_strategy(body: ValidateBody) -> dict[str, Any]:
        definition = body.definition()
        problems = definition.validate()
        if problems:
            return {"valid": False, "problems": problems}
        strategy = RuleStrategy(definition)
        symbol = body.symbol or next((s for s in engine.watchlist if strategy.applies_to(s)), None)
        result = _bars_for(symbol) if symbol else None
        if result is None or result.empty:
            return {"valid": True, "problems": [], "symbol": symbol, "bars": 0, "buy": 0, "sell": 0}
        try:
            frame = strategy.signals(result.bars)
        except Exception as exc:  # noqa: BLE001
            return {"valid": False, "problems": [f"Rule evaluation failed on {symbol}: {exc}"]}
        return {"valid": True, "problems": [], "symbol": symbol, "bars": len(result.bars),
                "buy": int(frame["buy"].sum()), "sell": int(frame["sell"].sum())}

    @app.post("/api/strategies", dependencies=auth, status_code=201)
    def save_strategy(body: StrategyBody) -> dict[str, Any]:
        definition = body.definition()
        try:
            engine.strategies.upsert(definition, previous_name=body.previous_name)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return strategy_payload(definition)

    @app.patch("/api/strategies/{name}", dependencies=auth)
    def set_strategy_enabled(name: str, body: EnabledBody) -> dict[str, Any]:
        _strategy_by_name(name)
        engine.strategies.set_enabled(name, body.enabled)
        return {"name": name, "enabled": body.enabled}

    @app.delete("/api/strategies/{name}", dependencies=auth)
    def delete_strategy(name: str) -> dict[str, Any]:
        if not engine.strategies.remove(name):
            raise HTTPException(status_code=404, detail=f"No rule strategy named {name!r}")
        return {"name": name, "deleted": True}

    @app.post("/api/backtest", dependencies=auth)
    def backtest(body: BacktestBody) -> dict[str, Any]:
        if body.definition is not None:
            definition = body.definition.definition()
            problems = definition.validate()
            if problems:
                raise HTTPException(status_code=422, detail="; ".join(problems))
            strategy: Strategy = RuleStrategy(definition)
        elif body.strategy:
            strategy = _strategy_by_name(body.strategy)
        else:
            raise HTTPException(status_code=422, detail="Provide a strategy name or definition")
        symbol = body.symbol.upper()
        result = _bars_for(symbol)
        if result is None or result.empty:
            raise HTTPException(status_code=409, detail=f"No bars loaded for {symbol} yet; refresh it first")
        try:
            bt = run_backtest(strategy, result.bars, symbol, body.cash, 1.0, body.commission)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        summary = bt.summary()
        return {
            "summary": summary,
            "equity": [_finite(v) for v in bt.equity_curve.to_numpy(dtype=float).tolist()],
            "trades": [{**asdict(t), "entry_time": t.entry_time.isoformat(), "exit_time": t.exit_time.isoformat()}
                       for t in bt.trades],
            "buy": [int(i) for i in bt.buy_markers.to_numpy().nonzero()[0]],
            "sell": [int(i) for i in bt.sell_markers.to_numpy().nonzero()[0]],
        }

    # ------------------------------------------------------- signals etc
    @app.get("/api/signals", dependencies=auth)
    def get_signals(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
        rows = engine.storage.recent_signals(limit)
        out = []
        for row in rows:
            signal = Signal(symbol=row["symbol"], action=row["action"], strategy=row["strategy"], price=row["price"],
                            bar_time=datetime.fromisoformat(row["bar_time"]), reason=row["reason"] or "",
                            created_at=datetime.fromisoformat(row["created_at"]), id=row["id"])
            out.append(serialize_signal(signal, bool(row["executed"])))
        return out

    @app.get("/api/signals/markers/{symbol}", dependencies=auth)
    def get_signal_markers(symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        result = _bars_for(symbol)
        rows = engine.storage.recent_signals(500, symbol=symbol)
        buys = [r["bar_time"] for r in rows if r["action"] == "BUY"]
        sells = [r["bar_time"] for r in rows if r["action"] == "SELL"]
        df = None if result is None else result.bars
        return {"symbol": symbol, "buy": marker_indices(df, buys), "sell": marker_indices(df, sells)}

    @app.get("/api/positions")
    def get_positions(user: User = Depends(require_auth)) -> dict[str, Any]:
        return positions_payload(engine, user.username, _account_of(user))

    @app.post("/api/account/reset")
    def reset_account(user: User = Depends(require_auth)) -> dict[str, Any]:
        broker = _account_of(user)
        if not isinstance(broker, PaperBroker):
            raise HTTPException(status_code=409, detail="Only the paper account can be reset")
        broker.initial_cash = config.trading.initial_cash
        broker.reset()
        return positions_payload(engine, user.username, broker)

    # ------------------------------------------------------ history / analytics
    @app.get("/api/history")
    def get_history(user: User = Depends(require_auth), limit: int = Query(default=500, ge=1, le=5000)) -> dict[str, Any]:
        """Fills, closed trades, cumulative P&L and statistics of the signed-in user's account."""
        broker = _account_of(user)
        fills = engine.storage.fills_for_account(user.username)
        report = build_report(user.username, fills, len(broker.positions()))
        data = report.to_dict()
        data["fills"] = list(reversed(fills))[:limit]
        data["account_summary"] = positions_payload(engine, user.username, broker)["account"]
        return data

    @app.get("/api/analytics")
    def get_analytics(user: User = Depends(require_auth)) -> dict[str, Any]:
        broker = _account_of(user)
        report = build_report(user.username, engine.storage.fills_for_account(user.username), len(broker.positions()))
        data = report.to_dict(include_trades=False)
        data["ai_available"] = ai.configured
        data["ai_model"] = config.ai.model if ai.configured else None
        data["open_positions"] = len(broker.positions())
        return data

    @app.post("/api/analytics/ai")
    def ai_analysis(user: User = Depends(require_auth)) -> dict[str, Any]:
        if not ai.configured:
            raise HTTPException(status_code=409, detail="No AI provider configured (Settings -> AI insights)")
        broker = _account_of(user)
        report = build_report(user.username, engine.storage.fills_for_account(user.username), len(broker.positions()))
        try:
            result = ai.analyze(report.ai_context())
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {**result, "generated_at": datetime.now(UTC).isoformat()}

    @app.get("/api/events", dependencies=auth)
    def recent_events(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
        return broadcaster.history[-limit:]

    # --------------------------------------------------------- settings
    @app.get("/api/settings", dependencies=auth)
    def get_settings() -> dict[str, Any]:
        data = config.to_dict()
        data["data"]["api_key_set"] = bool(config.data.api_key)
        data["data"]["api_key"] = ""  # never hand the key back to the browser
        data["notifications"]["email"]["password"] = ""
        data["web"]["token"] = ""
        data["web"]["password_hash"] = ""
        data["web"]["secret_key"] = ""
        data["ai"]["api_key_set"] = bool(config.ai.api_key)
        data["ai"]["api_key"] = ""
        data["payments"]["stripe"]["secret_key_set"] = bool(config.payments.stripe.secret_key)
        data["payments"]["stripe"]["webhook_secret_set"] = bool(config.payments.stripe.webhook_secret)
        data["payments"]["stripe"]["secret_key"] = ""
        data["payments"]["stripe"]["webhook_secret"] = ""
        data["payments"]["configured"] = wallet.enabled
        data["fx"]["available"] = ctx.fx is not None
        data["email_configured"] = mailer.configured
        data["options"] = {"providers": list(PROVIDER_BUDGETS), "intervals": list(VALID_INTERVALS),
                           "budgets": {k: {"per_minute": v[0], "per_day": v[1]} for k, v in PROVIDER_BUDGETS.items()}}
        data["config_path"] = config_path
        return data

    @app.put("/api/settings", dependencies=auth)
    def update_settings(body: SettingsBody) -> dict[str, Any]:
        raw = config.to_dict()
        if body.scan_interval is not None:
            raw["scan_interval"] = body.scan_interval
        for section in ("data", "market_hours", "notifications", "trading", "ai", "web", "execution", "payments", "fx"):
            patch = getattr(body, section)
            if patch:
                if section in ("data", "ai") and not patch.get("api_key"):
                    patch.pop("api_key", None)  # blank = keep the stored key
                if section == "payments" and isinstance(patch.get("stripe"), dict):
                    for secret_field in ("secret_key", "webhook_secret"):
                        if not patch["stripe"].get(secret_field):
                            patch["stripe"].pop(secret_field, None)
                    patch["stripe"].pop("secret_key_set", None)
                    patch["stripe"].pop("webhook_secret_set", None)
                if section == "payments":
                    patch.pop("configured", None)
                if section == "fx":
                    patch.pop("available", None)
                if section == "web":
                    patch = {k: v for k, v in patch.items() if k in ("allow_signup", "email_verification", "public_url")}
                if section == "notifications" and "email" in patch and not patch["email"].get("password"):
                    patch["email"].pop("password", None)
                _deep_update(raw[section], patch)
        try:
            updated = AppConfig.from_dict(raw)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid settings: {exc}") from exc
        data_fields = ("provider", "api_key", "requests_per_minute", "requests_per_day", "csv_directory", "interval")
        data_changed = any(getattr(updated.data, f) != getattr(config.data, f) for f in data_fields)
        # Apply in place so the running engine sees the new values.
        config.scan_interval = updated.scan_interval
        config.data = updated.data
        config.market_hours = updated.market_hours
        config.notifications = updated.notifications
        config.trading = updated.trading
        config.ai = updated.ai
        ai.cfg = config.ai
        config.execution = updated.execution
        engine.execution.cfg = config.execution
        fx_changed = updated.fx != config.fx
        payments_changed = updated.payments != config.payments
        config.fx = updated.fx
        config.payments = updated.payments
        wallet.cfg = config.payments
        if fx_changed:
            if ctx.fx is not None:
                ctx.fx.close()
            ctx.fx = _build_fx()
            wallet.fx = ctx.fx
        if payments_changed:
            if ctx.payments is not None:
                ctx.payments.close()
            ctx.payments = _build_payments()
            wallet.provider = ctx.payments
        config.web.allow_signup = updated.web.allow_signup
        config.web.email_verification = updated.web.email_verification
        config.web.public_url = updated.web.public_url
        mailer.cfg = config.notifications.email
        _save_config()
        _sync_webhook(engine)
        if data_changed:
            _apply_data_settings()
        return {"ok": True, "data_reconfigured": data_changed, "status": status_payload(engine)}

    @app.post("/api/settings/provider", dependencies=auth)
    def set_provider(body: ProviderBody) -> dict[str, Any]:
        """Switch the market-data provider (e.g. to demo data) and optionally set its API key."""
        if body.provider not in PROVIDER_BUDGETS:
            raise HTTPException(status_code=422, detail=f"Unknown provider {body.provider!r}")
        config.data.provider = body.provider
        if body.api_key is not None and body.api_key.strip():
            config.data.api_key = body.api_key.strip()
        _save_config()
        _apply_data_settings()
        return {"ok": True, "status": status_payload(engine)}

    # --------------------------------------------------------- websocket
    @app.websocket("/ws")
    async def websocket_events(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
        user = _user_from_cookie(websocket.cookies.get(SESSION_COOKIE))
        if user is None and not _token_ok(token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        queue = broadcaster.subscribe()
        try:
            await websocket.send_text(json.dumps({"type": "hello", "status": status_payload(engine)}))
            while True:
                message = await queue.get()
                await websocket.send_text(json.dumps(message, default=str))
        except WebSocketDisconnect:
            pass
        finally:
            broadcaster.unsubscribe(queue)

    # ------------------------------------------------------------ static
    app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")

    def _page(name: str) -> FileResponse:
        return FileResponse(STATIC_DIR / name, headers={"Cache-Control": "no-store"})

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> Response:
        if _authenticated(request) is None:
            return RedirectResponse("login", status_code=302)  # relative: works behind any base path
        return _page("index.html")

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request) -> Response:
        if _authenticated(request) is not None:
            return RedirectResponse("./", status_code=302)
        return _page("login.html")

    @app.get("/signup", include_in_schema=False)
    def signup_page(request: Request) -> Response:
        if _authenticated(request) is not None:
            return RedirectResponse("./", status_code=302)
        if _setup_required() or not config.web.allow_signup:
            return RedirectResponse("login", status_code=302)
        return _page("signup.html")

    @app.get("/api/health", include_in_schema=False)
    def health() -> JSONResponse:
        """Liveness: the process serves requests."""
        return JSONResponse({"ok": True, "running": engine.running, "version": __version__})

    @app.get("/api/ready", include_in_schema=False)
    def ready() -> JSONResponse:
        """Readiness: event fan-out is wired up and the strategy store is loaded."""
        ok = broadcaster.is_started and bool(engine.strategies.all_strategies() or True)
        return JSONResponse({"ok": ok, "websocket_clients": broadcaster.client_count}, status_code=200 if ok else 503)

    return app


class NoCacheStaticFiles(StaticFiles):
    """Static files that browsers must revalidate (ETag/Last-Modified) so updates are picked up."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _deep_update(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _sync_webhook(engine: TradingEngine) -> None:
    notifier = engine.notifier
    for channel in notifier.channels:
        if isinstance(channel, WebhookChannel):
            notifier.remove_channel(channel)
    url = engine.config.notifications.webhook_url
    if url:
        notifier.add_channel(WebhookChannel(url))


def create_server(engine: TradingEngine, config_path: str | None = None, host: str | None = None,
                  port: int | None = None, log_level: str = "info") -> Any:
    """Build a uvicorn server for the web app (call ``.run()`` to serve; set ``.should_exit`` to stop)."""
    import uvicorn

    cfg = engine.config.web
    app = create_app(engine, config_path)
    return uvicorn.Server(uvicorn.Config(app, host=host or cfg.host, port=port or cfg.port, log_level=log_level,
                                         proxy_headers=True, forwarded_allow_ips="*", root_path=cfg.base_path))


def serve(engine: TradingEngine, config_path: str | None = None, host: str | None = None, port: int | None = None,
          open_browser: bool | None = None, log_level: str = "info",
          on_started: Callable[[str], None] | None = None) -> int:
    """Run the web server until interrupted (blocking)."""
    cfg = engine.config.web
    host = host or cfg.host
    port = port or cfg.port
    server = create_server(engine, config_path, host, port, log_level)
    url = f"http://{host}:{port}/"
    if open_browser if open_browser is not None else cfg.open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    if on_started:
        on_started(url)
    logger.info("Web UI available at %s", url)
    if engine.storage.count_users() == 0:
        logger.warning("No administrator account yet - open %s to create it", url)
        if host not in ("127.0.0.1", "localhost", "::1"):
            logger.warning("The web UI is exposed on %s without a password!", host)
    server.run()
    return 0


__all__ = ["Broadcaster", "create_app", "create_server", "serialize_event", "serve", "status_payload"]
