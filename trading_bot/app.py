"""Composition root: builds a fully wired engine from configuration."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

from .broker import PaperBroker
from .config import AppConfig
from .data import MarketDataService, RateLimiter, create_provider
from .engine import TradingEngine
from .notify import Notifier, build_notifier
from .storage import Storage
from .strategy import StrategyStore


class _StderrHandler(logging.StreamHandler):
    """Console handler that always writes to the *current* ``sys.stderr``.

    A plain StreamHandler captures the stream object at creation time, which
    breaks when stderr is later replaced or closed (test runners, GUI launchers).
    """

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:
        pass  # the stream is always resolved dynamically


def configure_logging(level: str = "INFO", log_dir: str | None = None) -> None:
    root = logging.getLogger()
    if getattr(root, "_trading_bot_configured", False):
        root.setLevel(level.upper())
        return
    root.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    stream = _StderrHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(Path(log_dir) / "trading_bot.log", maxBytes=2_000_000,
                                                  backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    root._trading_bot_configured = True  # type: ignore[attr-defined]


def build_engine(config: AppConfig, notifier: Notifier | None = None, provider=None) -> TradingEngine:
    data_dir = config.data_dir_path
    data_dir.mkdir(parents=True, exist_ok=True)

    provider = provider or create_provider(config.data.provider, config.data.api_key, config.csv_directory_path)
    per_minute, per_day = config.data.budget()
    limiter = RateLimiter(per_minute, per_day, state_file=data_dir / f"ratelimit_{provider.name}.json")
    service = MarketDataService(provider, limiter, cache_ttl=config.data.cache_ttl_seconds)

    store = StrategyStore(config.strategies_path, plugin_dir=config.plugins_path)
    store.load()

    broker = PaperBroker(config.trading.initial_cash, state_file=data_dir / "paper_broker.json")
    storage = Storage(data_dir / "history.sqlite3")
    notifier = notifier or build_notifier(config.notifications)
    return TradingEngine(config, service, store, notifier, broker, storage)
