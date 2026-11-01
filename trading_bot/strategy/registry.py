"""Persistence and discovery of strategies.

Rule strategies are stored in a JSON file that the GUI edits.  Advanced users
can additionally drop Python modules into a ``strategies/`` directory; every
:class:`Strategy` subclass found there is loaded as a plugin.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import os
import threading
from collections.abc import Iterable
from pathlib import Path

from .base import RuleStrategy, Strategy, StrategyDefinition

logger = logging.getLogger(__name__)


def default_definitions() -> list[StrategyDefinition]:
    """Strategies shipped with the application (mirroring the v0 heuristics)."""
    return [
        StrategyDefinition(
            name="RSI Reversal",
            description="Buy when RSI leaves oversold territory, sell when it leaves overbought.",
            buy_rule="crossover(rsi(period), oversold)",
            sell_rule="crossunder(rsi(period), overbought)",
            params={"period": 14, "oversold": 30, "overbought": 70},
            cooldown_bars=5,
        ),
        StrategyDefinition(
            name="MA Crossover",
            description="Classic fast/slow moving-average crossover.",
            buy_rule="crossover(sma(fast), sma(slow))",
            sell_rule="crossunder(sma(fast), sma(slow))",
            params={"fast": 5, "slow": 20},
        ),
        StrategyDefinition(
            name="MACD Momentum",
            description="MACD line crossing its signal line, filtered by volume.",
            buy_rule="crossover(macd(), macd_signal()) and volume > sma(20, volume)",
            sell_rule="crossunder(macd(), macd_signal())",
            stop_loss_pct=2.0,
            take_profit_pct=4.0,
        ),
        StrategyDefinition(
            name="Bollinger Bounce",
            description="Mean reversion: buy a close below the lower band, sell at the middle band.",
            buy_rule="close < bb_lower(20, 2) and rsi(14) < 40",
            sell_rule="close > bb_middle(20, 2)",
            enabled=False,
        ),
    ]


class StrategyStore:
    """Thread-safe container for strategy definitions with JSON persistence."""

    def __init__(self, path: str | os.PathLike[str] | None = None, plugin_dir: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path else None
        self.plugin_dir = Path(plugin_dir) if plugin_dir else None
        self._lock = threading.RLock()
        self._definitions: list[StrategyDefinition] = []
        self._plugins: list[Strategy] = []
        self._listeners: list = []

    # ------------------------------------------------------------- loading
    def load(self) -> None:
        with self._lock:
            if self.path and self.path.exists():
                with self.path.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if not isinstance(raw, list):
                    raise ValueError(f"{self.path} must contain a JSON list of strategies")
                self._definitions = [StrategyDefinition.from_dict(item) for item in raw]
                logger.info("Loaded %d strategies from %s", len(self._definitions), self.path)
            else:
                self._definitions = default_definitions()
                if self.path:
                    self.save()
            self._plugins = list(load_python_strategies(self.plugin_dir)) if self.plugin_dir else []
        self._notify()

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump([d.to_dict() for d in self._definitions], fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)

    # ------------------------------------------------------------ editing
    def definitions(self) -> list[StrategyDefinition]:
        with self._lock:
            return list(self._definitions)

    def get(self, name: str) -> StrategyDefinition | None:
        with self._lock:
            for d in self._definitions:
                if d.name == name:
                    return d
        return None

    def upsert(self, definition: StrategyDefinition, previous_name: str | None = None) -> None:
        problems = definition.validate()
        if problems:
            raise ValueError("; ".join(problems))
        with self._lock:
            target = previous_name or definition.name
            if definition.name != target and any(d.name == definition.name for d in self._definitions):
                raise ValueError(f"A strategy named {definition.name!r} already exists")
            if any(p.name == definition.name for p in self._plugins):
                raise ValueError(f"{definition.name!r} is the name of a plugin strategy")
            for i, d in enumerate(self._definitions):
                if d.name == target:
                    self._definitions[i] = definition
                    break
            else:
                self._definitions.append(definition)
            self.save()
        self._notify()

    def remove(self, name: str) -> bool:
        with self._lock:
            before = len(self._definitions)
            self._definitions = [d for d in self._definitions if d.name != name]
            changed = len(self._definitions) != before
            if changed:
                self.save()
        if changed:
            self._notify()
        return changed

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            for d in self._definitions:
                if d.name == name:
                    d.enabled = enabled
                    self.save()
                    break
            else:
                for p in self._plugins:
                    if p.name == name:
                        p.enabled = enabled
        self._notify()

    # ----------------------------------------------------------- runtime
    def active_strategies(self) -> list[Strategy]:
        """Instantiate every enabled strategy (rules + plugins)."""
        out: list[Strategy] = []
        with self._lock:
            for d in self._definitions:
                if not d.enabled:
                    continue
                try:
                    out.append(RuleStrategy(d))
                except ValueError as exc:
                    logger.error("Skipping invalid strategy %r: %s", d.name, exc)
            out.extend(p for p in self._plugins if p.enabled)
        return out

    def all_strategies(self) -> list[Strategy]:
        with self._lock:
            out: list[Strategy] = []
            for d in self._definitions:
                try:
                    out.append(RuleStrategy(d))
                except ValueError:
                    continue
            out.extend(self._plugins)
        return out

    def plugins(self) -> list[Strategy]:
        with self._lock:
            return list(self._plugins)

    def add_listener(self, callback) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # pragma: no cover - listeners must not break the store
                logger.exception("Strategy store listener failed")


def load_python_strategies(directory: str | os.PathLike[str] | None) -> Iterable[Strategy]:
    """Import every ``*.py`` file in ``directory`` and yield Strategy instances."""
    if not directory:
        return
    directory = Path(directory)
    if not directory.is_dir():
        return
    for file in sorted(directory.glob("*.py")):
        if file.name.startswith("_"):
            continue
        module_name = f"trading_bot_plugins.{file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, file)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            logger.exception("Failed to import strategy plugin %s", file)
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Strategy) and obj not in (Strategy, RuleStrategy) and obj.__module__ == module_name:
                try:
                    instance = obj()
                except Exception:
                    logger.exception("Failed to instantiate plugin strategy %s", obj.__name__)
                    continue
                logger.info("Loaded plugin strategy %r from %s", instance.name, file.name)
                yield instance
