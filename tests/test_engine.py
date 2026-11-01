import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_bot.broker import PaperBroker
from trading_bot.config import AppConfig
from trading_bot.data import DemoProvider, MarketDataService, ProviderError, RateLimiter
from trading_bot.engine import (
    BarsUpdated,
    EngineStateChanged,
    Event,
    EventBus,
    PositionsChanged,
    ScanCompleted,
    SignalEvent,
    StatusEvent,
    TradingEngine,
)
from trading_bot.notify import CallbackChannel, Notifier
from trading_bot.storage import Storage
from trading_bot.strategy import (
    ACTION_BUY,
    ACTION_SELL,
    Signal,
    StrategyDefinition,
    StrategyStore,
)

from .conftest import FakeClock

EngineSetup = tuple[TradingEngine, list[Signal], list[Event], FakeClock]


class FailingProvider(DemoProvider):
    def get_bars(self, symbol: str, interval: str = "5min", limit: int = 300) -> pd.DataFrame:
        if symbol == "BAD":
            raise ProviderError("no such symbol")
        return super().get_bars(symbol, interval, limit)


def make_engine(tmp_path: Path, definitions: list[StrategyDefinition], watchlist: Sequence[str] = ("AAPL", "MSFT"),
                trading: bool = False, clock: FakeClock | None = None, provider: DemoProvider | None = None,
                per_minute: int = 1000, per_day: int = 100000, cash: float = 100_000.0,
                market_hours: bool = False) -> EngineSetup:
    """Build a fully wired engine on the demo provider with a controllable clock."""
    clock = clock or FakeClock(1_750_000_000.0)  # 2025-06-15 14:13 UTC, a Sunday
    config = AppConfig.from_dict({
        "watchlist": list(watchlist), "scan_interval": 1,
        "data": {"provider": "demo", "interval": "5min", "bars": 120, "cache_ttl_seconds": 1},
        "trading": {"trading_enabled": trading, "initial_cash": cash, "position_size_pct": 0.1, "max_positions": 2},
        "market_hours": {"enabled": market_hours},
    })
    provider = provider or DemoProvider(clock=clock.datetime)
    service = MarketDataService(provider, RateLimiter(per_minute, per_day, clock=clock), cache_ttl=1, clock=clock)
    store = StrategyStore(tmp_path / "strategies.json")
    store._definitions = list(definitions)
    store.save()
    received: list[Signal] = []
    notifier = Notifier([CallbackChannel(received.append)])
    engine = TradingEngine(config, service, store, notifier, PaperBroker(cash), Storage(":memory:"))
    engine._now = clock.datetime
    events: list[Event] = []
    engine.bus.subscribe(events.append)
    return engine, received, events, clock


def demo_provider(engine: TradingEngine) -> DemoProvider:
    provider = engine.data.provider
    assert isinstance(provider, DemoProvider)
    return provider


def last_close(engine: TradingEngine, symbol: str) -> float:
    latest = engine.latest_bars(symbol)
    assert latest is not None
    return float(latest.bars["close"].iloc[-1])


def always_buy(name: str = "buyer", **kw: Any) -> StrategyDefinition:
    return StrategyDefinition(name=name, buy_rule="close > 0", **kw)


def test_scan_produces_signals_and_notifies(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy()])
    signals = engine.scan_once()
    assert {s.symbol for s in signals} == {"AAPL", "MSFT"}
    assert all(s.action == ACTION_BUY for s in signals)
    assert len(received) == 2
    assert engine.stats.scans == 1 and engine.stats.signals == 2
    assert sum(isinstance(e, BarsUpdated) for e in events) == 2
    assert sum(isinstance(e, SignalEvent) for e in events) == 2
    done = [e for e in events if isinstance(e, ScanCompleted)]
    assert done and done[0].symbols_scanned == 2 and done[0].signals == 2
    assert engine.latest_bars("AAPL") is not None and "AAPL" in engine.latest_prices()
    assert len(engine.storage.recent_signals()) == 2
    snap = engine.snapshot()
    assert snap["scans"] == 1 and snap["running"] is False


def test_same_bar_is_not_notified_twice(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy()])
    engine.scan_once()
    clock.advance(5)  # cache expired, but still the same 5-minute bar
    assert engine.scan_once() == []
    assert len(received) == 2
    clock.advance(300)  # a new bar appears
    assert len(engine.scan_once()) == 2
    assert len(received) == 4


