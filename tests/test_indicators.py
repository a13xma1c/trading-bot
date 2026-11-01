from collections.abc import Sequence

import numpy as np
import pandas as pd
import pytest

from trading_bot import indicators as ind


def series(values: Sequence[float] | np.ndarray) -> pd.Series:
    return pd.Series(values, dtype=float, index=pd.date_range("2024-01-01", periods=len(values), freq="D", tz="UTC"))


def test_sma_matches_hand_computation() -> None:
    s = series([1, 2, 3, 4, 5])
    out = ind.sma(s, 3)
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert out.iloc[2:].tolist() == [2.0, 3.0, 4.0]


def test_ema_recursion() -> None:
    s = series([10, 11, 12, 13, 14, 15])
    out = ind.ema(s, 3)
    # pandas ewm(adjust=False) with min_periods=3: first defined value at index 2.
    alpha = 2 / 4
    expected = 10.0
    for v in s.iloc[1:]:
        expected = alpha * v + (1 - alpha) * expected
    assert out.iloc[-1] == pytest.approx(expected)
    assert np.isnan(out.iloc[1])


def test_rsi_bounds_and_extremes() -> None:
    rising = series(np.arange(1, 40, dtype=float))
    out = ind.rsi(rising, 14)
    assert out.dropna().between(0, 100).all()
    assert out.iloc[-1] == pytest.approx(100.0)  # no losses at all
    falling = series(np.arange(40, 1, -1, dtype=float))
    assert ind.rsi(falling, 14).iloc[-1] == pytest.approx(0.0)


def test_rsi_reference_value() -> None:
    # Classic 14-period worked example (StockCharts "RSI" article); TA-Lib reproduces these values.
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
              46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18,
              44.22, 44.57, 43.42, 42.66, 43.13]
    expected = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71, 50.42, 39.99, 41.46,
                41.87, 45.46, 37.30, 33.08, 37.77]
    out = ind.rsi(series(closes), 14)
    assert np.isnan(out.iloc[13])
    # The published table was built from rounded intermediates, hence the tolerance.
    assert out.iloc[14:].tolist() == pytest.approx(expected, abs=0.1)

    # Exact comparison with a straightforward loop implementation of Wilder's method.
    deltas = np.diff(closes)
    gains, losses = np.clip(deltas, 0, None), np.clip(-deltas, 0, None)
    avg_gain, avg_loss = gains[:14].mean(), losses[:14].mean()
    reference = [100 - 100 / (1 + avg_gain / avg_loss)]
    for g, lo in zip(gains[14:], losses[14:]):
        avg_gain = (avg_gain * 13 + g) / 14
        avg_loss = (avg_loss * 13 + lo) / 14
        reference.append(100 - 100 / (1 + avg_gain / avg_loss))
    assert out.iloc[14:].tolist() == pytest.approx(reference, abs=1e-9)


def test_atr_seeded_like_talib() -> None:
    highs = [10, 11, 12, 11, 13, 14, 13]
    lows = [9, 10, 10, 10, 11, 12, 12]
    closes = [9.5, 10.5, 11, 10.5, 12.5, 13, 12.5]
    idx = pd.date_range("2024-01-01", periods=7, freq="D", tz="UTC")
    df = pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes, "volume": 1}, index=idx, dtype=float)
    tr = ind.true_range(df).to_numpy()
    out = ind.atr(df, 3)
    assert np.isnan(out.iloc[2])
    seed = tr[1:4].mean()
    assert out.iloc[3] == pytest.approx(seed)
    assert out.iloc[4] == pytest.approx((seed * 2 + tr[4]) / 3)


def test_macd_columns_and_relationship(ohlcv: pd.DataFrame) -> None:
    out = ind.macd(ohlcv["close"])
    assert list(out.columns) == ["macd", "signal", "hist"]
    valid = out.dropna()
    assert len(valid) > 100
    assert np.allclose(valid["hist"], valid["macd"] - valid["signal"])


