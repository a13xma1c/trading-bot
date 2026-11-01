"""Chart payloads: bars, indicator overlays and markers in a JSON-friendly form."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ..strategy import Expression, ExpressionError, RuleStrategy, Strategy

MAX_INDICATOR_PANES = 3


def bars_payload(df: pd.DataFrame) -> list[list[float]]:
    """``[[epoch_ms, open, high, low, close, volume], ...]`` oldest first."""
    if df is None or df.empty:
        return []
    # Resolution independent (pandas may store the index in ns, us, ms or s).
    index = pd.DatetimeIndex(df.index)
    times = [int(ts.timestamp() * 1000) for ts in index]
    cols = [df[c].to_numpy(dtype=float).tolist() for c in ("open", "high", "low", "close", "volume")]
    return [[t, o, h, lo, c, v] for t, o, h, lo, c, v in zip(times, *cols)]


def _clean(values: np.ndarray) -> list[float | None]:
    return [None if (v is None or math.isnan(v)) else float(v) for v in values.tolist()]


def overlay_payload(strategy: Strategy, df: pd.DataFrame | None) -> dict[str, Any]:
    """Indicator series used by ``strategy`` plus where it fires on ``df``.

    Series on the price scale (moving averages, bands, VWAP ...) go to ``price``,
    volume-scale series to ``volume`` and everything else is grouped into up to
    three ``panes`` by order of magnitude so that e.g. RSI (0-100) and MACD
    (around 0) do not flatten each other.
    """
    out: dict[str, Any] = {"strategy": strategy.name, "price": [], "volume": [], "panes": [], "buy": [], "sell": []}
    if df is None or df.empty:
        return out
    lo, hi = float(df["low"].min()), float(df["high"].max())
    span = max(hi - lo, 0.05 * hi)
    band_lo, band_hi = lo - span, hi + span
    volume_hi = float(df["volume"].max())
    groups: dict[int, list[dict[str, Any]]] = {}

    series: dict[str, pd.Series] = {}
    if isinstance(strategy, RuleStrategy):
        for text in strategy.overlays():
            try:
                value = Expression(text, set(strategy.params)).evaluate_raw(df, strategy.params)
            except ExpressionError:
                continue
            if isinstance(value, pd.Series) and value.dtype != bool:
                series[text] = value

    for name, s in series.items():
        values = s.reindex(df.index).to_numpy(dtype=float)
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            continue
        entry = {"name": name, "values": _clean(values)}
        inside = float(np.mean((finite >= band_lo) & (finite <= band_hi)))
        magnitude = float(np.nanpercentile(np.abs(finite), 95))
        if inside >= 0.8:
            out["price"].append(entry)
        elif volume_hi > 0 and 0.05 * volume_hi <= magnitude <= 5 * volume_hi and np.all(finite >= 0):
            out["volume"].append(entry)
        else:
            groups.setdefault(int(round(math.log10(magnitude + 1e-12))), []).append(entry)

    buckets = sorted(groups)
    panes: list[list[dict[str, Any]]] = [[] for _ in range(min(len(buckets), MAX_INDICATOR_PANES))]
    for i, bucket in enumerate(buckets):
        panes[min(i, len(panes) - 1)].extend(groups[bucket])
    out["panes"] = panes

    frame = strategy.signals(df)
    out["buy"] = [int(i) for i in np.flatnonzero(frame["buy"].to_numpy(dtype=bool))]
    out["sell"] = [int(i) for i in np.flatnonzero(frame["sell"].to_numpy(dtype=bool))]
    return out


def marker_indices(df: pd.DataFrame | None, times: list[Any]) -> list[int]:
    """Map timestamps to bar indices (nearest earlier bar), dropping those outside ``df``."""
    if df is None or df.empty:
        return []
    index = df.index
    out: list[int] = []
    for when in times:
        ts = pd.Timestamp(when)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        pos = index.get_indexer(pd.DatetimeIndex([ts]))[0]
        if pos < 0:
            pos = int(index.searchsorted(ts)) - 1
            if pos < 0 or pos >= len(index):
                continue
        out.append(int(pos))
    return out