def test_dedupe_survives_restart_via_storage(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy()])
    engine.scan_once()
    # A new engine sharing the same storage must not re-notify signals already recorded.
    engine2 = TradingEngine(engine.config, engine.data, engine.strategies, Notifier([]), PaperBroker(1), engine.storage)
    assert engine2.scan_once() == []


def test_strategy_symbol_filter_and_disabled(tmp_path: Path) -> None:
    defs = [always_buy("only-msft", symbols=["MSFT"]), always_buy("off", enabled=False)]
    engine, received, events, clock = make_engine(tmp_path, defs)
    signals = engine.scan_once()
    assert [(s.symbol, s.strategy) for s in signals] == [("MSFT", "only-msft")]


def test_provider_errors_are_isolated(tmp_path: Path) -> None:
    clock = FakeClock(1_750_000_000.0)
    engine, received, events, clock = make_engine(tmp_path, [always_buy()], watchlist=("BAD", "AAPL"), clock=clock,
                                                  provider=FailingProvider(clock=clock.datetime))
    signals = engine.scan_once()
    assert [s.symbol for s in signals] == ["AAPL"]
    assert engine.stats.errors == 1
    assert any(isinstance(e, StatusEvent) and e.level == "error" and "BAD" in e.message for e in events)


def test_budget_exhaustion_reports_warning(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy()], per_minute=1)
    signals = engine.scan_once()
    assert [s.symbol for s in signals] == ["AAPL"]  # only one request allowed this minute
    assert any(isinstance(e, StatusEvent) and e.level == "warning" for e in events)
    assert engine.effective_refresh_interval() == pytest.approx(120.0)  # 2 symbols / 1 per minute


def test_strategy_exception_does_not_stop_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy("boom"), always_buy("fine")])
    strategies = engine.strategies.active_strategies()

    def explode(symbol: str, df: pd.DataFrame) -> list[Signal]:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(strategies[0], "evaluate", explode)
    monkeypatch.setattr(engine.strategies, "active_strategies", lambda: strategies)
    signals = engine.scan_once()
    assert {s.strategy for s in signals} == {"fine"}
    assert engine.stats.errors == 2
    assert "kaboom" in engine.stats.last_error


def test_paper_trading_executes_buy_then_sell(tmp_path: Path) -> None:
    defs = [StrategyDefinition(name="flip", buy_rule="close > 0", stop_loss_pct=5, take_profit_pct=10)]
    engine, received, events, clock = make_engine(tmp_path, defs, watchlist=("AAPL",), trading=True)
    signals = engine.scan_once()
    assert len(signals) == 1
    pos = engine.broker.position("AAPL")
    assert pos is not None
    price = signals[0].price
    assert pos.quantity == int(100_000 * 0.1 // price)
    assert pos.stop_loss == pytest.approx(price * 0.95) and pos.take_profit == pytest.approx(price * 1.10)
    assert any(isinstance(e, PositionsChanged) for e in events)
    assert engine.storage.recent_signals()[0]["executed"] == 1
    assert engine.storage.recent_fills()[0]["side"] == "BUY"

    # Second BUY on a new bar must not add to the existing position.
    clock.advance(300)
    engine.scan_once()
    still = engine.broker.position("AAPL")
    assert still is not None and still.quantity == pos.quantity

    # Now a SELL rule closes it.
    engine.strategies._definitions = [StrategyDefinition(name="flip", buy_rule="close < 0", sell_rule="close > 0")]
    clock.advance(300)
    signals = engine.scan_once()
    assert signals and signals[0].action == ACTION_SELL
    assert engine.broker.position("AAPL") is None
    assert [f["side"] for f in engine.storage.recent_fills()] == ["SELL", "BUY"]


def test_sell_signal_without_position_is_only_notified(tmp_path: Path) -> None:
    defs = [StrategyDefinition(name="s", buy_rule="close < 0", sell_rule="close > 0")]
    engine, received, events, clock = make_engine(tmp_path, defs, watchlist=("AAPL",), trading=True)
    signals = engine.scan_once()
    assert signals[0].action == ACTION_SELL
    assert received and engine.storage.recent_signals()[0]["executed"] == 0


def test_max_positions_and_insufficient_cash(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy()], watchlist=("AAPL", "MSFT", "TSLA"), trading=True)
    engine.scan_once()
    assert len(engine.broker.positions()) == 2  # max_positions
    engine2, received2, events2, clock2 = make_engine(tmp_path, [always_buy()], watchlist=("AAPL",), trading=True, cash=5.0)
    engine2.scan_once()
    assert not engine2.broker.positions()
    assert received2  # still notified


