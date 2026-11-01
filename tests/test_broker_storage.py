from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_bot.broker import BrokerError, Fill, PaperBroker
from trading_bot.storage import Storage
from trading_bot.strategy import ACTION_BUY, ACTION_SELL, Signal


def test_paper_broker_buy_sell_and_pnl() -> None:
    b = PaperBroker(10_000, commission=1.0)
    fill = b.submit_market_order("aapl", "buy", 10, 100.0, strategy="s", stop_loss=95.0, take_profit=110.0)
    assert fill.side == "BUY" and fill.symbol == "AAPL"
    assert b.cash() == pytest.approx(10_000 - 1000 - 1)
    pos = b.position("AAPL")
    assert pos is not None
    assert pos.quantity == 10 and pos.avg_price == 100 and pos.stop_loss == 95 and pos.take_profit == 110
    assert pos.unrealized_pnl(105) == pytest.approx(50) and pos.unrealized_pnl_pct(105) == pytest.approx(5)
    assert b.equity({"AAPL": 105}) == pytest.approx(8999 + 1050)

    b.submit_market_order("AAPL", "BUY", 10, 110.0)  # average up
    averaged = b.position("AAPL")
    assert averaged is not None and averaged.avg_price == pytest.approx(105)
    fill = b.submit_market_order("AAPL", "SELL", 20, 120.0)
    assert fill.realized_pnl == pytest.approx((120 - 105) * 20 - 1)
    assert b.position("AAPL") is None
    assert b.realized_pnl() == pytest.approx(fill.realized_pnl)
    assert len(b.fills()) == 3


def test_paper_broker_rejections() -> None:
    b = PaperBroker(100)
    with pytest.raises(BrokerError, match="Insufficient"):
        b.submit_market_order("AAPL", "BUY", 10, 100.0)
    with pytest.raises(BrokerError, match="no such position"):
        b.submit_market_order("AAPL", "SELL", 1, 100.0)
    with pytest.raises(BrokerError):
        b.submit_market_order("AAPL", "BUY", 0, 100.0)
    with pytest.raises(BrokerError):
        b.submit_market_order("AAPL", "BUY", 1, -1.0)
    with pytest.raises(BrokerError):
        b.submit_market_order("AAPL", "HOLD", 1, 1.0)


def test_paper_broker_persistence(tmp_path: Path) -> None:
    state = tmp_path / "broker.json"
    b = PaperBroker(5_000, state_file=state)
    b.submit_market_order("MSFT", "BUY", 2, 100.0, strategy="x", stop_loss=90.0)
    again = PaperBroker(5_000, state_file=state)
    assert again.cash() == pytest.approx(4_800)
    pos = again.position("MSFT")
    assert pos is not None
    assert pos.quantity == 2 and pos.stop_loss == 90.0 and pos.strategy == "x"
    assert len(again.fills()) == 1
    again.reset()
    assert again.cash() == 5_000 and not again.positions()
    state.write_text("{bad json")
    assert PaperBroker(5_000, state_file=state).cash() == 5_000


def test_storage_signals_and_fills(tmp_path: Path) -> None:
    st = Storage(tmp_path / "h.sqlite3")
    sig = Signal("AAPL", ACTION_BUY, "s", 10.0, datetime(2024, 1, 1, tzinfo=UTC), "r")
    assert st.record_signal(sig)
    assert not st.record_signal(sig)  # same id
    dup = Signal("AAPL", ACTION_BUY, "s", 10.0, datetime(2024, 1, 1, tzinfo=UTC), "r")
    assert not st.record_signal(dup)  # same (strategy, symbol, action, bar_time)
    assert st.has_signal(dup)
    other = Signal("AAPL", ACTION_SELL, "s", 11.0, datetime(2024, 1, 1, tzinfo=UTC))
    assert st.record_signal(other, executed=True)
    rows = st.recent_signals(10)
    assert len(rows) == 2
    assert st.recent_signals(10, symbol="aapl")[0]["symbol"] == "AAPL"
    st.mark_executed(sig.id)
    assert all(r["executed"] == 1 for r in st.recent_signals(10))
    objs = st.signals_as_objects(10)
    assert {o.action for o in objs} == {ACTION_BUY, ACTION_SELL}
    assert objs[0].bar_time.tzinfo is not None

    st.record_fill(Fill("AAPL", "BUY", 1, 10.0, datetime.now(UTC), "s"))
    assert st.recent_fills()[0]["symbol"] == "AAPL"
    st.close()

    reopened = Storage(tmp_path / "h.sqlite3")
    assert len(reopened.recent_signals()) == 2
    reopened.close()


