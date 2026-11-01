"""Command line entry point.

    python -m trading_bot serve               # web application (default), opens the browser
    python -m trading_bot run                 # headless scanner (notifications via log/webhook/email)
    python -m trading_bot backtest AAPL       # backtest every strategy on a symbol
    python -m trading_bot scan                # single scan, print signals, exit
    python -m trading_bot check               # validate config and strategies
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading

from . import __version__
from .app import build_engine, configure_logging
from .config import AppConfig, ConfigError


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trading_bot", description="Real-time trading signal bot")
    p.add_argument("--config", "-c", default=os.environ.get("TRADING_BOT_CONFIG", "config.json"),
                   help="path to config.json (default: $TRADING_BOT_CONFIG or ./config.json)")
    p.add_argument("--version", action="version", version=f"trading-bot {__version__}")
    sub = p.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="start the web application (default)")
    serve.add_argument("--host", help="bind address (default: config web.host, 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port (default: config web.port, 8080)")
    serve.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    run = sub.add_parser("run", help="run headless")
    run.add_argument("--once", action="store_true", help="scan once and exit")
    sub.add_parser("scan", help="scan once and print signals as JSON")
    bt = sub.add_parser("backtest", help="backtest strategies on historical bars")
    bt.add_argument("symbol")
    bt.add_argument("--strategy", help="only this strategy (default: all)")
    bt.add_argument("--interval", help="bar interval (default: config)")
    bt.add_argument("--bars", type=int, help="number of bars (default: config)")
    bt.add_argument("--cash", type=float, default=10_000.0)
    sub.add_parser("check", help="validate configuration and strategies")
    pw = sub.add_parser("set-password", help="set or reset the administrator password of the web UI "
                        "(stored hashed in the config file and applied when the server starts)")
    pw.add_argument("--username", help="administrator login name (default: keep the configured one)")
    pw.add_argument("--password", help="password (prompted securely when omitted; or TRADING_BOT_WEB_PASSWORD)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "serve"
    try:
        config = AppConfig.load(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    configure_logging(config.log_level, str(config.data_dir_path))

    if command == "serve":
        return _serve(config, args)
    if command == "check":
        return _check(config)
    if command in ("run", "scan"):
        return _run(config, once=(command == "scan" or args.once), as_json=(command == "scan"))
    if command == "backtest":
        return _backtest(config, args)
    if command == "set-password":
        return _set_password(config, args)
    return 1


def _serve(config: AppConfig, args: argparse.Namespace) -> int:
    from .web.server import serve

    engine = build_engine(config)
    try:
        return serve(engine, args.config, host=getattr(args, "host", None), port=getattr(args, "port", None),
                     open_browser=False if getattr(args, "no_browser", False) else None)
    finally:
        engine.close()


def _set_password(config: AppConfig, args: argparse.Namespace) -> int:
    import getpass
    import os

    from .web.auth import hash_password, validate_password_strength

    password = args.password or os.environ.get("TRADING_BOT_WEB_PASSWORD")
    if not password:
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Confirm password: "):
            print("Passwords do not match", file=sys.stderr)
            return 1
    problem = validate_password_strength(password)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    if args.username:
        config.web.username = args.username.strip()
    config.web.password_hash = hash_password(password)
    config.save(args.config)
    print(f"Password for administrator {config.web.username!r} saved to {args.config}; it is applied when the server starts")
    return 0


def _check(config: AppConfig) -> int:
    from .strategy import StrategyStore

    print(f"Provider: {config.data.provider} (interval {config.data.interval}, {config.data.bars} bars)")
    if config.requires_api_key() and not config.data.api_key:
        print("  WARNING: no API key configured (data.api_key or TRADING_BOT_API_KEY)")
    print(f"Watchlist: {', '.join(config.watchlist)}")
    per_min, per_day = config.data.budget()
    print(f"Budget: {per_min}/min, {per_day}/day")
    store = StrategyStore(config.strategies_path, plugin_dir=config.plugins_path)
    store.load()
    ok = True
    for d in store.definitions():
        problems = d.validate()
        state = "enabled" if d.enabled else "disabled"
        if problems:
            ok = False
            print(f"  [INVALID] {d.name} ({state}): {'; '.join(problems)}")
        else:
            print(f"  [ok] {d.name} ({state})")
    for p in store.plugins():
        print(f"  [plugin] {p.name}")
    return 0 if ok else 1


def _run(config: AppConfig, once: bool, as_json: bool) -> int:
    engine = build_engine(config)
    try:
        if once:
            signals = engine.scan_once()
            if as_json:
                print(json.dumps([s.to_dict() for s in signals], indent=2))
            return 0
        stop = threading.Event()

        def _handle(signum, frame):  # noqa: ARG001
            stop.set()

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
        engine.start()
        print("Engine running. Press Ctrl+C to stop.")
        while not stop.is_set():
            stop.wait(1.0)
        return 0
    finally:
        engine.close()


def _backtest(config: AppConfig, args: argparse.Namespace) -> int:
    from .backtest import run_backtest

    engine = build_engine(config)
    try:
        interval = args.interval or config.data.interval
        result = engine.data.get_bars(args.symbol, interval, args.bars or config.data.bars, force=True)
        strategies = engine.strategies.all_strategies()
        if args.strategy:
            strategies = [s for s in strategies if s.name == args.strategy]
            if not strategies:
                print(f"No strategy named {args.strategy!r}", file=sys.stderr)
                return 1
        rows = [run_backtest(s, result.bars, args.symbol, args.cash).summary() for s in strategies]
        if not rows:
            print("No strategies to test")
            return 1
        keys = list(rows[0].keys())
        widths = {k: max(len(k), *(len(str(r[k])) for r in rows)) for k in keys}
        print("  ".join(k.ljust(widths[k]) for k in keys))
        for r in rows:
            print("  ".join(str(r[k]).ljust(widths[k]) for k in keys))
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    sys.exit(main())