def test_protective_exit_generates_sell_signal(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy("x", enabled=False)], watchlist=("AAPL",), trading=True)
    engine.scan_once()
    price = last_close(engine, "AAPL")
    engine.broker.submit_market_order("AAPL", "BUY", 10, price, strategy="manual", stop_loss=price * 1.5)
    clock.advance(300)
    signals = engine.scan_once()
    assert len(signals) == 1
    assert signals[0].action == ACTION_SELL and "Stop loss" in signals[0].reason
    assert engine.broker.position("AAPL") is None
    # take profit path
    engine.broker.submit_market_order("AAPL", "BUY", 10, price, strategy="manual", take_profit=price * 0.5)
    clock.advance(300)
    signals = engine.scan_once()
    assert signals and "Take profit" in signals[0].reason


def test_watchlist_management(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [])
    assert engine.add_symbol(" tsla ") and not engine.add_symbol("TSLA") and not engine.add_symbol("")
    assert engine.watchlist == ["AAPL", "MSFT", "TSLA"] and engine.config.watchlist == engine.watchlist
    assert engine.remove_symbol("aapl") and not engine.remove_symbol("AAPL")
    assert engine.watchlist == ["MSFT", "TSLA"]
    assert engine.fetch_symbol("tsla") is not None
    assert engine.latest_bars("TSLA") is not None


def test_background_thread_start_stop(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy()])
    engine.start()
    assert engine.running
    engine.start()  # idempotent
    deadline = time.time() + 5
    while time.time() < deadline and not any(isinstance(e, ScanCompleted) for e in events):
        time.sleep(0.05)
    assert any(isinstance(e, ScanCompleted) for e in events)
    engine.request_scan()
    engine.stop()
    assert not engine.running
    states = [e.running for e in events if isinstance(e, EngineStateChanged)]
    assert states == [True, False]
    engine.close()


def test_loop_survives_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy()])
    calls = {"n": 0}

    def flaky(force: bool = False) -> list[Signal]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first scan explodes")
        return []

    monkeypatch.setattr(engine, "scan_once", flaky)
    engine.start()
    deadline = time.time() + 5
    while time.time() < deadline and calls["n"] < 2:
        engine.request_scan()
        time.sleep(0.05)
    engine.stop()
    assert calls["n"] >= 2
    assert engine.stats.errors == 1
    assert any(isinstance(e, StatusEvent) and "explodes" in e.message for e in events)


def test_event_bus_isolation() -> None:
    bus = EventBus()
    seen: list[Event] = []

    def bad(event: Event) -> None:
        raise RuntimeError("x")

    bus.subscribe(bad)
    bus.subscribe(seen.append)
    bus.publish(StatusEvent("hi"))
    assert len(seen) == 1
    bus.unsubscribe(seen.append)
    bus.publish(StatusEvent("again"))
    assert len(seen) == 1


def test_closed_bars_only_skips_forming_bar(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy()], watchlist=("AAPL",))
    engine.config.data.closed_bars_only = True  # demo bars end exactly at the clock => last bar still forming
    signals = engine.scan_once()
    assert len(signals) == 1
    latest = engine.latest_bars("AAPL")
    assert latest is not None
    last_bar = latest.bars.index[-1].to_pydatetime()
    assert signals[0].bar_time == last_bar - timedelta(minutes=5)
    engine.config.data.closed_bars_only = False
    clock.advance(1)
    signals = engine.scan_once()
    assert signals and signals[0].bar_time == last_bar


# ------------------------------------------------------------ market hours

MONDAY_NOON_ET = 1_750_089_600.0  # 2025-06-16 12:00 New York (EDT) == 16:00 UTC, a Monday


