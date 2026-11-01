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