def test_bollinger_bands_symmetric(ohlcv: pd.DataFrame) -> None:
    bands = ind.bollinger(ohlcv["close"], 20, 2)
    valid = bands.dropna()
    assert (valid["upper"] >= valid["middle"]).all()
    assert (valid["lower"] <= valid["middle"]).all()
    assert np.allclose(valid["upper"] - valid["middle"], valid["middle"] - valid["lower"])
    assert np.allclose(valid["middle"], ind.sma(ohlcv["close"], 20).dropna())


def test_atr_positive_and_true_range(ohlcv: pd.DataFrame) -> None:
    tr = ind.true_range(ohlcv)
    assert np.isnan(tr.iloc[0]) and (tr.dropna() >= 0).all()
    atr = ind.atr(ohlcv, 14)
    assert (atr.dropna() > 0).all()
    assert np.isnan(atr.iloc[13]) and not np.isnan(atr.iloc[14])


def test_stochastic_in_range(ohlcv: pd.DataFrame) -> None:
    st = ind.stochastic(ohlcv, 14, 3)
    assert st["k"].dropna().between(0, 100).all()
    assert st["d"].dropna().between(0, 100).all()


def test_crossover_and_crossunder_fire_once() -> None:
    a = series([1, 2, 3, 4, 3, 2, 1])
    b = series([2.5] * 7)
    over = ind.crossover(a, b)
    under = ind.crossunder(a, b)
    assert over.tolist() == [False, False, True, False, False, False, False]
    assert under.tolist() == [False, False, False, False, False, True, False]
    assert ind.crossover(a, 2.5).tolist() == over.tolist()  # scalar works too


def test_crossover_on_touch_counts_as_prev_leq() -> None:
    a = series([1, 2, 2, 3])
    b = series([2, 2, 2, 2])
    assert ind.crossover(a, b).tolist() == [False, False, False, True]


def test_highest_lowest_change_pct_change() -> None:
    s = series([1, 3, 2, 5, 4])
    assert ind.highest(s, 2).iloc[-1] == 5
    assert ind.lowest(s, 3).iloc[-1] == 2
    assert ind.change(s, 1).iloc[-1] == -1
    assert ind.pct_change(s, 1).iloc[-1] == pytest.approx(-20.0)
    assert ind.shift(s, 2).iloc[-1] == 2


def test_vwap_and_obv(ohlcv: pd.DataFrame) -> None:
    v = ind.vwap(ohlcv)
    assert v.iloc[0] == pytest.approx((ohlcv["high"].iloc[0] + ohlcv["low"].iloc[0] + ohlcv["close"].iloc[0]) / 3)
    o = ind.obv(ohlcv)
    assert o.iloc[0] == 0
    assert len(o) == len(ohlcv)


def test_period_validation() -> None:
    s = series([1, 2, 3])
    with pytest.raises(ValueError):
        ind.sma(s, 0)
    with pytest.raises(ValueError):
        ind.sma(s, "abc")
    with pytest.raises(ValueError):
        ind.sma(s, s)


def test_validate_ohlcv_sorts_and_dedupes() -> None:
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-01", "2024-01-02"], tz="UTC")
    df = pd.DataFrame({"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3], "close": [1, 2, 3],
                       "volume": [1, 2, 3], "extra": [0, 0, 0]}, index=idx)
    out = ind.validate_ohlcv(df)
    assert list(out.columns) == list(ind.OHLCV_COLUMNS)
    assert len(out) == 2
    assert out["close"].tolist() == [2.0, 3.0]  # last duplicate wins, sorted ascending


def test_validate_ohlcv_rejects_bad_frames() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        ind.validate_ohlcv(pd.DataFrame({"close": [1]}, index=pd.DatetimeIndex(["2024-01-01"])))
    with pytest.raises(ValueError, match="DatetimeIndex"):
        ind.validate_ohlcv(pd.DataFrame({c: [1] for c in ind.OHLCV_COLUMNS}))