def test_market_hours_detection(tmp_path: Path) -> None:
    engine, *_ = make_engine(tmp_path, [], market_hours=True, clock=FakeClock(MONDAY_NOON_ET))
    assert engine.market_is_open()
    sunday = FakeClock(1_750_000_000.0)
    engine._now = sunday.datetime
    assert not engine.market_is_open()
    nxt = engine.next_market_open()
    assert nxt.weekday() == 0 and nxt.strftime("%H:%M") == "09:30"
    # 16:00 local is already closed; next open is the following day.
    engine._now = FakeClock(MONDAY_NOON_ET + 4 * 3600).datetime
    assert not engine.market_is_open()
    assert engine.next_market_open().weekday() == 1
    engine.config.market_hours.enabled = False
    assert engine.market_is_open()


def test_pacing_uses_session_length(tmp_path: Path) -> None:
    # Twelve Data style budget: 800/day, 8/min, 5 symbols.
    engine, *_ = make_engine(tmp_path, [], watchlist=("A", "B", "C", "D", "E"), per_minute=8, per_day=800,
                             market_hours=True)
    session = engine.config.market_hours.session_seconds()
    assert session == 6.5 * 3600
    assert engine.effective_refresh_interval() == pytest.approx(session * 5 / 800)  # ~2.4 minutes
    engine.config.market_hours.enabled = False
    assert engine.effective_refresh_interval() == pytest.approx(86400 * 5 / 800)  # 9 minutes over 24h


def test_closed_market_serves_cache_but_fetches_missing_symbols(tmp_path: Path) -> None:
    clock = FakeClock(1_750_000_000.0)  # Sunday
    engine, received, events, clock = make_engine(tmp_path, [always_buy()], market_hours=True, clock=clock)
    provider = demo_provider(engine)
    engine.scan_once()
    assert provider.calls == 2  # nothing cached yet -> fetched once
    done = [e for e in events if isinstance(e, ScanCompleted)][-1]
    assert not done.market_open and done.refreshed == 2
    clock.advance(3600)
    engine.scan_once()
    assert provider.calls == 2  # closed: cache reused even though it is old
    done = [e for e in events if isinstance(e, ScanCompleted)][-1]
    assert done.refreshed == 0
    engine.scan_once(force=True)
    assert provider.calls == 4  # forced refresh always hits the provider
    assert engine.add_symbol("NEW")
    engine.scan_once()
    assert provider.calls == 5  # new symbol fetched even while closed


def test_open_market_refreshes_on_pacing_interval(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [], market_hours=True, clock=FakeClock(MONDAY_NOON_ET),
                                                  per_minute=8, per_day=800)
    provider = demo_provider(engine)
    interval = engine.effective_refresh_interval()
    engine.scan_once()
    assert provider.calls == 2
    clock.advance(interval / 2)
    engine.scan_once()
    assert provider.calls == 2
    clock.advance(interval / 2 + 1)
    engine.scan_once()
    assert provider.calls == 4


def test_request_scan_force_refreshes_in_loop(tmp_path: Path) -> None:
    clock = FakeClock(1_750_000_000.0)  # Sunday: the loop would otherwise never refresh
    engine, received, events, clock = make_engine(tmp_path, [], market_hours=True, clock=clock)
    provider = demo_provider(engine)
    engine.start()
    deadline = time.time() + 5
    while time.time() < deadline and provider.calls < 2:
        time.sleep(0.02)
    assert provider.calls == 2
    engine.request_scan(force=True)
    deadline = time.time() + 5
    while time.time() < deadline and provider.calls < 4:
        time.sleep(0.02)
    engine.stop()
    assert provider.calls == 4


def test_fetch_failures_are_reported_per_symbol_and_cleared(tmp_path: Path) -> None:
    clock = FakeClock(1_750_000_000.0)

    class Flaky(DemoProvider):
        fail = False

        def get_bars(self, symbol: str, interval: str = "5min", limit: int = 300) -> pd.DataFrame:
            if self.fail:
                raise ProviderError("boom")
            return super().get_bars(symbol, interval, limit)

    provider = Flaky(clock=clock.datetime)
    engine, received, events, clock = make_engine(tmp_path, [], watchlist=("AAPL",), clock=clock, provider=provider)
    from trading_bot.engine import FetchFailed

    provider.fail = True
    assert engine.fetch_symbol("AAPL") is None
    failed = [e for e in events if isinstance(e, FetchFailed)]
    assert failed and failed[0].symbol == "AAPL" and not failed[0].has_cache and "boom" in failed[0].message
    assert engine.last_error("AAPL") == "boom"
    provider.fail = False
    assert engine.fetch_symbol("AAPL", force=True) is not None
    assert engine.last_error("AAPL") is None
    # A failed *refresh* keeps serving the cache and flags has_cache=True.
    provider.fail = True
    clock.advance(10)
    result = engine.fetch_symbol("AAPL", force=True)
    assert result is not None and not result.empty
    assert [e for e in events if isinstance(e, FetchFailed)][-1].has_cache
    assert engine.snapshot()["errors_by_symbol"] == {"AAPL": "boom"}


