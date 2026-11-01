"""Example Python plugin strategy.

Any ``Strategy`` subclass defined in a ``.py`` file inside the ``strategies/``
directory is discovered at start-up and listed next to the rule-based
strategies in the GUI.  Python strategies can use the full power of pandas; the
only contract is ``signals(df) -> DataFrame`` with boolean ``buy``/``sell``
columns aligned to ``df``.
"""

import pandas as pd

from trading_bot import indicators as ind
from trading_bot.strategy import Strategy


class VolumeBreakout(Strategy):
    name = "Volume Breakout (plugin)"
    description = "Close breaks the 20-bar high on at least 2x average volume; exit below the 10-bar low."
    enabled = False  # enable from the GUI or set True
    stop_loss_pct = 3.0
    take_profit_pct = 6.0

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        breakout = df["close"] > ind.highest(df["high"], 20).shift(1)
        volume_ok = df["volume"] > 2 * ind.sma(df["volume"], 20).shift(1)
        buy = (breakout & volume_ok).fillna(False)
        sell = (df["close"] < ind.lowest(df["low"], 10).shift(1)).fillna(False)
        return pd.DataFrame({"buy": buy, "sell": sell})
