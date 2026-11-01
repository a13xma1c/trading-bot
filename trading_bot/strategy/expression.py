"""A small, safe rule language for defining trading strategies.

Rules are written as boolean expressions over OHLCV data and technical
indicators, for example::

    rsi(14) < 30 and close > sma(200)
    crossover(ema(12), ema(26)) and volume > sma(20, volume) * 1.5

Expressions are parsed with :mod:`ast` and evaluated by walking the tree, so
only a whitelisted subset of Python syntax is available.  There is no access to
attributes, subscripts, imports, lambdas or arbitrary builtins; the evaluator
cannot execute user code.  Every sub-expression evaluates to a
:class:`pandas.Series` aligned to the bar index (or a scalar), which makes the
same rule usable for live signals (look at the last bar) and backtests (look at
the whole history).
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .. import indicators as ind

MAX_EXPRESSION_LENGTH = 2000
MAX_NODES = 500
MAX_EXPONENT = 64

SERIES_NAMES = ("open", "high", "low", "close", "volume")


class ExpressionError(ValueError):
    """Raised for syntax or semantic errors in a rule expression."""


# --------------------------------------------------------------------------
# Function registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    signature: str
    description: str
    func: Callable[..., Any]
    min_args: int
    max_args: int


class _Context:
    """Evaluation context: the data frame, parameters and a memo cache."""

    __slots__ = ("df", "params", "cache")

    def __init__(self, df: pd.DataFrame, params: dict[str, Any]):
        self.df = df
        self.params = params
        self.cache: dict[str, Any] = {}


def _source(ctx: _Context, source) -> pd.Series:
    if source is None:
        return ctx.df["close"]
    if isinstance(source, pd.Series):
        return source
    raise ExpressionError("source argument must be a price series such as close, high or sma(20)")


def _series_arg(value, name: str) -> pd.Series:
    if not isinstance(value, pd.Series):
        raise ExpressionError(f"{name} expects a series argument (e.g. close or sma(20))")
    return value


def _f_sma(ctx, period, source=None):
    return ind.sma(_source(ctx, source), period)


def _f_ema(ctx, period, source=None):
    return ind.ema(_source(ctx, source), period)


def _f_wma(ctx, period, source=None):
    return ind.wma(_source(ctx, source), period)


def _f_rsi(ctx, period=14, source=None):
    return ind.rsi(_source(ctx, source), period)


def _macd(ctx, fast, slow, signal):
    key = f"__macd({fast},{slow},{signal})"
    if key not in ctx.cache:
        ctx.cache[key] = ind.macd(ctx.df["close"], fast, slow, signal)
    return ctx.cache[key]


def _f_macd(ctx, fast=12, slow=26, signal=9):
    return _macd(ctx, fast, slow, signal)["macd"]


def _f_macd_signal(ctx, fast=12, slow=26, signal=9):
    return _macd(ctx, fast, slow, signal)["signal"]


def _f_macd_hist(ctx, fast=12, slow=26, signal=9):
    return _macd(ctx, fast, slow, signal)["hist"]


def _bb(ctx, period, std):
    key = f"__bb({period},{std})"
    if key not in ctx.cache:
        ctx.cache[key] = ind.bollinger(ctx.df["close"], period, std)
    return ctx.cache[key]


def _f_bb_upper(ctx, period=20, std=2.0):
    return _bb(ctx, period, std)["upper"]


def _f_bb_middle(ctx, period=20, std=2.0):
    return _bb(ctx, period, std)["middle"]


def _f_bb_lower(ctx, period=20, std=2.0):
    return _bb(ctx, period, std)["lower"]


def _f_atr(ctx, period=14):
    return ind.atr(ctx.df, period)


def _stoch(ctx, k, d):
    key = f"__stoch({k},{d})"
    if key not in ctx.cache:
        ctx.cache[key] = ind.stochastic(ctx.df, k, d)
    return ctx.cache[key]


def _f_stoch_k(ctx, k=14, d=3):
    return _stoch(ctx, k, d)["k"]


def _f_stoch_d(ctx, k=14, d=3):
    return _stoch(ctx, k, d)["d"]


def _f_vwap(ctx):
    return ind.vwap(ctx.df)


def _f_obv(ctx):
    return ind.obv(ctx.df)


def _f_highest(ctx, source, period):
    return ind.highest(_series_arg(source, "highest"), period)


def _f_lowest(ctx, source, period):
    return ind.lowest(_series_arg(source, "lowest"), period)


def _f_prev(ctx, source, n=1):
    return ind.shift(_series_arg(source, "prev"), n)


def _f_change(ctx, source, n=1):
    return ind.change(_series_arg(source, "change"), n)


def _f_pct_change(ctx, source, n=1):
    return ind.pct_change(_series_arg(source, "pct_change"), n)


def _f_crossover(ctx, a, b):
    return ind.crossover(_series_arg(a, "crossover"), b)


def _f_crossunder(ctx, a, b):
    return ind.crossunder(_series_arg(a, "crossunder"), b)


def _f_abs(ctx, x):
    return x.abs() if isinstance(x, pd.Series) else abs(x)


def _f_min(ctx, *args):
    return _reduce(ctx, args, np.minimum)


def _f_max(ctx, *args):
    return _reduce(ctx, args, np.maximum)


def _reduce(ctx, args, op):
    if not args:
        raise ExpressionError("min/max need at least one argument")
    out = args[0]
    for nxt in args[1:]:
        if isinstance(out, pd.Series) or isinstance(nxt, pd.Series):
            out = op(_align(out, ctx), _align(nxt, ctx))
        else:
            out = op(out, nxt)
    return out


def _f_bars_since(ctx, cond):
    """Number of bars since ``cond`` was last true (NaN if never)."""
    cond = _to_bool(_series_arg(cond, "bars_since"))
    idx = np.arange(len(cond), dtype=float)
    last_true = pd.Series(np.where(cond.to_numpy(), idx, np.nan), index=cond.index).ffill()
    return pd.Series(idx, index=cond.index) - last_true


def _align(value, ctx: _Context) -> pd.Series:
    if isinstance(value, pd.Series):
        return value
    return pd.Series(float(value), index=ctx.df.index)


def _to_bool(value) -> pd.Series:
    if value.dtype == bool:
        return value
    return value.fillna(False).astype(bool)


FUNCTIONS: dict[str, FunctionSpec] = {}


def _register(name, signature, description, func, min_args, max_args):
    FUNCTIONS[name] = FunctionSpec(name, signature, description, func, min_args, max_args)


_register("sma", "sma(period, source=close)", "Simple moving average", _f_sma, 1, 2)
_register("ema", "ema(period, source=close)", "Exponential moving average", _f_ema, 1, 2)
_register("wma", "wma(period, source=close)", "Weighted moving average", _f_wma, 1, 2)
_register("rsi", "rsi(period=14, source=close)", "Relative strength index (0-100)", _f_rsi, 0, 2)
_register("macd", "macd(fast=12, slow=26, signal=9)", "MACD line", _f_macd, 0, 3)
_register("macd_signal", "macd_signal(fast=12, slow=26, signal=9)", "MACD signal line", _f_macd_signal, 0, 3)
_register("macd_hist", "macd_hist(fast=12, slow=26, signal=9)", "MACD histogram", _f_macd_hist, 0, 3)
_register("bb_upper", "bb_upper(period=20, std=2)", "Upper Bollinger band", _f_bb_upper, 0, 2)
_register("bb_middle", "bb_middle(period=20, std=2)", "Middle Bollinger band", _f_bb_middle, 0, 2)
_register("bb_lower", "bb_lower(period=20, std=2)", "Lower Bollinger band", _f_bb_lower, 0, 2)
_register("atr", "atr(period=14)", "Average true range", _f_atr, 0, 1)
_register("stoch_k", "stoch_k(k=14, d=3)", "Stochastic %K", _f_stoch_k, 0, 2)
_register("stoch_d", "stoch_d(k=14, d=3)", "Stochastic %D", _f_stoch_d, 0, 2)
_register("vwap", "vwap()", "Volume weighted average price", _f_vwap, 0, 0)
_register("obv", "obv()", "On-balance volume", _f_obv, 0, 0)
_register("highest", "highest(source, period)", "Highest value over period", _f_highest, 2, 2)
_register("lowest", "lowest(source, period)", "Lowest value over period", _f_lowest, 2, 2)
_register("prev", "prev(source, n=1)", "Value n bars ago", _f_prev, 1, 2)
_register("change", "change(source, n=1)", "Absolute change over n bars", _f_change, 1, 2)
_register("pct_change", "pct_change(source, n=1)", "Percent change over n bars", _f_pct_change, 1, 2)
_register("crossover", "crossover(a, b)", "True when a crosses above b", _f_crossover, 2, 2)
_register("crossunder", "crossunder(a, b)", "True when a crosses below b", _f_crossunder, 2, 2)
_register("bars_since", "bars_since(condition)", "Bars since condition was last true", _f_bars_since, 1, 1)
_register("abs", "abs(x)", "Absolute value", _f_abs, 1, 1)
_register("min", "min(a, b, ...)", "Element-wise minimum", _f_min, 1, 10)
_register("max", "max(a, b, ...)", "Element-wise maximum", _f_max, 1, 10)


def function_help() -> list[tuple[str, str]]:
    """Return ``(signature, description)`` pairs for GUI help panels."""
    return [(spec.signature, spec.description) for spec in FUNCTIONS.values()]


# --------------------------------------------------------------------------
# Parsing / validation
# --------------------------------------------------------------------------

_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}

_CMP_OPS = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}


class Expression:
    """A parsed and validated rule expression."""

    def __init__(self, text: str, param_names: Iterable[str] = frozenset()):
        self.text = text.strip()
        self.param_names = frozenset(param_names)
        if not self.text:
            raise ExpressionError("Expression is empty")
        if len(self.text) > MAX_EXPRESSION_LENGTH:
            raise ExpressionError(f"Expression is too long (max {MAX_EXPRESSION_LENGTH} characters)")
        try:
            self._tree = ast.parse(self.text, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"Syntax error at column {exc.offset}: {exc.msg}") from exc
        self._validate()

    def __repr__(self) -> str:
        return f"Expression({self.text!r})"

    # ---------------------------------------------------------- validation
    def _validate(self) -> None:
        self._node_count = 0
        self._visit(self._tree)

    def _visit(self, node: ast.AST) -> None:
        self._node_count += 1
        if self._node_count > MAX_NODES:
            raise ExpressionError("Expression is too complex")
        self._check_node(node)
        if isinstance(node, ast.Call):
            # The callee name was validated in _check_node; only descend into arguments.
            for child in node.args:
                self._visit(child)
            for kw in node.keywords:
                self._visit(kw.value)
            return
        for sub in ast.iter_child_nodes(node):
            self._visit(sub)

    def _check_node(self, node: ast.AST) -> None:
        if isinstance(node, (ast.Expression, ast.Load)):
            return
        if isinstance(node, ast.BoolOp):
            if not isinstance(node.op, (ast.And, ast.Or)):
                raise ExpressionError("Unsupported boolean operator")
            return
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _BIN_OPS:
                raise ExpressionError(f"Unsupported operator: {_describe(node)}")
            if isinstance(node.op, ast.Pow):
                if not (isinstance(node.right, ast.Constant) and isinstance(node.right.value, (int, float))
                        and abs(node.right.value) <= MAX_EXPONENT):
                    raise ExpressionError(f"Exponent must be a constant between -{MAX_EXPONENT} and {MAX_EXPONENT}")
            return
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
                raise ExpressionError(f"Unsupported unary operator: {_describe(node)}")
            return
        if isinstance(node, ast.Compare):
            for op in node.ops:
                if type(op) not in _CMP_OPS:
                    raise ExpressionError(f"Unsupported comparison in: {_describe(node)}")
            return
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExpressionError(f"Only named functions may be called: {_describe(node)}")
            spec = FUNCTIONS.get(node.func.id)
            if spec is None:
                raise ExpressionError(f"Unknown function: {node.func.id}()")
            n_args = len(node.args) + len(node.keywords)
            if n_args < spec.min_args or n_args > spec.max_args:
                raise ExpressionError(
                    f"{node.func.id}() takes {spec.min_args}-{spec.max_args} arguments, got {n_args}. "
                    f"Usage: {spec.signature}"
                )
            for kw in node.keywords:
                if kw.arg is None:
                    raise ExpressionError("**kwargs are not supported")
            return
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or isinstance(node.value, (int, float)):
                return
            raise ExpressionError(f"Unsupported constant: {node.value!r}")
        if isinstance(node, ast.Name):
            if node.id in SERIES_NAMES or node.id in self.param_names:
                return
            if node.id in FUNCTIONS:
                raise ExpressionError(f"{node.id} is a function; call it like {FUNCTIONS[node.id].signature}")
            raise ExpressionError(
                f"Unknown name: {node.id!r}. Use open/high/low/close/volume, a strategy parameter, or a function"
            )
        if isinstance(node, (ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.keyword)) or isinstance(
            node, tuple(_BIN_OPS) + tuple(_CMP_OPS)
        ):
            return
        raise ExpressionError(f"Unsupported syntax: {_describe(node)}")

    # ---------------------------------------------------------- evaluation
    def evaluate(self, df: pd.DataFrame, params: dict[str, Any] | None = None) -> pd.Series:
        """Evaluate over the whole frame, returning a boolean series."""
        params = dict(params or {})
        missing = self.param_names - set(params)
        if missing:
            raise ExpressionError(f"Missing parameter values: {', '.join(sorted(missing))}")
        ctx = _Context(df, params)
        result = self._eval(self._tree.body, ctx)
        if isinstance(result, pd.Series):
            if result.dtype != bool:
                result = result.fillna(False).astype(bool)
            return result
        return pd.Series(bool(result), index=df.index)

    def evaluate_raw(self, df: pd.DataFrame, params: dict[str, Any] | None = None):
        """Evaluate without coercing to boolean (used to plot indicator values)."""
        params = dict(params or {})
        missing = self.param_names - set(params)
        if missing:
            raise ExpressionError(f"Missing parameter values: {', '.join(sorted(missing))}")
        return self._eval(self._tree.body, _Context(df, params))

    def evaluate_last(self, df: pd.DataFrame, params: dict[str, Any] | None = None) -> bool:
        """Evaluate and return the value on the most recent bar."""
        if df.empty:
            return False
        return bool(self.evaluate(df, params).iloc[-1])

    def _eval(self, node: ast.AST, ctx: _Context):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in SERIES_NAMES:
                return ctx.df[node.id]
            return ctx.params[node.id]
        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand, ctx)
            if isinstance(node.op, ast.Not):
                if isinstance(operand, pd.Series):
                    return ~_to_bool(operand)
                return not operand
            if isinstance(node.op, ast.USub):
                return -operand
            return +operand
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left, ctx)
            right = self._eval(node.right, ctx)
            with np.errstate(divide="ignore", invalid="ignore"):
                return _BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.BoolOp):
            values = [self._eval(v, ctx) for v in node.values]
            if any(isinstance(v, pd.Series) for v in values):
                series = [_to_bool(_align(v, ctx)) if not isinstance(v, pd.Series) else _to_bool(v) for v in values]
                out = series[0]
                for s in series[1:]:
                    out = (out & s) if isinstance(node.op, ast.And) else (out | s)
                return out
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, ctx)
            result = None
            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator, ctx)
                part = _CMP_OPS[type(op)](left, right)
                if isinstance(part, pd.Series):
                    part = _to_bool(part)
                if result is None:
                    result = part
                elif isinstance(result, pd.Series) or isinstance(part, pd.Series):
                    result = _to_bool(_align(result, ctx)) & _to_bool(_align(part, ctx))
                else:
                    result = result and part
                left = right
            return result
        if isinstance(node, ast.Call):
            key = ast.unparse(node)
            if key in ctx.cache:
                return ctx.cache[key]
            assert isinstance(node.func, ast.Name)  # guaranteed by validation
            spec = FUNCTIONS[node.func.id]
            args = [self._eval(a, ctx) for a in node.args]
            kwargs = {kw.arg: self._eval(kw.value, ctx) for kw in node.keywords if kw.arg is not None}
            try:
                value = spec.func(ctx, *args, **kwargs)
            except TypeError as exc:
                raise ExpressionError(f"Bad arguments for {spec.signature}: {exc}") from exc
            except ValueError as exc:
                raise ExpressionError(f"{spec.name}(): {exc}") from exc
            ctx.cache[key] = value
            return value
        raise ExpressionError(f"Unsupported syntax: {_describe(node)}")

    def indicator_calls(self) -> list[str]:
        """Return the distinct indicator calls used (for chart overlays)."""
        calls = []
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Call):
                text = ast.unparse(node)
                if text not in calls:
                    calls.append(text)
        return calls


def _describe(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive
        return type(node).__name__


def validate_expression(text: str, param_names: Iterable[str] = frozenset()) -> str | None:
    """Return an error message for ``text`` or ``None`` if it is valid."""
    try:
        Expression(text, param_names)
    except ExpressionError as exc:
        return str(exc)
    return None