def test_background_workers_are_joined_on_close(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy()], watchlist=("AAPL",))
    thread = engine.scan_in_background(force=True)
    fetch = engine.fetch_in_background("MSFT", force=True)
    assert engine.wait_for_workers(5.0)
    engine.close()  # also waits for the workers before closing the storage
    assert not thread.is_alive() and not fetch.is_alive()
    assert engine.stats.scans == 1
    assert engine.latest_bars("MSFT") is not None
    assert not any(isinstance(e, StatusEvent) and e.level == "error" for e in events)


def test_reconfigure_data_swaps_provider_live(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy()], watchlist=("AAPL",))
    engine.scan_once()
    first = engine.data
    assert engine.latest_bars("AAPL") is not None
    engine.config.data.provider = "alphavantage"
    engine.config.data.api_key = ""
    service = engine.reconfigure_data()
    assert service is engine.data and service is not first
    assert service.provider.name == "alphavantage"
    assert engine.latest_bars("AAPL") is None  # cache dropped with the old provider
    assert any(isinstance(e, StatusEvent) and "alphavantage" in e.message for e in events)
    engine.scan_once()  # no key: reported per symbol, never raises
    error = engine.last_error("AAPL")
    assert error is not None and "API key" in error
    engine.config.data.provider = "demo"
    engine.reconfigure_data()
    assert engine.last_error("AAPL") is None
    engine.scan_once()
    assert engine.latest_bars("AAPL") is not None


# ---------------------------------------------------------- multi-account


def test_signals_execute_in_every_auto_trading_account(tmp_path: Path) -> None:
    """Users share the strategies but keep independent paper accounts; only auto-trading accounts execute."""
    engine, received, events, clock = make_engine(tmp_path, [always_buy()], watchlist=("AAPL",), trading=True)
    engine.accounts.ensure("bob", enabled=True)
    engine.accounts.ensure("carol", enabled=False)
    signals = engine.scan_once()
    assert len(signals) == 1 and len(received) == 1  # one signal, one notification
    admin, bob, carol = (engine.accounts.get(n) for n in (engine.accounts.default_name, "bob", "carol"))
    assert admin is not None and bob is not None and carol is not None
    assert admin.position("AAPL") is not None and bob.position("AAPL") is not None
    assert carol.position("AAPL") is None  # auto-trading off: notified only
    fills = engine.storage.recent_fills()
    assert sorted(f["account"] for f in fills) == sorted([engine.accounts.default_name, "bob"])
    assert [f["account"] for f in engine.storage.fills_for_account("bob")] == ["bob"]
    assert engine.storage.recent_fills(account="carol") == []

    # Enabling Carol later: she joins on the next (new-bar) signal without touching the others.
    engine.accounts.set_enabled("carol", True)
    clock.advance(300)
    engine.scan_once()
    assert carol.position("AAPL") is not None
    admin_pos = admin.position("AAPL")
    assert admin_pos is not None and admin_pos.quantity == fills[0]["quantity"]


def test_protective_exits_are_evaluated_per_account(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy("x", enabled=False)], watchlist=("AAPL",), trading=True)
    engine.scan_once()
    price = last_close(engine, "AAPL")
    bob = engine.accounts.ensure("bob", enabled=True)
    # Bob's position has a stop that is already breached; the admin's has a harmless one.
    bob.submit_market_order("AAPL", "BUY", 5, price, strategy="manual", stop_loss=price * 1.5)
    engine.broker.submit_market_order("AAPL", "BUY", 5, price, strategy="manual", stop_loss=price * 0.5)
    clock.advance(300)
    signals = engine.scan_once()
    assert len(signals) == 1 and signals[0].account == "bob" and "Stop loss" in signals[0].reason
    assert bob.position("AAPL") is None
    assert engine.broker.position("AAPL") is not None  # untouched
    sells = [f for f in engine.storage.recent_fills() if f["side"] == "SELL"]
    assert [f["account"] for f in sells] == ["bob"]
    assert signals[0].to_dict()["account"] == "bob"