def test_storage_fills_per_account_and_schema_migration(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.sqlite3"
    # A database from the single-account version: no ``account`` columns, old dedupe index.
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE signals (id TEXT PRIMARY KEY, created_at TEXT, bar_time TEXT, symbol TEXT, action TEXT,
                              strategy TEXT, price REAL, reason TEXT, executed INTEGER DEFAULT 0);
        CREATE UNIQUE INDEX signals_dedupe ON signals (strategy, symbol, action, bar_time);
        CREATE TABLE fills (id TEXT PRIMARY KEY, timestamp TEXT, symbol TEXT, side TEXT, quantity REAL, price REAL,
                            strategy TEXT, realized_pnl REAL);
        INSERT INTO fills VALUES ('f1', '2024-01-01T10:00:00+00:00', 'AAPL', 'BUY', 3, 10.0, 's', 0);
    """)
    conn.commit()
    conn.close()

    st = Storage(path)
    legacy = st.recent_fills()
    assert legacy[0]["account"] == ""  # migrated rows belong to the (unnamed) legacy account
    when = datetime(2024, 1, 2, tzinfo=UTC)
    st.record_fill(Fill("AAPL", "BUY", 1, 10.0, when, "s"), account="admin")
    st.record_fill(Fill("MSFT", "BUY", 2, 20.0, when, "s"), account="bob")
    st.record_fill(Fill("MSFT", "SELL", 2, 25.0, when, "s", realized_pnl=10.0), account="bob")
    assert len(st.recent_fills()) == 4
    assert [f["symbol"] for f in st.recent_fills(account="admin")] == ["AAPL"]
    bob = st.fills_for_account("bob")
    assert [(f["side"], f["realized_pnl"]) for f in bob] == [("BUY", 0.0), ("SELL", 10.0)]  # chronological
    assert st.fills_for_account("nobody") == []
    # The same bar may now trigger once per account (protective exits carry the account).
    base = Signal("AAPL", ACTION_SELL, "risk", 9.0, when, "stop")
    assert st.record_signal(base)
    assert st.record_signal(Signal("AAPL", ACTION_SELL, "risk", 9.0, when, "stop", account="bob"))
    assert not st.record_signal(Signal("AAPL", ACTION_SELL, "risk", 9.0, when, "stop", account="bob"))
    assert st.recent_signals(10)[0]["account"] == "bob"
    st.close()


def test_storage_users(tmp_path: Path) -> None:
    st = Storage(tmp_path / "u.sqlite3")
    assert st.count_users() == 0 and st.list_users() == []
    expires = datetime(2030, 1, 1, tzinfo=UTC)
    row = st.create_user("Alice", "Alice@Example.com", "hash-a", role="admin", email_verified=True)
    assert row["id"] == 1 and row["role"] == "admin" and row["email_verified"] == 1 and row["created_at"]
    bob = st.create_user("bob", "bob@example.com", "hash-b", verification_token="tok", verification_expires=expires,
                         auto_trade=False)
    assert bob["auto_trade"] == 0 and bob["verification_expires"] == expires.isoformat()
    assert st.count_users() == 2
    # Lookups are case-insensitive, by username or e-mail.
    assert st.find_user("alice") is not None and st.find_user("ALICE@example.com") is not None
    assert st.find_user("bob@example.com") is not None and st.find_user("nobody") is None
    assert st.find_user_by_token("tok") is not None and st.find_user_by_token("nope") is None
    assert st.get_user(2) is not None and st.get_user(99) is None
    # Uniqueness (case-insensitive) on both username and e-mail.
    with pytest.raises(ValueError, match="already"):
        st.create_user("ALICE", None, "x")
    with pytest.raises(ValueError, match="already"):
        st.create_user("alice2", "BOB@example.com", "x")
    st.create_user("no-mail-1", None, "x")
    st.create_user("no-mail-2", None, "x")  # several users without e-mail are fine
    # Updates.
    st.update_user(2, email_verified=True, verification_token=None, verification_expires=None, last_login=expires)
    updated = st.get_user(2)
    assert updated is not None and updated["email_verified"] == 1 and updated["verification_token"] is None
    assert updated["last_login"] == expires.isoformat()
    with pytest.raises(ValueError, match="already"):
        st.update_user(2, username="alice")
    with pytest.raises(ValueError, match="cannot update"):
        st.update_user(2, id=5)
    st.update_user(2)  # no-op
    assert [u["username"] for u in st.list_users()] == ["Alice", "bob", "no-mail-1", "no-mail-2"]
    st.close()


# ------------------------------------------------------ paper broker: orders & cash


def test_paper_broker_limit_orders(tmp_path: Path) -> None:
    broker = PaperBroker(1_000, state_file=tmp_path / "paper.json", commission=1.0)
    when = datetime(2024, 3, 4, 15, 0, tzinfo=UTC)
    with pytest.raises(BrokerError, match="Insufficient cash"):
        broker.submit_limit_order("AAPL", "BUY", 20, 100.0)
    with pytest.raises(BrokerError, match="position too small"):
        broker.submit_limit_order("AAPL", "SELL", 1, 100.0)
    with pytest.raises(BrokerError, match="Unknown side"):
        broker.submit_limit_order("AAPL", "HOLD", 1, 100.0)
    order = broker.submit_limit_order("aapl", "buy", 5, 90.0, strategy="manual", stop_loss=80.0, expires_after_bars=2)
    assert order.symbol == "AAPL" and order.side == "BUY" and order.id in {o.id for o in broker.pending_orders()}
    # Cash reserved by resting buys counts against further buys and withdrawals.
    with pytest.raises(BrokerError, match="reserved"):
        broker.submit_limit_order("MSFT", "BUY", 6, 100.0)
    with pytest.raises(BrokerError, match="available for withdrawal"):
        broker.withdraw(600)
    assert broker.withdraw(100) == 900
    # A bar that does not trade through the limit leaves the order resting.
    assert broker.process_pending("AAPL", high=100, low=95, when=when) == []
    assert broker.pending_orders()[0].bars_seen == 1
    # Resting orders survive a restart.
    reloaded = PaperBroker(1_000, state_file=tmp_path / "paper.json")
    assert [o.id for o in reloaded.pending_orders()] == [order.id] and reloaded.cash() == 900
    # The next bar dips below the limit -> filled at the limit price with the default commission.
    fills = broker.process_pending("AAPL", high=96, low=89, when=when)
    assert len(fills) == 1 and fills[0].price == 90.0 and fills[0].quantity == 5 and fills[0].timestamp == when
    assert fills[0].commission == 1.0 and broker.pending_orders() == []
    pos = broker.position("AAPL")
    assert pos is not None and pos.stop_loss == 80.0 and broker.cash() == pytest.approx(900 - 450 - 1)
    # Expiry: an order that is not hit within N bars disappears.
    stale = broker.submit_limit_order("AAPL", "SELL", 5, 200.0, expires_after_bars=2)
    assert broker.process_pending("AAPL", 100, 90, when) == [] and broker.pending_orders()
    assert broker.process_pending("AAPL", 100, 90, when) == [] and broker.pending_orders() == []
    # Cancel + unrelated symbols are untouched.
    keep = broker.submit_limit_order("AAPL", "SELL", 5, 150.0)
    other = broker.submit_limit_order("MSFT", "BUY", 1, 50.0)
    assert broker.process_pending("MSFT", 60, 55, when) == [] and len(broker.pending_orders()) == 2
    assert broker.cancel_order(keep.id) is True and broker.cancel_order(keep.id) is False
    assert [o.id for o in broker.pending_orders()] == [other.id]
    assert stale.id != keep.id
    # A resting sell whose position vanished is dropped instead of crashing.
    sell = broker.submit_limit_order("AAPL", "SELL", 5, 150.0)
    broker.submit_market_order("AAPL", "SELL", 5, 100.0)
    assert broker.process_pending("AAPL", 160, 140, when) == [] and sell.id not in {o.id for o in broker.pending_orders()}
    broker.reset()
    assert broker.pending_orders() == [] and broker.cash() == 1_000


def test_paper_broker_deposits_commission_override_and_close() -> None:
    broker = PaperBroker(100, commission=2.0)
    assert broker.supports_deposits and broker.is_paper and broker.describe()["name"] == "paper"
    assert broker.deposit(50) == 150
    with pytest.raises(BrokerError):
        broker.deposit(0)
    with pytest.raises(BrokerError):
        broker.withdraw(-1)
    fill = broker.submit_market_order("AAPL", "BUY", 1, 100.0, commission=0.5)
    assert fill.commission == 0.5 and broker.cash() == pytest.approx(49.5)
    when = datetime(2024, 1, 1, tzinfo=UTC)
    fill = broker.submit_market_order("AAPL", "SELL", 1, 110.0, when=when)  # default commission applies
    assert fill.commission == 2.0 and fill.timestamp == when and fill.realized_pnl == pytest.approx(10 - 2)
    broker.submit_market_order("MSFT", "BUY", 2, 10.0, strategy="s")
    closed = broker.close_position("msft", 12.0)
    assert closed.side == "SELL" and closed.quantity == 2 and closed.strategy == "s" and broker.position("MSFT") is None
    with pytest.raises(BrokerError, match="No open position"):
        broker.close_position("MSFT", 12.0)


def test_account_manager_external_broker(tmp_path: Path) -> None:
    from trading_bot.broker import AccountManager, Broker, Position

    class FakeExternal(Broker):
        name = "fake"

        def submit_market_order(self, symbol: str, side: str, quantity: float, price: float, strategy: str = "",
                                stop_loss: float | None = None, take_profit: float | None = None) -> Fill:
            return Fill(symbol, side, quantity, price, datetime.now(UTC), strategy)

        def positions(self) -> dict[str, Position]:
            return {}

        def cash(self) -> float:
            return 42.0

        @property
        def is_paper(self) -> bool:
            return False

    manager = AccountManager(1_000, data_dir=tmp_path)
    paper = manager.ensure("bob")
    assert isinstance(paper, PaperBroker) and manager.paper_account("bob") is paper
    external = FakeExternal()
    assert manager.set_broker("bob", external) is external and manager.get("bob") is external
    assert not external.supports_deposits and external.describe() == {"name": "fake", "paper": False, "label": "fake"}
    with pytest.raises(BrokerError, match="does not support limit orders"):
        external.submit_limit_order("AAPL", "BUY", 1, 1.0)
    assert external.pending_orders() == [] and external.cancel_order("x") is False and external.process_pending("A", 1, 1, datetime.now(UTC)) == []
    # Paper state is kept while the external broker is active and comes back on disconnect.
    paper.deposit(10)
    assert manager.set_broker("bob", None) is paper and manager.get("bob") is paper and paper.cash() == 1_010
    # An external broker can also seed a brand-new account.
    carol = manager.ensure("carol", enabled=True, broker=FakeExternal())
    assert isinstance(carol, FakeExternal) and isinstance(manager.paper_account("carol"), PaperBroker)
