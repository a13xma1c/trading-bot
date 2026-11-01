"""REST / WebSocket API tests (FastAPI TestClient, no browser)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_bot.app import build_engine
from trading_bot.config import AppConfig
from trading_bot.data import TwelveDataProvider
from trading_bot.engine import TradingEngine
from trading_bot.web.server import create_app

from .conftest import TwelveDataSession
from .web_helpers import TEST_PASSWORD, TEST_USER, write_project


def make_config(tmp_path: Path, **overrides: Any) -> Path:
    strategies: list[dict[str, Any]] = [
        {"name": "MA Crossover", "buy_rule": "crossover(sma(fast), sma(slow))", "sell_rule": "crossunder(sma(fast), sma(slow))",
         "params": {"fast": 5, "slow": 20}, "description": "classic"},
        {"name": "Always Buy", "buy_rule": "close > 0"},
    ]
    overrides.setdefault("scan_interval", 5)
    return write_project(tmp_path, strategies=strategies, **overrides)


def sign_in(client: TestClient, username: str = TEST_USER, password: str = TEST_PASSWORD) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text


@pytest.fixture
def web(tmp_path: Path) -> Iterator[tuple[TestClient, TradingEngine, Path]]:
    config_path = make_config(tmp_path)
    engine = build_engine(AppConfig.load(config_path))
    with TestClient(create_app(engine, str(config_path))) as client:
        sign_in(client)
        yield client, engine, config_path
    engine.close()


# ------------------------------------------------------------------ basics


def test_index_and_static_and_health(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, _, _ = web
    page = client.get("/")
    assert page.status_code == 200 and "<title>Trading Bot</title>" in page.text
    assert 'id="logoutButton"' in page.text
    assert 'id="startButton"' in page.text and 'id="chartCanvas"' in page.text
    assert client.get("/static/css/material.css").status_code == 200
    assert "--md-sys-color-primary" in client.get("/static/css/material.css").text
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/openapi.json").status_code == 200


def test_status_and_engine_lifecycle(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, _ = web
    status = client.get("/api/status").json()
    assert status["watchlist"] == ["AAPL", "MSFT"] and status["running"] is False
    assert status["provider"] == "demo" and status["market_open"] is True
    assert "per minute" in status["budget"]["text"]
    assert client.post("/api/engine/start").json()["running"] is True
    assert engine.running
    assert client.post("/api/engine/stop").json()["running"] is False
    assert not engine.running


def test_app_works_without_api_key_and_key_can_be_added_live(tmp_path: Path) -> None:
    """The shipped config (alphavantage, no key) must start; the key/provider is set from the UI."""
    config_path = make_config(tmp_path, data={"provider": "alphavantage", "api_key": ""})
    engine = build_engine(AppConfig.load(config_path))  # no crash without a key
    try:
        with TestClient(create_app(engine, str(config_path))) as client:
            sign_in(client)
            status = client.get("/api/status").json()
            assert status["requires_api_key"] is True and status["api_key_set"] is False
            response = client.post("/api/engine/start")
            assert response.status_code == 409 and "API key" in response.json()["detail"]
            bars = client.get("/api/bars/AAPL")
            assert bars.status_code == 503 and "requires an API key" in bars.json()["detail"]
            assert "requires an API key" in client.get("/api/watchlist").json()[0]["error"]

            # One click: switch to demo data, applied immediately.
            switched = client.post("/api/settings/provider", json={"provider": "demo"})
            assert switched.status_code == 200 and switched.json()["status"]["requires_api_key"] is False
            assert engine.wait_for_workers(10.0)
            assert len(client.get("/api/bars/AAPL").json()["bars"]) == 200
            assert client.get("/api/watchlist").json()[0]["error"] is None
            assert AppConfig.load(config_path).data.provider == "demo"

            # Entering a key for a real provider in Settings is applied without restart.
            saved = client.put("/api/settings", json={"data": {"provider": "twelvedata", "api_key": "abc123"}})
            assert saved.status_code == 200 and saved.json()["data_reconfigured"] is True
            assert saved.json()["status"]["provider"] == "twelvedata" and saved.json()["status"]["api_key_set"] is True
            assert engine.data.provider.name == "twelvedata"
            assert engine.config.data.api_key == "abc123"
            assert client.post("/api/settings/provider", json={"provider": "bloomberg"}).status_code == 422
    finally:
        engine.close()


def test_api_token_for_scripted_access(tmp_path: Path) -> None:
    """Scripts can use the bearer token instead of a browser session."""
    config_path = make_config(tmp_path, web={"token": "s3cret"})
    engine = build_engine(AppConfig.load(config_path))
    try:
        with TestClient(create_app(engine, str(config_path))) as client:
            assert client.get("/api/status").status_code == 401
            assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
            assert client.get("/api/status", headers={"Authorization": "Bearer s3cret"}).status_code == 200
            assert client.get("/api/status?token=s3cret").status_code == 200
            assert client.get("/api/auth/status", headers={"Authorization": "Bearer s3cret"}).json()["username"] == "api-token"
            with client.websocket_connect("/ws?token=s3cret") as ws:
                assert ws.receive_json()["type"] == "hello"
    finally:
        engine.close()


# -------------------------------------------------------------------- auth


def test_login_required_and_session_flow(tmp_path: Path) -> None:
    config_path = make_config(tmp_path)
    engine = build_engine(AppConfig.load(config_path))
    try:
        with TestClient(create_app(engine, str(config_path))) as client:
            # Anonymous: the app redirects to the login page and the API refuses.
            assert client.get("/", follow_redirects=False).status_code == 302
            assert client.get("/", follow_redirects=False).headers["location"] == "/login"
            login_page = client.get("/login")
            assert login_page.status_code == 200 and 'id="loginForm"' in login_page.text
            assert client.get("/api/status").status_code == 401
            assert client.post("/api/engine/start").status_code == 401
            status = client.get("/api/auth/status").json()
            assert status["authenticated"] is False and status["username"] is None
            assert status["setup_required"] is False and status["configured_username"] == TEST_USER
            with pytest.raises(Exception):  # noqa: B017 - WebSocket closed with 4401
                with client.websocket_connect("/ws"):
                    pass

            # Wrong credentials.
            bad = client.post("/api/auth/login", json={"username": TEST_USER, "password": "nope"})
            assert bad.status_code == 401 and "Invalid" in bad.json()["detail"]
            assert client.post("/api/auth/login", json={"username": "someone", "password": TEST_PASSWORD}).status_code == 401
            assert client.post("/api/auth/setup", json={"username": "x", "password": "whatever123"}).status_code == 409

            # Login sets an HttpOnly session cookie; everything works afterwards.
            ok = client.post("/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD})
            assert ok.status_code == 200 and ok.json() == {"authenticated": True, "username": TEST_USER}
            cookie = ok.headers["set-cookie"]
            assert "tb_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie.lower().replace("samesite=lax", "SameSite=lax")
            assert client.get("/api/status").status_code == 200
            assert client.get("/api/auth/status").json()["username"] == TEST_USER
            assert client.get("/", follow_redirects=False).status_code == 200
            assert client.get("/login", follow_redirects=False).status_code == 302  # already signed in
            with client.websocket_connect("/ws") as ws:
                assert ws.receive_json()["type"] == "hello"

            # Logout revokes the session.
            assert client.post("/api/auth/logout").json() == {"authenticated": False}
            assert client.get("/api/status").status_code == 401
    finally:
        engine.close()


def test_login_throttling(tmp_path: Path) -> None:
    config_path = make_config(tmp_path)
    engine = build_engine(AppConfig.load(config_path))
    try:
        with TestClient(create_app(engine, str(config_path))) as client:
            for _ in range(4):
                assert client.post("/api/auth/login", json={"username": TEST_USER, "password": "bad"}).status_code == 401
            locked = client.post("/api/auth/login", json={"username": TEST_USER, "password": "bad"})
            assert locked.status_code == 429 and "Retry-After" in locked.headers
            # Even the right password is refused while locked out.
            assert client.post("/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD}).status_code == 429
    finally:
        engine.close()


def test_first_run_setup(tmp_path: Path) -> None:
    config_path = make_config(tmp_path, login=False)
    engine = build_engine(AppConfig.load(config_path))
    try:
        with TestClient(create_app(engine, str(config_path))) as client:
            assert client.get("/api/auth/status").json()["setup_required"] is True
            assert client.post("/api/auth/login", json={"username": "admin", "password": "x"}).status_code == 409
            weak = client.post("/api/auth/setup", json={"username": "owner", "password": "short"})
            assert weak.status_code == 422 and "8 characters" in weak.json()["detail"]
            status = client.get("/api/auth/status").json()
            assert [p["name"] for p in status["providers"]] == ["alphavantage", "twelvedata", "csv", "demo"]
            assert client.post("/api/auth/setup", json={"username": "o", "password": "long enough password",
                                                        "provider": "nope"}).status_code == 422
            created = client.post("/api/auth/setup", json={"username": "owner", "password": "long enough password",
                                                           "provider": "twelvedata", "api_key": "key-from-setup"})
            assert created.status_code == 201 and created.json()["username"] == "owner"
            assert created.json()["status"]["provider"] == "twelvedata" and created.json()["status"]["api_key_set"]
            assert client.get("/api/status").status_code == 200  # signed in right away
            saved = AppConfig.load(config_path)
            assert saved.web.username == "owner" and saved.web.password_hash.startswith("pbkdf2_sha256$")
            assert saved.data.provider == "twelvedata" and saved.data.api_key == "key-from-setup"
            assert engine.data.provider.name == "twelvedata"
            assert "long enough password" not in config_path.read_text()
            # Setup is one-shot.
            assert client.post("/api/auth/setup", json={"username": "x", "password": "another password"}).status_code == 409
            with TestClient(create_app(engine, str(config_path))) as fresh:
                assert fresh.post("/api/auth/login", json={"username": "owner", "password": "long enough password"}).status_code == 200
    finally:
        engine.close()


def test_change_password(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, config_path = web
    wrong = client.post("/api/auth/password", json={"current_password": "nope", "new_password": "new password 1"})
    assert wrong.status_code == 401
    weak = client.post("/api/auth/password", json={"current_password": TEST_PASSWORD, "new_password": "short"})
    assert weak.status_code == 422
    changed = client.post("/api/auth/password", json={"current_password": TEST_PASSWORD, "new_password": "new password 1",
                                                      "username": "trader"})
    assert changed.status_code == 200 and changed.json()["username"] == "trader"
    assert client.get("/api/status").status_code == 200  # the caller keeps a fresh session
    assert AppConfig.load(config_path).web.username == "trader"
    assert client.get("/api/settings").json()["web"]["password_hash"] == ""  # never exposed
    with TestClient(create_app(engine, str(config_path))) as fresh:
        assert fresh.post("/api/auth/login", json={"username": "trader", "password": TEST_PASSWORD}).status_code == 401
        assert fresh.post("/api/auth/login", json={"username": "trader", "password": "new password 1"}).status_code == 200


# --------------------------------------------------------------- watchlist


def test_watchlist_crud_and_bars(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, config_path = web
    rows = client.get("/api/watchlist").json()
    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"] and rows[0]["price"] is None

    bars = client.get("/api/bars/AAPL").json()
    assert bars["symbol"] == "AAPL" and len(bars["bars"]) == 200
    t, o, h, lo, c, v = bars["bars"][-1]
    assert lo <= min(o, c) <= max(o, c) <= h and v > 0 and t > 1_600_000_000_000  # epoch milliseconds
    assert client.get("/api/watchlist").json()[0]["price"] == pytest.approx(c)

    added = client.post("/api/watchlist", json={"symbol": "tsla"})
    assert added.status_code == 201 and added.json()["added"] is True and "TSLA" in engine.watchlist
    assert engine.wait_for_workers(5.0)
    assert AppConfig.load(config_path).watchlist == ["AAPL", "MSFT", "TSLA"]
    assert client.post("/api/watchlist", json={"symbol": "TSLA"}).json()["added"] is False
    assert client.post("/api/watchlist", json={"symbol": "BAD SYMBOL"}).status_code == 422
    assert client.post("/api/watchlist", json={"symbol": ""}).status_code == 422

    assert client.delete("/api/watchlist/TSLA").json()["watchlist"] == ["AAPL", "MSFT"]
    assert client.delete("/api/watchlist/TSLA").status_code == 404
    assert client.post("/api/bars/NOPE/refresh").status_code == 404
    assert client.post("/api/bars/AAPL/refresh").json()["ok"] is True
    assert engine.wait_for_workers(5.0)


def test_fetch_errors_are_reported(tmp_path: Path) -> None:
    config_path = make_config(tmp_path, data={"provider": "twelvedata", "api_key": "k"})
    session = TwelveDataSession()
    session.mode = "auth"
    engine = build_engine(AppConfig.load(config_path), provider=TwelveDataProvider("k", session=session))
    try:
        with TestClient(create_app(engine)) as client:
            sign_in(client)
            response = client.get("/api/bars/AAPL")
            assert response.status_code == 503 and "apikey" in response.json()["detail"]
            assert client.get("/api/watchlist").json()[0]["error"]
            assert client.get("/api/status").json()["errors_by_symbol"]["AAPL"]
            session.mode = "ok"
            assert len(client.get("/api/bars/AAPL").json()["bars"]) == 200
            assert client.get("/api/watchlist").json()[0]["error"] is None
    finally:
        engine.close()


# -------------------------------------------------------------- strategies


def test_strategy_endpoints(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, _ = web
    names = [s["name"] for s in client.get("/api/strategies").json()]
    assert names[:2] == ["MA Crossover", "Always Buy"]
    help_ = client.get("/api/strategies/help").json()
    assert any(f["signature"].startswith("crossover(") for f in help_["functions"])

    body = {"name": "Breakout", "buy_rule": "close > highest(high, n)", "sell_rule": "close < lowest(low, n)",
            "params": {"n": 10}, "symbols": ["aapl"], "stop_loss_pct": 2.5}
    client.get("/api/bars/AAPL")  # load data so validation can evaluate the rule
    validation = client.post("/api/strategies/validate", json=body).json()
    assert validation["valid"] and validation["bars"] == 200 and validation["symbol"] == "AAPL"
    bad = client.post("/api/strategies/validate", json={**body, "buy_rule": "close > nope"}).json()
    assert not bad["valid"] and "nope" in bad["problems"][0]

    created = client.post("/api/strategies", json=body)
    assert created.status_code == 201 and created.json()["symbols"] == ["AAPL"]
    stored = engine.strategies.get("Breakout")
    assert stored is not None and stored.params == {"n": 10.0} and stored.stop_loss_pct == 2.5

    renamed = client.post("/api/strategies", json={**body, "name": "Breakout v2", "previous_name": "Breakout"})
    assert renamed.status_code == 201
    assert engine.strategies.get("Breakout") is None and engine.strategies.get("Breakout v2") is not None
    duplicate = client.post("/api/strategies", json={**body, "name": "MA Crossover", "previous_name": "Breakout v2"})
    assert duplicate.status_code == 422 and "already exists" in duplicate.json()["detail"]
    invalid = client.post("/api/strategies", json={**body, "name": "x", "buy_rule": "close >"})
    assert invalid.status_code == 422

    assert client.patch("/api/strategies/Breakout%20v2", json={"enabled": False}).json()["enabled"] is False
    v2 = engine.strategies.get("Breakout v2")
    assert v2 is not None and v2.enabled is False
    assert client.patch("/api/strategies/missing", json={"enabled": True}).status_code == 404
    assert client.delete("/api/strategies/Breakout%20v2").json()["deleted"] is True
    assert client.delete("/api/strategies/Breakout%20v2").status_code == 404


def test_overlay_and_backtest(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, _, _ = web
    client.get("/api/bars/AAPL")
    overlay = client.get("/api/bars/AAPL/overlay/MA%20Crossover").json()
    assert {s["name"] for s in overlay["price"]} == {"sma(fast)", "sma(slow)"}
    assert len(overlay["price"][0]["values"]) == 200 and overlay["price"][0]["values"][0] is None
    assert overlay["buy"] and overlay["sell"] and overlay["panes"] == []
    assert client.get("/api/bars/AAPL/overlay/missing").status_code == 404

    result = client.post("/api/backtest", json={"symbol": "AAPL", "strategy": "MA Crossover", "cash": 5000}).json()
    assert result["summary"]["strategy"] == "MA Crossover" and result["summary"]["trades"] > 0
    assert len(result["equity"]) == 200 and result["equity"][0] == 5000
    assert len(result["trades"]) == result["summary"]["trades"]
    assert result["buy"] and result["sell"]

    definition = {"name": "adhoc", "buy_rule": "crossover(sma(3), sma(9))", "sell_rule": "crossunder(sma(3), sma(9))"}
    adhoc = client.post("/api/backtest", json={"symbol": "AAPL", "definition": definition}).json()
    assert adhoc["summary"]["strategy"] == "adhoc"
    assert client.post("/api/backtest", json={"symbol": "AAPL"}).status_code == 422
    assert client.post("/api/backtest", json={"symbol": "ZZZZ", "strategy": "MA Crossover"}).status_code == 409
    assert client.post("/api/backtest", json={"symbol": "AAPL", "definition": {**definition, "buy_rule": "x"}}).status_code == 422


# ----------------------------------------------------- signals / positions


def test_scan_signals_positions_and_events(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, _ = web
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert client.post("/api/engine/scan").json()["ok"] is True
        assert engine.wait_for_workers(10.0)
        types: list[str] = []
        while True:
            message = ws.receive_json()
            types.append(message["type"])
            if message["type"] == "scan":
                assert message["symbols_scanned"] == 2 and message["status"]["scans"] == 1
                break
        assert "bars" in types and "signal" in types and "positions" in types

    signals = client.get("/api/signals").json()
    assert {s["symbol"] for s in signals} == {"AAPL", "MSFT"}
    assert all(s["action"] == "BUY" and s["executed"] for s in signals if s["strategy"] == "Always Buy")
    assert "headline" in signals[0] and "description" in signals[0]
    markers = client.get("/api/signals/markers/AAPL").json()
    assert markers["buy"] == [199]

    positions = client.get("/api/positions").json()
    assert positions["account"]["paper"] and positions["account"]["open_positions"] == 2
    assert {p["symbol"] for p in positions["positions"]} == {"AAPL", "MSFT"}
    assert len(positions["fills"]) == 2
    events = client.get("/api/events").json()
    assert any(e["type"] == "signal" for e in events)

    reset = client.post("/api/account/reset").json()
    assert reset["account"]["open_positions"] == 0 and reset["account"]["cash"] == 50_000


# ---------------------------------------------------------------- settings


def test_settings_roundtrip(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, config_path = web
    settings = client.get("/api/settings").json()
    assert settings["data"]["provider"] == "demo" and settings["data"]["api_key"] == "" and not settings["data"]["api_key_set"]
    assert "demo" in settings["options"]["providers"] and "5min" in settings["options"]["intervals"]

    response = client.put("/api/settings", json={
        "scan_interval": 120,
        "data": {"api_key": "new-key", "bars": 250, "closed_bars_only": True},
        "market_hours": {"enabled": True, "timezone": "Europe/London", "open": "08:00", "close": "16:30"},
        "notifications": {"webhook_url": "https://hooks.example/x", "sound": False},
        "trading": {"trading_enabled": False, "position_size_pct": 0.25},
    })
    assert response.status_code == 200
    body = response.json()
    assert body["data_reconfigured"] is True  # API key / bars changed -> data service rebuilt live
    assert engine.config.scan_interval == 120 and engine.config.data.bars == 250 and engine.config.data.closed_bars_only
    assert engine.config.market_hours.timezone == "Europe/London" and engine.config.trading.position_size_pct == 0.25
    assert engine.config.notifications.webhook_url == "https://hooks.example/x"
    assert any(type(c).__name__ == "WebhookChannel" for c in engine.notifier.channels)
    saved = AppConfig.load(config_path)
    assert saved.data.api_key == "new-key" and saved.scan_interval == 120
    assert client.get("/api/settings").json()["data"]["api_key_set"] is True

    # Blank key keeps the stored one; invalid values are rejected with details.
    assert client.put("/api/settings", json={"data": {"api_key": ""}}).json()["data_reconfigured"] is False
    assert AppConfig.load(config_path).data.api_key == "new-key"
    bad = client.put("/api/settings", json={"market_hours": {"timezone": "Mars/Olympus"}})
    assert bad.status_code == 422 and "time zone" in bad.json()["detail"]
    assert client.put("/api/settings", json={"scan_interval": 0}).status_code == 422