def test_account_manager_persists_per_user_state(tmp_path: Path) -> None:
    from trading_bot.broker import AccountManager

    manager = AccountManager(1_000.0, data_dir=tmp_path, default_name="admin")
    bob = manager.ensure("bob", enabled=False)
    bob.submit_market_order("AAPL", "BUY", 2, 100.0)
    assert manager.is_enabled("bob") is False and manager.is_enabled("nobody") is False
    assert manager.names() == ["bob"]
    assert manager.default is manager.get("admin")
    assert [name for name, _ in manager.enabled()] == ["admin"]
    assert (tmp_path / "accounts" / "bob.json").exists()
    # A second manager on the same directory restores Bob's positions.
    again = AccountManager(1_000.0, data_dir=tmp_path, default_name="admin").ensure("bob")
    pos = again.position("AAPL")
    assert pos is not None and pos.quantity == 2 and again.cash() == pytest.approx(800.0)
    # Names are sanitised for the file system.
    manager.ensure("../evil name").submit_market_order("X", "BUY", 1, 1.0)
    assert (tmp_path / "accounts" / ".._evil_name.json").exists()  # stays inside the accounts directory
    manager.remove("bob")
    assert "bob" not in manager.names()


# ------------------------------------------------- execution model & manual trading


def test_paper_fills_apply_slippage_commission_and_ledger(tmp_path: Path) -> None:
    engine, received, events, clock = make_engine(tmp_path, [always_buy()], watchlist=("AAPL",), trading=True)
    engine.config.execution.slippage_model = "fixed"
    engine.config.execution.slippage_bps = 10
    engine.config.execution.commission_per_order = 2.5
    signals = engine.scan_once()
    fill = engine.storage.recent_fills()[0]
    assert fill["price"] == pytest.approx(signals[0].price * 1.001) and fill["commission"] == 2.5
    pos = engine.broker.position("AAPL")
    assert pos is not None and pos.quantity * fill["price"] + 2.5 <= 100_000 * 0.1 + fill["price"]  # sized to the budget
    ledger = engine.storage.transactions(engine.accounts.default_name)
    assert len(ledger) == 1 and ledger[0]["kind"] == "buy" and ledger[0]["fee"] == 2.5
    assert ledger[0]["amount"] == pytest.approx(-(pos.quantity * fill["price"] + 2.5))
    assert ledger[0]["balance_after"] == pytest.approx(engine.broker.cash()) and ledger[0]["reference"] == fill["id"]
    assert ledger[0]["metadata"]["source"] == "signal" and ledger[0]["symbol"] == "AAPL"


def test_resting_limit_orders_fill_during_scans(tmp_path: Path) -> None:
    from trading_bot.engine import OrderEvent

    engine, received, events, clock = make_engine(tmp_path, [always_buy("x", enabled=False)], watchlist=("AAPL",), trading=True)
    engine.scan_once()
    price = last_close(engine, "AAPL")
    broker = engine.broker
    assert isinstance(broker, PaperBroker)
    far = broker.submit_limit_order("AAPL", "BUY", 3, price * 0.5, strategy="manual")   # never reached
    near = broker.submit_limit_order("AAPL", "BUY", 2, price * 1.5, strategy="manual")  # marketable on the next bar
    clock.advance(300)
    engine.scan_once()
    assert [o.id for o in broker.pending_orders()] == [far.id]
    pos = broker.position("AAPL")
    assert pos is not None and pos.quantity == 2 and pos.avg_price == pytest.approx(price * 1.5)
    fills = engine.storage.recent_fills()
    assert len(fills) == 1 and fills[0]["side"] == "BUY" and fills[0]["quantity"] == 2
    ledger = engine.storage.transactions(engine.accounts.default_name)
    assert ledger[0]["metadata"]["source"] == "limit order"
    order_events = [e for e in events if isinstance(e, OrderEvent)]
    assert order_events and order_events[-1].kind == "filled" and order_events[-1].symbol == "AAPL"
    assert any(isinstance(e, PositionsChanged) and e.account == engine.accounts.default_name for e in events)
    # No new bar -> pending orders are not re-evaluated (and nothing crashes).
    engine.scan_once()
    assert [o.id for o in broker.pending_orders()] == [far.id]
    assert near.id not in {o.id for o in broker.pending_orders()}


