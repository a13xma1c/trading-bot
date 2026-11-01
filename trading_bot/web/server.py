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
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.requests import Request

from .. import __version__
from ..backtest import run_backtest
from ..broker import PaperBroker
from ..config import PROVIDER_BUDGETS, VALID_INTERVALS, AppConfig, ConfigError
from ..data import ProviderError, RateLimitError
from ..engine import (
    BarsUpdated,
    EngineStateChanged,
    Event,
    FetchFailed,
    PositionsChanged,
    ScanCompleted,
    SignalEvent,
    StatusEvent,
    TradingEngine,
)
from ..notify import WebhookChannel
from ..strategy import RuleStrategy, Signal, Strategy, StrategyDefinition, function_help
from .charting import bars_payload, marker_indices, overlay_payload

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


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
                logger.warning("Dropping event for a slow WebSocket client")


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
        return {"type": "positions"}
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
    }


def watchlist_payload(engine: TradingEngine) -> list[dict[str, Any]]:
    rows = []
    for symbol in engine.watchlist:
        result = engine.latest_bars(symbol) or engine.data.cached_bars(symbol, engine.config.data.interval)
        row: dict[str, Any] = {"symbol": symbol, "price": None, "change_pct": None, "time": None,
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


def positions_payload(engine: TradingEngine) -> dict[str, Any]:
    broker = engine.broker
    prices = engine.latest_prices()
    rows = []
    for symbol, pos in sorted(broker.positions().items()):
        last = prices.get(symbol, pos.avg_price)
        rows.append({"symbol": symbol, "quantity": pos.quantity, "avg_price": pos.avg_price, "last": last,
                     "pnl": pos.unrealized_pnl(last), "pnl_pct": pos.unrealized_pnl_pct(last),
                     "stop_loss": pos.stop_loss, "take_profit": pos.take_profit, "strategy": pos.strategy,
                     "opened_at": pos.opened_at.isoformat()})
    account = {"cash": broker.cash(), "equity": broker.equity(prices), "open_positions": len(rows),
               "paper": isinstance(broker, PaperBroker),
               "realized_pnl": broker.realized_pnl() if isinstance(broker, PaperBroker) else None}
    fills = engine.storage.recent_fills(100)
    return {"account": account, "positions": rows, "fills": fills}


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


class SettingsBody(BaseModel):
    scan_interval: int | None = None
    data: dict[str, Any] | None = None
    market_hours: dict[str, Any] | None = None
    notifications: dict[str, Any] | None = None
    trading: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(engine: TradingEngine, config_path: str | None = None, token: str | None = None) -> FastAPI:
    config: AppConfig = engine.config
    api_token = token if token is not None else config.web.token
    broadcaster = Broadcaster(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        broadcaster.start(asyncio.get_running_loop())
        try:
            yield
        finally:
            broadcaster.stop()

    app = FastAPI(title="Trading Bot", version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)
    app.state.engine = engine
    app.state.broadcaster = broadcaster

    # ------------------------------------------------------------- auth
    def _check_token(provided: str | None) -> None:
        if api_token and not (provided and secrets.compare_digest(provided, api_token)):
            raise HTTPException(status_code=401, detail="Invalid or missing API token")

    async def require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        provided = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token")
        _check_token(provided)

    auth: list[Any] = [Depends(require_token)]

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
        return watchlist_payload(engine)

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
        return {"symbol": symbol, "interval": config.data.interval, "bars": bars_payload(result.bars),
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

    @app.get("/api/positions", dependencies=auth)
    def get_positions() -> dict[str, Any]:
        return positions_payload(engine)

    @app.post("/api/account/reset", dependencies=auth)
    def reset_account() -> dict[str, Any]:
        broker = engine.broker
        if not isinstance(broker, PaperBroker):
            raise HTTPException(status_code=409, detail="Only the paper account can be reset")
        broker.initial_cash = config.trading.initial_cash
        broker.reset()
        return positions_payload(engine)

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
        data["options"] = {"providers": list(PROVIDER_BUDGETS), "intervals": list(VALID_INTERVALS),
                           "budgets": {k: {"per_minute": v[0], "per_day": v[1]} for k, v in PROVIDER_BUDGETS.items()}}
        data["config_path"] = config_path
        return data

    @app.put("/api/settings", dependencies=auth)
    def update_settings(body: SettingsBody) -> dict[str, Any]:
        raw = config.to_dict()
        if body.scan_interval is not None:
            raw["scan_interval"] = body.scan_interval
        for section in ("data", "market_hours", "notifications", "trading"):
            patch = getattr(body, section)
            if patch:
                if section == "data" and not patch.get("api_key"):
                    patch.pop("api_key", None)  # blank = keep the stored key
                if section == "notifications" and "email" in patch and not patch["email"].get("password"):
                    patch["email"].pop("password", None)
                _deep_update(raw[section], patch)
        try:
            updated = AppConfig.from_dict(raw)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"Invalid settings: {exc}") from exc
        provider_changed = updated.data.provider != config.data.provider or updated.data.api_key != config.data.api_key
        # Apply in place so the running engine sees the new values.
        config.scan_interval = updated.scan_interval
        config.data = updated.data
        config.market_hours = updated.market_hours
        config.notifications = updated.notifications
        config.trading = updated.trading
        _save_config()
        _sync_webhook(engine)
        return {"ok": True, "restart_required": provider_changed, "status": status_payload(engine)}

    # --------------------------------------------------------- websocket
    @app.websocket("/ws")
    async def websocket_events(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
        if api_token and not (token and secrets.compare_digest(token, api_token)):
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
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health", include_in_schema=False)
    def health() -> JSONResponse:
        return JSONResponse({"ok": True, "running": engine.running})

    return app


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
    return uvicorn.Server(uvicorn.Config(app, host=host or cfg.host, port=port or cfg.port, log_level=log_level))


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
    if cfg.token:
        logger.info("API token required (web.token / TRADING_BOT_WEB_TOKEN)")
    elif host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning("The web UI is exposed on %s without an API token - set web.token", host)
    server.run()
    return 0


__all__ = ["Broadcaster", "create_app", "create_server", "serialize_event", "serve", "status_payload"]
