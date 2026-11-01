import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from trading_bot.strategy import (
    ACTION_BUY,
    ACTION_SELL,
    RuleStrategy,
    Signal,
    Strategy,
    StrategyDefinition,
    StrategyStore,
    default_definitions,
    load_python_strategies,
)


def test_definition_roundtrip_and_validation() -> None:
    d = StrategyDefinition(name="x", buy_rule="close > sma(n)", sell_rule="close < sma(n)", params={"n": 5},
                           symbols=["aapl"], stop_loss_pct=1.5)
    raw = json.loads(json.dumps(d.to_dict()))
    back = StrategyDefinition.from_dict(raw)
    assert back == d
    assert back.symbols == ["AAPL"]
    assert back.validate() == []
    assert back.applies_to("aapl") and not back.applies_to("MSFT")
    assert StrategyDefinition(name="all", buy_rule="close > 0").applies_to("ANY")


def test_definition_reports_problems() -> None:
    d = StrategyDefinition(name=" ", buy_rule="close >", sell_rule="foo", params={"close": 1, "1x": 2},
                           stop_loss_pct=-1, cooldown_bars=-2)
    problems = d.validate()
    assert any("name" in p.lower() for p in problems)
    assert any(p.startswith("Buy rule") for p in problems)
    assert any(p.startswith("Sell rule") for p in problems)
    assert any("Invalid parameter name: 'close'" in p for p in problems)
    assert any("'1x'" in p for p in problems)
    assert any("Stop loss" in p for p in problems)
    assert any("Cooldown" in p for p in problems)


def test_from_dict_requires_core_fields() -> None:
    with pytest.raises(ValueError):
        StrategyDefinition.from_dict({"name": "x"})


def test_rule_strategy_signals_and_evaluate(ohlcv: pd.DataFrame) -> None:
    d = StrategyDefinition(name="always", buy_rule="close > 0", sell_rule="close < 0")
    s = RuleStrategy(d)
    frame = s.signals(ohlcv)
    assert frame["buy"].all() and not frame["sell"].any()
    signals = s.evaluate("AAPL", ohlcv)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == ACTION_BUY and sig.symbol == "AAPL" and sig.strategy == "always"
    assert sig.price == ohlcv["close"].iloc[-1]
    assert sig.bar_time == ohlcv.index[-1].to_pydatetime()
    assert "close > 0" in sig.reason
    assert s.evaluate("AAPL", ohlcv.iloc[0:0]) == []


def test_rule_strategy_without_sell_rule(ohlcv: pd.DataFrame) -> None:
    s = RuleStrategy(StrategyDefinition(name="b", buy_rule="close > 0"))
    assert not s.signals(ohlcv)["sell"].any()
    assert s.sell_expr is None
    assert s.overlays() == []


def test_rule_strategy_rejects_invalid_definition() -> None:
    with pytest.raises(ValueError):
        RuleStrategy(StrategyDefinition(name="bad", buy_rule="nope"))


def test_cooldown_suppresses_repeated_signals(ohlcv: pd.DataFrame) -> None:
    s = RuleStrategy(StrategyDefinition(name="c", buy_rule="close > 0", cooldown_bars=3))
    frame = s.signals(ohlcv)
    assert frame["buy"].all()  # raw signals every bar
    # evaluate() applies the cooldown: the last bar fires only if the previous fire was > 3 bars ago.
    from trading_bot.strategy.base import _apply_cooldown
    cooled = _apply_cooldown(frame, 3)
    fired = cooled.index[cooled["buy"]]
    gaps = pd.Series(fired).diff().dropna().dt.total_seconds() / 300
    assert (gaps == 4).all()


def test_signal_key_and_describe() -> None:
    sig = Signal("AAPL", ACTION_SELL, "s", 10.5, datetime(2024, 1, 1, tzinfo=UTC), "why")
    assert sig.key == ("s", "AAPL", "SELL", "2024-01-01T00:00:00+00:00")
    assert sig.headline() == "SELL AAPL @ 10.50"
    assert "why" in sig.describe()
    d = sig.to_dict()
    assert d["bar_time"].startswith("2024-01-01")


def test_default_definitions_are_valid(ohlcv: pd.DataFrame) -> None:
    for d in default_definitions():
        assert d.validate() == []
        RuleStrategy(d).signals(ohlcv)


def test_store_creates_defaults_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "strategies.json"
    store = StrategyStore(path)
    store.load()
    assert path.exists()
    names = [d.name for d in store.definitions()]
    assert "RSI Reversal" in names
    events = []
    store.add_listener(lambda: events.append(1))

    store.upsert(StrategyDefinition(name="Mine", buy_rule="close > open"))
    assert store.get("Mine") is not None
    store.upsert(StrategyDefinition(name="Mine2", buy_rule="close > open"), previous_name="Mine")
    assert store.get("Mine") is None and store.get("Mine2") is not None
    with pytest.raises(ValueError):
        store.upsert(StrategyDefinition(name="RSI Reversal", buy_rule="close > open"), previous_name="Mine2")
    with pytest.raises(ValueError):
        store.upsert(StrategyDefinition(name="broken", buy_rule="???"))
    store.set_enabled("Mine2", False)
    mine2 = store.get("Mine2")
    assert mine2 is not None and mine2.enabled is False
    assert store.remove("Mine2") and not store.remove("Mine2")
    assert events  # listeners were notified

    again = StrategyStore(path)
    again.load()
    assert [d.name for d in again.definitions()] == [d.name for d in store.definitions()]


def test_store_active_strategies_skips_disabled_and_invalid(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps([
        {"name": "ok", "buy_rule": "close > 0", "enabled": True},
        {"name": "off", "buy_rule": "close > 0", "enabled": False},
        {"name": "broken", "buy_rule": "close >>> 0", "enabled": True},
    ]))
    store = StrategyStore(path)
    store.load()
    assert [s.name for s in store.active_strategies()] == ["ok"]
    assert [s.name for s in store.all_strategies()] == ["ok", "off"]


def test_store_rejects_non_list(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text("{}")
    with pytest.raises(ValueError):
        StrategyStore(path).load()


def test_python_plugin_loading(tmp_path: Path, ohlcv: pd.DataFrame) -> None:
    plugin_dir = tmp_path / "strategies"
    plugin_dir.mkdir()
    (plugin_dir / "my_plugin.py").write_text(
        "import pandas as pd\n"
        "from trading_bot.strategy import Strategy\n"
        "class Momentum(Strategy):\n"
        "    name = 'Momentum plugin'\n"
        "    def signals(self, df):\n"
        "        buy = df['close'] > df['close'].shift(1)\n"
        "        return pd.DataFrame({'buy': buy.fillna(False), 'sell': ~buy.fillna(False)})\n"
    )
    (plugin_dir / "_ignored.py").write_text("raise RuntimeError('should not import')\n")
    (plugin_dir / "broken.py").write_text("this is not python\n")
    plugins = list(load_python_strategies(plugin_dir))
    assert [p.name for p in plugins] == ["Momentum plugin"]
    assert isinstance(plugins[0], Strategy)
    signals = plugins[0].evaluate("X", ohlcv)
    assert len(signals) == 1

    store = StrategyStore(tmp_path / "s.json", plugin_dir=plugin_dir)
    store.load()
    assert any(s.name == "Momentum plugin" for s in store.active_strategies())
    store.set_enabled("Momentum plugin", False)
    assert not any(s.name == "Momentum plugin" for s in store.active_strategies())
    assert list(load_python_strategies(None)) == []
    assert list(load_python_strategies(tmp_path / "missing")) == []
