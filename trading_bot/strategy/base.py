"""Strategy model: definitions, signals and the rule-based strategy."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from .expression import Expression, ExpressionError

ACTION_BUY = "BUY"
ACTION_SELL = "SELL"


@dataclass
class Signal:
    """A trade recommendation produced by a strategy."""

    symbol: str
    action: str
    strategy: str
    price: float
    bar_time: datetime
    reason: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Identity used to suppress duplicate notifications for the same bar."""
        return (self.strategy, self.symbol, self.action, self.bar_time.isoformat())

    def headline(self) -> str:
        return f"{self.action} {self.symbol} @ {self.price:.2f}"

    def describe(self) -> str:
        when = self.bar_time.astimezone().strftime("%Y-%m-%d %H:%M")
        text = f"{self.headline()} - {self.strategy} ({when})"
        if self.reason:
            text += f"\n{self.reason}"
        return text

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bar_time"] = self.bar_time.isoformat()
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class StrategyDefinition:
    """Serializable description of a rule-based strategy."""

    name: str
    buy_rule: str
    sell_rule: str = ""
    description: str = ""
    symbols: list[str] = field(default_factory=list)  # empty => whole watchlist
    params: dict[str, float] = field(default_factory=dict)
    enabled: bool = True
    stop_loss_pct: float = 0.0   # 0 disables
    take_profit_pct: float = 0.0  # 0 disables
    cooldown_bars: int = 0       # min bars between two signals of the same kind

    def __post_init__(self) -> None:
        self.name = str(self.name).strip()
        self.symbols = [str(s).strip().upper() for s in (self.symbols or []) if str(s).strip()]
        self.params = {str(k): float(v) for k, v in (self.params or {}).items()}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StrategyDefinition:
        raw = dict(raw)
        if "name" not in raw or "buy_rule" not in raw:
            raise ValueError("strategy definition needs at least 'name' and 'buy_rule'")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def applies_to(self, symbol: str) -> bool:
        return not self.symbols or symbol.upper() in self.symbols

    def validate(self) -> list[str]:
        """Return a list of human readable problems (empty if valid)."""
        problems: list[str] = []
        if not self.name.strip():
            problems.append("Strategy name is required")
        for key in self.params:
            if not key.isidentifier() or key in ("open", "high", "low", "close", "volume"):
                problems.append(f"Invalid parameter name: {key!r}")
        try:
            Expression(self.buy_rule, self.params.keys())
        except ExpressionError as exc:
            problems.append(f"Buy rule: {exc}")
        if self.sell_rule.strip():
            try:
                Expression(self.sell_rule, self.params.keys())
            except ExpressionError as exc:
                problems.append(f"Sell rule: {exc}")
        if self.stop_loss_pct < 0 or self.take_profit_pct < 0:
            problems.append("Stop loss / take profit must be >= 0")
        if self.cooldown_bars < 0:
            problems.append("Cooldown bars must be >= 0")
        return problems


class Strategy(ABC):
    """Base class for all strategies (rule-based or written in Python)."""

    name: str = "unnamed"
    description: str = ""
    symbols: list[str] = []
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    cooldown_bars: int = 0
    enabled: bool = True

    @abstractmethod
    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a frame with boolean ``buy`` and ``sell`` columns aligned to ``df``."""

    def applies_to(self, symbol: str) -> bool:
        return not self.symbols or symbol.upper() in [s.upper() for s in self.symbols]

    def overlays(self) -> list[str]:
        """Indicator expressions worth drawing on the chart (optional)."""
        return []

    def evaluate(self, symbol: str, df: pd.DataFrame) -> list[Signal]:
        """Evaluate the most recent bar of ``df`` and return any signals."""
        if df.empty:
            return []
        frame = self.signals(df)
        frame = _apply_cooldown(frame, self.cooldown_bars)
        last = frame.iloc[-1]
        bar_time = df.index[-1].to_pydatetime()
        price = float(df["close"].iloc[-1])
        out: list[Signal] = []
        if bool(last["buy"]):
            out.append(Signal(symbol, ACTION_BUY, self.name, price, bar_time, self.reason(ACTION_BUY)))
        if bool(last["sell"]):
            out.append(Signal(symbol, ACTION_SELL, self.name, price, bar_time, self.reason(ACTION_SELL)))
        return out

    def reason(self, action: str) -> str:
        return self.description


class RuleStrategy(Strategy):
    """Strategy driven by buy/sell expressions (what the GUI editor creates)."""

    def __init__(self, definition: StrategyDefinition):
        problems = definition.validate()
        if problems:
            raise ValueError("; ".join(problems))
        self.definition = definition
        self.name = definition.name
        self.description = definition.description
        self.symbols = list(definition.symbols)
        self.stop_loss_pct = definition.stop_loss_pct
        self.take_profit_pct = definition.take_profit_pct
        self.cooldown_bars = definition.cooldown_bars
        self.enabled = definition.enabled
        self.params = dict(definition.params)
        self.buy_expr = Expression(definition.buy_rule, self.params.keys())
        self.sell_expr = Expression(definition.sell_rule, self.params.keys()) if definition.sell_rule.strip() else None

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        buy = self.buy_expr.evaluate(df, self.params)
        if self.sell_expr is not None:
            sell = self.sell_expr.evaluate(df, self.params)
        else:
            sell = pd.Series(False, index=df.index)
        return pd.DataFrame({"buy": buy, "sell": sell})

    def overlays(self) -> list[str]:
        calls = self.buy_expr.indicator_calls()
        if self.sell_expr is not None:
            calls += [c for c in self.sell_expr.indicator_calls() if c not in calls]
        return calls

    def reason(self, action: str) -> str:
        rule = self.definition.buy_rule if action == ACTION_BUY else self.definition.sell_rule
        return f"{action} rule matched: {rule}"


def _apply_cooldown(frame: pd.DataFrame, cooldown: int) -> pd.DataFrame:
    """Suppress repeated signals of the same type within ``cooldown`` bars."""
    if cooldown <= 0:
        return frame
    out = frame.copy()
    for col in ("buy", "sell"):
        values = out[col].to_numpy().copy()
        last = -10**9
        for i, flag in enumerate(values):
            if flag:
                if i - last <= cooldown:
                    values[i] = False
                else:
                    last = i
        out[col] = values
    return out