def test_order_service_simulate_place_close_cancel(tmp_path: Path) -> None:
    from trading_bot.engine import OrderEvent
    from trading_bot.execution import OrderRequest
    from trading_bot.trading import OrderError, OrderService

    clock = FakeClock(1_750_000_000.0)
    engine, received, events, clock = make_engine(tmp_path, [always_buy("x", enabled=False)], watchlist=("AAPL",), trading=True,
                                                  clock=clock, provider=FailingProvider(clock=clock.datetime))
    engine.config.execution.commission_per_order = 1.0
    service = OrderService(engine)
    broker = engine.broker
    # Symbols outside the watchlist are fetched on demand for the snapshot.
    snapshot = service.snapshot("MSFT")
    assert snapshot.price > 0 and snapshot.bars > 0
    with pytest.raises(OrderError) as bad:
        service.simulate(broker, OrderRequest("AAPL", "BUY", 0))
    assert bad.value.status == 422
    sim = service.simulate(broker, OrderRequest("AAPL", "BUY", 10))
    assert sim.accepted and sim.commission == 1.0

    result = service.place("admin", broker, OrderRequest("AAPL", "BUY", 10, stop_loss_pct=5))
    assert result.status == "filled" and result.fill is not None and result.fill["quantity"] == 10
    assert result.transaction is not None and result.transaction["kind"] == "buy" and result.transaction["metadata"]["source"] == "manual"
    pos = broker.position("AAPL")
    assert pos is not None and pos.stop_loss == pytest.approx(result.fill["price"] * 0.95) and pos.strategy == "manual"
    assert any(isinstance(e, OrderEvent) and e.kind == "filled" for e in events)
    with pytest.raises(OrderError) as too_many:
        service.place("admin", broker, OrderRequest("AAPL", "SELL", 11))
    assert too_many.value.status == 409 and "only 10 held" in str(too_many.value)

    # Non-marketable limit orders rest; marketable ones fill at once.
    resting = service.place("admin", broker, OrderRequest("AAPL", "BUY", 1, "limit", 1.0))
    assert resting.status == "pending" and resting.order is not None and resting.order["limit_price"] == 1.0
    assert any(isinstance(e, OrderEvent) and e.kind == "placed" for e in events)
    immediate = service.place("admin", broker, OrderRequest("AAPL", "BUY", 1, "limit", snapshot.price * 10))
    assert immediate.status == "filled" and immediate.fill is not None and immediate.fill["price"] <= snapshot.price * 10
    cancelled = service.cancel_order("admin", broker, resting.order["id"])
    assert cancelled.id == resting.order["id"] and broker.pending_orders() == []
    with pytest.raises(OrderError) as missing:
        service.cancel_order("admin", broker, "nope")
    assert missing.value.status == 404

    closed = service.close_position("admin", broker, "aapl")
    assert closed.status == "filled" and closed.fill is not None and closed.fill["side"] == "SELL" and closed.fill["quantity"] == 11
    assert broker.position("AAPL") is None
    with pytest.raises(OrderError) as none_left:
        service.close_position("admin", broker, "AAPL")
    assert none_left.value.status == 404
    kinds = [t["kind"] for t in engine.storage.transactions("admin", newest_first=False)]
    assert kinds == ["buy", "buy", "sell"]
    with pytest.raises(OrderError) as unknown:
        service.snapshot("BAD")  # the provider rejects this symbol
    assert unknown.value.status == 409 and "no such symbol" in str(unknown.value)


def test_two_strategies_same_bar_open_one_position(tmp_path: Path) -> None:
    """Two strategies signalling the same symbol on one bar: both are notified, only the first executes."""
    defs = [always_buy("first"), always_buy("second")]
    engine, received, events, clock = make_engine(tmp_path, defs, watchlist=("AAPL",), trading=True)
    signals = engine.scan_once()
    assert [s.strategy for s in signals] == ["first", "second"] and len(received) == 2
    pos = engine.broker.position("AAPL")
    assert pos is not None and pos.strategy == "first"
    executed = {row["strategy"]: bool(row["executed"]) for row in engine.storage.recent_signals()}
    assert executed == {"first": True, "second": False}
    assert len(engine.storage.recent_fills()) == 1
