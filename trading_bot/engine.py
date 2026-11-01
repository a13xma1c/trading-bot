"""The trading engine: periodically scans the watchlist, evaluates strategies,
raises notifications and (optionally) routes orders to a broker.

The engine is GUI-agnostic.  It publishes events on an :class:`EventBus`; the
GUI and the headless runner subscribe to those.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .broker import Broker, BrokerError, PaperBroker
from .config import INTERVAL_SECONDS, AppConfig
from .data import BarsResult, MarketDataService, ProviderError, RateLimitError
from .notify import Notifier
from .storage import Storage
from .strategy import ACTION_BUY, ACTION_SELL, Signal, Strategy, StrategyStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass
class Event:
    pass


@dataclass
class BarsUpdated(Event):
    symbol: str
    result: BarsResult


@dataclass
class SignalEvent(Event):
    signal: Signal
    executed: bool = False


@dataclass
class StatusEvent(Event):
    message: str
    level: str = "info"  # info | warning | error


@dataclass
class FetchFailed(Event):
    symbol: str
    message: str
    has_cache: bool  # stale data is still available for this symbol


@dataclass
class ScanCompleted(Event):
    started_at: datetime
    duration: float
    symbols_scanned: int
    signals: int
    budget: str
    market_open: bool = True
    refreshed: int = 0  # symbols for which new data was fetched


@dataclass
class PositionsChanged(Event):
    pass


@dataclass
class EngineStateChanged(Event):
    running: bool


class EventBus:
    """Minimal thread-safe publish/subscribe hub."""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[Event], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def publish(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for cb in subscribers:
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                logger.exception("Event subscriber failed for %s", type(event).__name__)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class EngineStats:
    scans: int = 0
    signals: int = 0
    errors: int = 0
    last_scan: datetime | None = None
    last_error: str = ""
    recent_signals: list[Signal] = field(default_factory=list)


class TradingEngine:
    def __init__(self, config: AppConfig, data: MarketDataService, strategies: StrategyStore,
                 notifier: Notifier, broker: Broker | None = None, storage: Storage | None = None,
                 bus: EventBus | None = None):
        self.config = config
        self.data = data
        self.strategies = strategies
        self.notifier = notifier
        self.broker = broker or PaperBroker(config.trading.initial_cash)
        self.storage = storage or Storage(":memory:")
        self.bus = bus or EventBus()
        self.stats = EngineStats()
        self._watchlist: list[str] = list(config.watchlist)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._seen: set[tuple[str, str, str, str]] = set()
        self._latest: dict[str, BarsResult] = {}
        self._errors: dict[str, str] = {}
        self._force_next = False
        self._workers: list[threading.Thread] = []  # one-off scans / fetches started by the GUI
        self._now: Callable[[], datetime] = lambda: datetime.now(UTC)  # injectable clock (tests)

    # --------------------------------------------------------------- state
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def watchlist(self) -> list[str]:
        with self._lock:
            return list(self._watchlist)

    def add_symbol(self, symbol: str) -> bool:
        symbol = symbol.strip().upper()
        if not symbol:
            return False
        with self._lock:
            if symbol in self._watchlist:
                return False
            self._watchlist.append(symbol)
            self.config.watchlist = list(self._watchlist)
        self.request_scan()
        return True

    def remove_symbol(self, symbol: str) -> bool:
        symbol = symbol.strip().upper()
        with self._lock:
            if symbol not in self._watchlist:
                return False
            self._watchlist.remove(symbol)
            self.config.watchlist = list(self._watchlist)
            self._latest.pop(symbol, None)
        return True

    def latest_bars(self, symbol: str) -> BarsResult | None:
        with self._lock:
            return self._latest.get(symbol.upper())

    def latest_prices(self) -> dict[str, float]:
        with self._lock:
            return {s: float(r.bars["close"].iloc[-1]) for s, r in self._latest.items() if not r.empty}

    def last_error(self, symbol: str) -> str | None:
        with self._lock:
            return self._errors.get(symbol.upper())

    def effective_refresh_interval(self) -> float:
        """Seconds between refreshes of one symbol so the daily budget lasts the trading session.

        The daily request budget is spread over the market session (or over 24h
        when market hours are disabled); the per-minute budget and the configured
        cache TTL act as additional floors.
        """
        n = max(1, len(self.watchlist))
        per_minute, per_day = self.data.limiter.per_minute, self.data.limiter.per_day
        mh = self.config.market_hours
        window = float(mh.session_seconds()) if mh.enabled else 86400.0
        pacing_day = window * n / per_day
        pacing_minute = 60.0 * n / per_minute
        return max(float(self.config.data.cache_ttl_seconds), pacing_day, pacing_minute)

    # --------------------------------------------------------- market hours
    def market_is_open(self, now: datetime | None = None) -> bool:
        mh = self.config.market_hours
        if not mh.enabled:
            return True
        local = (now or self._now()).astimezone(ZoneInfo(mh.timezone))
        if local.weekday() not in mh.weekdays:
            return False
        open_t = _time_of_day(mh.open)
        close_t = _time_of_day(mh.close)
        return open_t <= local.time() < close_t

    def next_market_open(self, now: datetime | None = None) -> datetime:
        mh = self.config.market_hours
        tz = ZoneInfo(mh.timezone)
        local = (now or self._now()).astimezone(tz)
        open_t = _time_of_day(mh.open)
        for day in range(0, 8):
            candidate = (local + timedelta(days=day)).replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
            if candidate.weekday() in mh.weekdays and candidate > local:
                return candidate
        return local  # unreachable with a non-empty weekday list

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._run, name="trading-engine", daemon=True)
        self._thread.start()
        self.bus.publish(EngineStateChanged(True))
        logger.info("Engine started (interval %ss, %d symbols)", self.config.scan_interval, len(self.watchlist))

    def stop(self, timeout: float = 10.0) -> None:
        if not self.running:
            return
        self._stop.set()
        self._wake.set()
        assert self._thread is not None
        self._thread.join(timeout)
        self._thread = None
        self.bus.publish(EngineStateChanged(False))
        logger.info("Engine stopped")

    def _spawn(self, name: str, target: Callable[[], Any]) -> threading.Thread:
        with self._lock:
            self._workers = [t for t in self._workers if t.is_alive()]
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._workers.append(thread)
        thread.start()
        return thread

    def scan_in_background(self, force: bool = False) -> threading.Thread:
        """Run one scan on a worker thread (used by the GUI when the loop is not running)."""
        def work() -> None:
            try:
                self.scan_once(force=force)
            except Exception as exc:  # noqa: BLE001
                self.stats.errors += 1
                self.stats.last_error = str(exc)
                logger.exception("Background scan failed")
                self.bus.publish(StatusEvent(f"Scan failed: {exc}", "error"))
        return self._spawn("manual-scan", work)

    def fetch_in_background(self, symbol: str, force: bool = False) -> threading.Thread:
        """Fetch one symbol on a worker thread; errors are reported through the event bus."""
        return self._spawn(f"fetch-{symbol}", lambda: self.fetch_symbol(symbol, force=force))

    def wait_for_workers(self, timeout: float = 10.0) -> bool:
        """Block until background scans/fetches started via the engine have finished."""
        with self._lock:
            workers = list(self._workers)
        deadline = time.monotonic() + timeout
        for thread in workers:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in workers)

    def _join_workers(self, timeout: float) -> None:
        self.wait_for_workers(timeout)

    def request_scan(self, force: bool = False) -> None:
        """Wake the scan loop; ``force`` refreshes every symbol regardless of pacing."""
        if force:
            self._force_next = True
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            force, self._force_next = self._force_next, False
            try:
                self.scan_once(force=force)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive no matter what
                self.stats.errors += 1
                self.stats.last_error = str(exc)
                logger.exception("Unhandled error in scan loop")
                self.bus.publish(StatusEvent(f"Scan failed: {exc}", "error"))
            self._wake.wait(timeout=self.config.scan_interval)
            self._wake.clear()

    # ----------------------------------------------------------------- scan
    def scan_once(self, force: bool = False) -> list[Signal]:
        """Refresh the watchlist and evaluate strategies.

        Data is refreshed according to the pacing interval while the market is
        open; when it is closed cached data is reused (a symbol without any cache
        is still fetched once).  ``force`` refreshes everything now, subject only
        to the hard API limits.
        """
        started = self._now()
        t0 = time.monotonic()
        strategies = self.strategies.active_strategies()
        symbols = self.watchlist
        market_open = self.market_is_open(started)
        refresh_interval = self.effective_refresh_interval()
        self.data.cache_ttl = refresh_interval
        produced: list[Signal] = []
        scanned = refreshed = 0
        for symbol in symbols:
            if self._stop.is_set():
                break
            if force:
                max_age = 0.0
            elif market_open:
                max_age = refresh_interval
            else:
                max_age = math.inf  # keep whatever we have until the next session
            before = self.latest_bars(symbol)
            result = self._fetch(symbol, max_age=max_age)
            if result is None or result.empty:
                continue
            scanned += 1
            if result is not before:
                refreshed += 1
            bars = self._tradable_bars(result.bars)
            if bars.empty:
                continue
            produced.extend(self._evaluate_symbol(symbol, bars, strategies))
            produced.extend(self._check_protective_exits(symbol, bars))
        self.stats.scans += 1
        self.stats.last_scan = started
        duration = time.monotonic() - t0
        budget = self.data.budget().describe()
        self.bus.publish(ScanCompleted(started, duration, scanned, len(produced), budget, market_open, refreshed))
        logger.info("Scan complete: %d/%d symbols (%d refreshed), %d signals, %.1fs, market %s. %s", scanned,
                    len(symbols), refreshed, len(produced), duration, "open" if market_open else "closed", budget)
        return produced

    def _tradable_bars(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Optionally drop the last bar when it has not closed yet."""
        if not self.config.data.closed_bars_only or bars.empty:
            return bars
        step = INTERVAL_SECONDS.get(self.config.data.interval, 0)
        now = self._now()
        last = bars.index[-1].to_pydatetime()
        if (now - last).total_seconds() < step:
            return bars.iloc[:-1]
        return bars

    def fetch_symbol(self, symbol: str, force: bool = False) -> BarsResult | None:
        """Fetch bars for one symbol outside the scan loop (used by the GUI).

        ``force`` bypasses the pacing interval (not the hard API limits); a stale
        cache entry is kept if the refresh fails.
        """
        symbol = symbol.strip().upper()
        return self._fetch(symbol, max_age=0.0 if force else None)

    def _fetch(self, symbol: str, max_age: float | None = None) -> BarsResult | None:
        try:
            result = self.data.get_bars(symbol, self.config.data.interval, self.config.data.bars, max_age=max_age)
        except RateLimitError as exc:
            self._record_error(symbol, str(exc), has_cache=False)
            self.bus.publish(StatusEvent(f"{symbol}: {exc}", "warning"))
            return None
        except ProviderError as exc:
            self.stats.errors += 1
            self.stats.last_error = str(exc)
            self._record_error(symbol, str(exc), has_cache=False)
            self.bus.publish(StatusEvent(f"{symbol}: {exc}", "error"))
            return None
        if result.error:
            # Refresh failed but older data is available; keep serving it and say so.
            self._record_error(symbol, result.error, has_cache=True)
        else:
            with self._lock:
                self._errors.pop(symbol, None)
        with self._lock:
            previous = self._latest.get(symbol)
            self._latest[symbol] = result
        if previous is not result:
            self.bus.publish(BarsUpdated(symbol, result))
        return result

    def _record_error(self, symbol: str, message: str, has_cache: bool) -> None:
        with self._lock:
            changed = self._errors.get(symbol) != message
            self._errors[symbol] = message
        if changed:
            logger.warning("%s: %s%s", symbol, message, " (serving cached data)" if has_cache else "")
            self.bus.publish(FetchFailed(symbol, message, has_cache))

    def _evaluate_symbol(self, symbol: str, bars: pd.DataFrame, strategies: list[Strategy]) -> list[Signal]:
        out: list[Signal] = []
        for strategy in strategies:
            if not strategy.applies_to(symbol):
                continue
            try:
                signals = strategy.evaluate(symbol, bars)
            except Exception as exc:  # noqa: BLE001 - one bad strategy must not stop the others
                self.stats.errors += 1
                self.stats.last_error = f"{strategy.name}: {exc}"
                logger.error("Strategy %r failed on %s: %s", strategy.name, symbol, exc)
                self.bus.publish(StatusEvent(f"Strategy {strategy.name} failed on {symbol}: {exc}", "error"))
                continue
            for signal in signals:
                if self._handle_signal(signal, strategy):
                    out.append(signal)
        return out

    def _check_protective_exits(self, symbol: str, bars: pd.DataFrame) -> list[Signal]:
        pos = self.broker.position(symbol)
        if pos is None:
            return []
        price = float(bars["close"].iloc[-1])
        bar_time = bars.index[-1].to_pydatetime()
        reason = None
        if pos.stop_loss is not None and price <= pos.stop_loss:
            reason = f"Stop loss hit ({price:.2f} <= {pos.stop_loss:.2f})"
        elif pos.take_profit is not None and price >= pos.take_profit:
            reason = f"Take profit hit ({price:.2f} >= {pos.take_profit:.2f})"
        if reason is None:
            return []
        signal = Signal(symbol, ACTION_SELL, pos.strategy or "risk-management", price, bar_time, reason)
        return [signal] if self._handle_signal(signal, None) else []

    # -------------------------------------------------------------- signals
    def _handle_signal(self, signal: Signal, strategy: Strategy | None) -> bool:
        """Deduplicate, persist, notify and optionally execute. Returns True if new."""
        key = signal.key
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            if len(self._seen) > 10_000:
                # Storage still guarantees de-duplication; the set is only a fast path.
                self._seen.clear()
        if not self.storage.record_signal(signal):
            return False  # already stored in a previous session
        executed = False
        if self.config.trading.trading_enabled:
            executed = self._execute(signal, strategy)
            if executed:
                self.storage.mark_executed(signal.id)
        self.stats.signals += 1
        self.stats.recent_signals.append(signal)
        del self.stats.recent_signals[:-200]
        self.notifier.notify(signal)
        self.bus.publish(SignalEvent(signal, executed))
        return True

    def _execute(self, signal: Signal, strategy: Strategy | None) -> bool:
        try:
            if signal.action == ACTION_BUY:
                if self.broker.position(signal.symbol) is not None:
                    logger.info("Already holding %s; not adding to position", signal.symbol)
                    return False
                if len(self.broker.positions()) >= self.config.trading.max_positions:
                    logger.info("Max positions reached; skipping BUY %s", signal.symbol)
                    return False
                equity = self.broker.equity(self.latest_prices())
                budget = min(self.broker.cash(), equity * self.config.trading.position_size_pct)
                qty = math.floor(budget / signal.price)
                if qty < 1:
                    logger.info("Not enough cash to buy one share of %s", signal.symbol)
                    return False
                sl = tp = None
                if strategy is not None:
                    if strategy.stop_loss_pct:
                        sl = signal.price * (1 - strategy.stop_loss_pct / 100.0)
                    if strategy.take_profit_pct:
                        tp = signal.price * (1 + strategy.take_profit_pct / 100.0)
                fill = self.broker.submit_market_order(signal.symbol, ACTION_BUY, qty, signal.price,
                                                       signal.strategy, sl, tp)
            else:
                pos = self.broker.position(signal.symbol)
                if pos is None:
                    logger.info("No position in %s to sell", signal.symbol)
                    return False
                fill = self.broker.submit_market_order(signal.symbol, ACTION_SELL, pos.quantity, signal.price,
                                                       signal.strategy)
        except BrokerError as exc:
            logger.error("Order rejected: %s", exc)
            self.bus.publish(StatusEvent(f"Order rejected: {exc}", "error"))
            return False
        self.storage.record_fill(fill)
        self.bus.publish(PositionsChanged())
        return True

    # ------------------------------------------------------------ utilities
    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "watchlist": self.watchlist,
            "scans": self.stats.scans,
            "signals": self.stats.signals,
            "errors": self.stats.errors,
            "last_scan": self.stats.last_scan.isoformat() if self.stats.last_scan else None,
            "budget": self.data.budget().describe(),
            "refresh_interval": self.effective_refresh_interval(),
            "market_open": self.market_is_open(),
            "errors_by_symbol": dict(self._errors),
        }

    def close(self) -> None:
        self.stop()
        self._join_workers(10.0)
        self.data.close()
        self.storage.close()


def _time_of_day(text: str) -> dtime:
    hours, minutes = str(text).split(":")
    return dtime(int(hours), int(minutes))
