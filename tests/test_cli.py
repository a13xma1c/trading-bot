import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from trading_bot.__main__ import main


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """``main()`` configures the root logger; undo it so handlers never outlive the test's tmp dir."""
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    if hasattr(root, "_trading_bot_configured"):
        delattr(root, "_trading_bot_configured")


@pytest.fixture
def demo_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "watchlist": ["AAPL", "MSFT"], "scan_interval": 5,
        "data": {"provider": "demo", "interval": "5min", "bars": 200},
        "trading": {"trading_enabled": True},
    }))
    (tmp_path / "strategies.json").write_text(json.dumps([
        {"name": "always", "buy_rule": "close > 0", "sell_rule": "close < 0"},
    ]))
    return tmp_path


def test_check_command(demo_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "Provider: demo" in out and "[ok] always" in out


def test_check_reports_invalid_strategy(demo_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (demo_project / "strategies.json").write_text(json.dumps([{"name": "bad", "buy_rule": "close >"}]))
    assert main(["check"]) == 1
    assert "[INVALID] bad" in capsys.readouterr().out


def test_scan_command_outputs_json_and_persists(demo_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan"]) == 0
    signals = json.loads(capsys.readouterr().out)
    assert {s["symbol"] for s in signals} == {"AAPL", "MSFT"}
    assert (demo_project / "bot_data" / "history.sqlite3").exists()
    assert (demo_project / "bot_data" / "paper_broker.json").exists()
    # A second scan on the same bar reports nothing new (state persisted on disk).
    assert main(["scan"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_run_once(demo_project: Path) -> None:
    assert main(["run", "--once"]) == 0


def test_backtest_command(demo_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["backtest", "AAPL", "--bars", "300"]) == 0
    out = capsys.readouterr().out
    assert "always" in out and "total_return_pct" in out
    assert main(["backtest", "AAPL", "--strategy", "missing"]) == 1


def test_bad_config_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text("{bad")
    assert main(["check"]) == 2
    assert "Configuration error" in capsys.readouterr().err


def test_serve_command_runs_web_server(demo_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time
    import urllib.request

    from trading_bot.web import server as web_server

    started: dict[str, Any] = {}
    real_create_server = web_server.create_server

    def create_server(engine, config_path=None, host=None, port=None, log_level="info"):  # type: ignore[no-untyped-def]
        srv = real_create_server(engine, config_path, host, port, "warning")
        started["server"] = srv
        started["url"] = f"http://{host}:{port}/"
        return srv

    monkeypatch.setattr(web_server, "create_server", create_server)
    from tests.web_helpers import free_port

    port = free_port()
    failure: list[BaseException] = []

    def run() -> None:
        try:
            main(["serve", "--host", "127.0.0.1", "--port", str(port), "--no-browser"])
        except BaseException as exc:  # noqa: BLE001
            failure.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline and not (started.get("server") and getattr(started["server"], "started", False)):
        time.sleep(0.05)
    assert not failure, f"serve raised: {failure[0]!r}"
    srv = started["server"]
    assert getattr(srv, "started", False), "server did not start"
    with urllib.request.urlopen(f"{started['url']}api/health", timeout=5) as response:  # noqa: S310
        assert json.loads(response.read())["ok"] is True
    with urllib.request.urlopen(f"{started['url']}", timeout=5) as response:  # noqa: S310
        assert b"<title>Trading Bot</title>" in response.read()
    srv.should_exit = True
    thread.join(10)
    assert not thread.is_alive()
