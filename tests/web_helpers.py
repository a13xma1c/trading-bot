"""Shared helpers for browser-based tests: a live server on a free port and a Playwright browser."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from trading_bot.app import build_engine
from trading_bot.config import AppConfig
from trading_bot.data import MarketDataProvider
from trading_bot.engine import TradingEngine

DEFAULT_STRATEGIES: list[dict[str, Any]] = [
    {"name": "MA Crossover", "description": "Classic fast/slow moving-average crossover.",
     "buy_rule": "crossover(sma(fast), sma(slow))", "sell_rule": "crossunder(sma(fast), sma(slow))",
     "params": {"fast": 5, "slow": 20}, "enabled": True},
    {"name": "Always Buy", "description": "Fires on every bar; exercises notifications deterministically.",
     "buy_rule": "close > 0", "enabled": True},
]


def write_project(directory: Path, *, strategies: list[dict[str, Any]] | None = None, **overrides: Any) -> Path:
    """Write config.json + strategies.json for a demo-provider project into ``directory``."""
    raw: dict[str, Any] = {
        "watchlist": ["AAPL", "MSFT"], "scan_interval": 2, "data_dir": "bot_data", "strategies_file": "strategies.json",
        "data": {"provider": "demo", "interval": "5min", "bars": 200, "cache_ttl_seconds": 1},
        "market_hours": {"enabled": False}, "notifications": {"desktop": False, "sound": False},
        "trading": {"trading_enabled": True, "initial_cash": 50_000}, "web": {"open_browser": False},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key].update(value)
        else:
            raw[key] = value
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps(raw, indent=2))
    (directory / "strategies.json").write_text(json.dumps(strategies if strategies is not None else DEFAULT_STRATEGIES))
    return directory / "config.json"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class LiveServer:
    url: str
    engine: TradingEngine
    config_path: Path
    _server: Any

    def stop(self) -> None:
        self._server.should_exit = True
        self.engine.close()


def start_live_server(config_path: Path, provider: MarketDataProvider | None = None) -> LiveServer:
    """Run the web app in a background thread (uvicorn) and wait until it accepts connections."""
    import uvicorn

    from trading_bot.web.server import create_app

    engine = build_engine(AppConfig.load(config_path), provider=provider)
    app = create_app(engine, str(config_path))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, name=f"uvicorn-{port}", daemon=True).start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            engine.close()
            raise RuntimeError("web server did not start")
        time.sleep(0.02)
    return LiveServer(f"http://127.0.0.1:{port}/", engine, config_path, server)


def playwright_browser() -> Iterator[Any]:
    """Yield a headless Chromium, skipping the test when Playwright or its browser is unavailable."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - dev dependency missing
        pytest.skip("playwright is not installed")
    # Convenience for environments that keep the browsers in a shared location.
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and Path("/opt/pw-browsers").is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError as exc:  # pragma: no cover - browser not installed
            pytest.skip(f"Chromium is not available for Playwright (run `playwright install chromium`): {exc}")
        try:
            yield browser
        finally:
            browser.close()
