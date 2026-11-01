from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_bot.strategy.expression import (
    FUNCTIONS,
    Expression,
    ExpressionError,
    function_help,
    validate_expression,
)


def test_simple_comparison(ohlcv: pd.DataFrame) -> None:
    out = Expression("close > open").evaluate(ohlcv)
    assert out.dtype == bool
    assert out.equals(ohlcv["close"] > ohlcv["open"])


def test_indicator_rule_matches_direct_computation(ohlcv: pd.DataFrame) -> None:
    from trading_bot import indicators as ind
    out = Expression("rsi(14) < 30 and close > sma(5)").evaluate(ohlcv)
    expected = ((ind.rsi(ohlcv["close"], 14) < 30) & (ohlcv["close"] > ind.sma(ohlcv["close"], 5))).fillna(False)
    assert out.equals(expected.astype(bool))


def test_parameters_are_substituted(ohlcv: pd.DataFrame) -> None:
    expr = Expression("crossover(sma(fast), sma(slow))", {"fast", "slow"})
    out = expr.evaluate(ohlcv, {"fast": 5, "slow": 20})
    from trading_bot import indicators as ind
    expected = ind.crossover(ind.sma(ohlcv["close"], 5), ind.sma(ohlcv["close"], 20))
    assert out.equals(expected)


def test_missing_parameter_value_is_reported(ohlcv: pd.DataFrame) -> None:
    expr = Expression("rsi(period) < 30", {"period"})
    with pytest.raises(ExpressionError, match="Missing parameter"):
        expr.evaluate(ohlcv, {})


def test_nan_comparisons_are_false(ohlcv: pd.DataFrame) -> None:
    out = Expression("sma(50) > 0").evaluate(ohlcv)
    assert not out.iloc[:49].any()
    assert out.iloc[49:].all()


def test_scalar_only_expression_broadcasts(ohlcv: pd.DataFrame) -> None:
    out = Expression("1 < 2").evaluate(ohlcv)
    assert out.all() and len(out) == len(ohlcv)
    assert not Expression("not (1 < 2)").evaluate(ohlcv).any()


def test_chained_comparison_and_arithmetic(ohlcv: pd.DataFrame) -> None:
    out = Expression("low <= close <= high").evaluate(ohlcv)
    assert out.all()
    out = Expression("(high - low) / close * 100 >= 0").evaluate(ohlcv)
    assert out.all()
    out = Expression("-close < 0 and +close > 0").evaluate(ohlcv)
    assert out.all()


def test_keyword_arguments(ohlcv: pd.DataFrame) -> None:
    from trading_bot import indicators as ind
    out = Expression("sma(period=10, source=high) > 0").evaluate(ohlcv)
    assert out.iloc[-1]
    raw = Expression("ema(10, source=low)").evaluate_raw(ohlcv)
    assert np.allclose(raw.dropna(), ind.ema(ohlcv["low"], 10).dropna())


def test_evaluate_last_and_raw(ohlcv: pd.DataFrame) -> None:
    assert Expression("close > 0").evaluate_last(ohlcv) is True
    assert Expression("close > 0").evaluate_last(ohlcv.iloc[0:0]) is False
    raw = Expression("sma(3)").evaluate_raw(ohlcv)
    assert isinstance(raw, pd.Series)
    assert raw.iloc[-1] == pytest.approx(ohlcv["close"].iloc[-3:].mean())


def test_min_max_abs_prev_bars_since(ohlcv: pd.DataFrame) -> None:
    assert Expression("max(close, open) >= close").evaluate(ohlcv).all()
    assert Expression("min(close, 1) <= 1").evaluate(ohlcv).all()
    assert Expression("abs(close - open) >= 0").evaluate(ohlcv).all()
    prev = Expression("prev(close, 1)").evaluate_raw(ohlcv)
    assert prev.iloc[1] == ohlcv["close"].iloc[0]
    since = Expression("bars_since(close > prev(close))").evaluate_raw(ohlcv)
    assert since.dropna().min() == 0


def test_function_memoisation_across_calls(ohlcv: pd.DataFrame, monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses
    calls = {"n": 0}
    original = FUNCTIONS["sma"].func

    def counting(ctx: Any, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return original(ctx, *args, **kwargs)

    monkeypatch.setitem(FUNCTIONS, "sma", dataclasses.replace(FUNCTIONS["sma"], func=counting))
    Expression("sma(20) > close and sma(20) < high and sma(10) > 0").evaluate(ohlcv)
    assert calls["n"] == 2  # sma(20) computed once, sma(10) once


@pytest.mark.parametrize("bad", [
    "__import__('os').system('true')",
    "close.__class__",
    "close[0]",
    "lambda: 1",
    "(x for x in close)",
    "[close]",
    "{1: 2}",
    "close if close else open",
    "'text'",
    "None",
    "foo",
    "sma",
    "open.mean()",
    "2 ** 2 ** 10",
    "2 ** 1000",
    "close is open",
    "close in open",
    "sma(close)",
    "sma()",
    "sma(1, 2, 3)",
    "sma(**{'period': 3})",
    "highest(5, close)",
    "crossover(1, close)",
    "close < 'a'",
    "",
    "close >",
    "rsi(14) <",
])
def test_rejected_expressions(bad: str, ohlcv: pd.DataFrame) -> None:
    with pytest.raises(ExpressionError):
        Expression(bad).evaluate(ohlcv)


def test_unknown_parameter_name_is_unknown_name() -> None:
    with pytest.raises(ExpressionError, match="Unknown name"):
        Expression("rsi(period) < 30")  # no param names declared


def test_function_used_as_name_gives_helpful_error() -> None:
    with pytest.raises(ExpressionError, match="call it like"):
        Expression("rsi < 30")


def test_length_and_complexity_limits() -> None:
    with pytest.raises(ExpressionError, match="too long"):
        Expression("close > 0 and " * 200 + "close > 0")
    nested = "abs(" * 300 + "close" + ")" * 300
    with pytest.raises(ExpressionError):
        Expression(nested)


def test_indicator_calls_are_collected() -> None:
    expr = Expression("crossover(sma(5), sma(20)) and rsi(14) < 70")
    calls = expr.indicator_calls()
    assert "sma(5)" in calls and "sma(20)" in calls and "rsi(14)" in calls
    assert len(calls) == len(set(calls))


def test_validate_expression_helper() -> None:
    assert validate_expression("close > open") is None
    error = validate_expression("closer > open")
    assert error is not None and "Unknown" in error


def test_function_help_lists_all_functions() -> None:
    names = {sig.split("(")[0] for sig, _ in function_help()}
    assert names == set(FUNCTIONS)
