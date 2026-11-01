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
            # The token acts on behalf of the administrator account.
            assert client.get("/api/auth/status", headers={"Authorization": "Bearer s3cret"}).json()["username"] == TEST_USER
            assert client.post("/api/auth/password", headers={"Authorization": "Bearer s3cret"},
                               json={"current_password": TEST_PASSWORD, "new_password": "New-password-1A"}).status_code == 200
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
            assert client.get("/", follow_redirects=False).headers["location"] == "login"  # relative: base-path safe
            login_page = client.get("/login")
            assert login_page.status_code == 200 and 'id="loginForm"' in login_page.text
            assert client.get("/api/status").status_code == 401
            assert client.post("/api/engine/start").status_code == 401
            status = client.get("/api/auth/status").json()
            assert status["authenticated"] is False and status["username"] is None
            assert status["setup_required"] is False and status["user"] is None
            assert status["allow_signup"] is True and status["verification_required"] is False
            assert status["email_configured"] is False
            with pytest.raises(Exception):  # noqa: B017 - WebSocket closed with 4401
                with client.websocket_connect("/ws"):
                    pass

            # Wrong credentials.
            bad = client.post("/api/auth/login", json={"username": TEST_USER, "password": "nope"})
            assert bad.status_code == 401 and "Invalid" in bad.json()["detail"]
            assert client.post("/api/auth/login", json={"username": "someone", "password": TEST_PASSWORD}).status_code == 401
            assert client.post("/api/auth/setup", json={"username": "x", "password": "Whatever-123!"}).status_code == 409

            # Login sets an HttpOnly session cookie; everything works afterwards.
            ok = client.post("/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD})
            assert ok.status_code == 200 and ok.json()["authenticated"] is True and ok.json()["username"] == TEST_USER
            assert ok.json()["user"]["role"] == "admin" and ok.json()["user"]["is_admin"] is True
            cookie = ok.headers["set-cookie"]
            assert "tb_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie.lower().replace("samesite=lax", "SameSite=lax")
            assert client.get("/api/status").status_code == 200
            assert client.get("/api/auth/status").json()["username"] == TEST_USER
            assert client.get("/api/auth/me").json()["username"] == TEST_USER
            assert client.get("/", follow_redirects=False).status_code == 200
            assert client.get("/signup", follow_redirects=False).status_code == 302  # already signed in
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
            assert weak.status_code == 422 and "At least 10 characters" in weak.json()["detail"]
            weak = client.post("/api/auth/setup", json={"username": "owner", "password": "Owner-secret-99!"})
            assert weak.status_code == 422 and "username" in weak.json()["detail"]
            check = client.post("/api/auth/password-check", json={"password": "Owner-sec", "username": "owner"}).json()
            assert check["passed"] is False and check["score"] <= 2
            failed = {r["id"] for r in check["rules"] if not r["passed"]}
            assert failed == {"length", "digit", "not_username"}
            assert client.post("/api/auth/password-check", json={"password": "Long-enough-pass1"}).json()["passed"] is True
            status = client.get("/api/auth/status").json()
            assert [p["name"] for p in status["providers"]] == ["alphavantage", "twelvedata", "csv", "demo"]
            assert client.post("/api/auth/setup", json={"username": "o", "password": "Long-enough-pass1",
                                                        "provider": "nope"}).status_code == 422
            created = client.post("/api/auth/setup", json={"username": "owner", "password": "Long-enough-pass1",
                                                           "provider": "twelvedata", "api_key": "key-from-setup"})
            assert created.status_code == 201 and created.json()["username"] == "owner"
            assert created.json()["status"]["provider"] == "twelvedata" and created.json()["status"]["api_key_set"]
            assert client.get("/api/status").status_code == 200  # signed in right away
            saved = AppConfig.load(config_path)
            # Credentials live in the user store, not in the config file any more.
            assert saved.web.username == "owner" and saved.web.password_hash == ""
            admin = engine.storage.find_user("owner")
            assert admin is not None and admin["role"] == "admin" and admin["email_verified"]
            assert saved.data.provider == "twelvedata" and saved.data.api_key == "key-from-setup"
            assert engine.data.provider.name == "twelvedata"
            assert "Long-enough-pass1" not in config_path.read_text()
            # Setup is one-shot.
            assert client.post("/api/auth/setup", json={"username": "x", "password": "Another-pass4!"}).status_code == 409
            with TestClient(create_app(engine, str(config_path))) as fresh:
                assert fresh.post("/api/auth/login", json={"username": "owner", "password": "Long-enough-pass1"}).status_code == 200
    finally:
        engine.close()


def test_change_password(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, config_path = web
    wrong = client.post("/api/auth/password", json={"current_password": "nope", "new_password": "New-password-1A"})
    assert wrong.status_code == 401
    weak = client.post("/api/auth/password", json={"current_password": TEST_PASSWORD, "new_password": "short"})
    assert weak.status_code == 422
    changed = client.post("/api/auth/password", json={"current_password": TEST_PASSWORD, "new_password": "New-password-1A"})
    assert changed.status_code == 200 and changed.json()["username"] == TEST_USER
    assert client.get("/api/status").status_code == 200  # the caller keeps a fresh session
    assert client.get("/api/settings").json()["web"]["password_hash"] == ""  # never exposed
    with TestClient(create_app(engine, str(config_path))) as fresh:
        assert fresh.post("/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD}).status_code == 401
        assert fresh.post("/api/auth/login", json={"username": TEST_USER, "password": "New-password-1A"}).status_code == 200


def test_profile_email_and_auto_trade(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, _ = web
    me = client.get("/api/auth/me").json()
    assert me["email"] is None and me["auto_trade"] is True
    assert client.patch("/api/auth/profile", json={"email": "not-an-email"}).status_code == 422
    updated = client.patch("/api/auth/profile", json={"email": "Admin@Example.com", "auto_trade": False}).json()
    assert updated["email"] == "admin@example.com" and updated["auto_trade"] is False
    assert engine.accounts.is_enabled(TEST_USER) is False
    assert client.get("/api/positions").json()["account"]["auto_trade"] is False
    # The e-mail can now be used to sign in.
    with TestClient(client.app) as fresh:
        assert fresh.post("/api/auth/login", json={"username": "admin@example.com", "password": TEST_PASSWORD}).status_code == 200
    assert client.patch("/api/auth/profile", json={"email": "", "auto_trade": True}).json()["email"] is None


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
    assert all(s["action"] == "BUY" for s in signals)
    # Exactly one BUY per symbol is executed (the first strategy that fired on the bar opens the position; a second
    # strategy signalling the same symbol on the same bar is notified but must not add to the position).
    executed = [s for s in signals if s["executed"]]
    assert sorted(s["symbol"] for s in executed) == ["AAPL", "MSFT"]
    assert any(s["strategy"] == "Always Buy" for s in signals)
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


def test_settings_ai_and_signup_policy(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, config_path = web
    settings = client.get("/api/settings").json()
    assert settings["ai"] == {"enabled": False, "base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt-4o-mini",
                              "timeout_seconds": 45.0, "api_key_set": False}
    assert settings["web"]["allow_signup"] is True and settings["web"]["email_verification"] == "auto"
    assert settings["email_configured"] is False and settings["notifications"]["email"]["password"] == ""
    response = client.put("/api/settings", json={
        "ai": {"enabled": True, "api_key": "sk-secret", "model": "gpt-test", "base_url": "https://llm.example/v1"},
        "web": {"allow_signup": False, "email_verification": "off", "public_url": "https://bot.example.com/",
                "username": "hacker", "host": "0.0.0.0", "token": "x"},  # only the sign-up fields are writable here
        "notifications": {"email": {"transport": "outbox", "sender": "bot@example.com", "password": "smtp-secret"}},
    })
    assert response.status_code == 200 and response.json()["data_reconfigured"] is False
    saved = AppConfig.load(config_path)
    assert saved.ai.enabled and saved.ai.api_key == "sk-secret" and saved.ai.model == "gpt-test"
    assert saved.web.allow_signup is False and saved.web.email_verification == "off" and saved.web.public_url == "https://bot.example.com/"
    assert saved.web.username == TEST_USER and saved.web.host == "127.0.0.1" and saved.web.token == ""
    assert saved.notifications.email.transport == "outbox" and saved.notifications.email.password == "smtp-secret"
    again = client.get("/api/settings").json()
    assert again["ai"]["api_key"] == "" and again["ai"]["api_key_set"] is True and again["email_configured"] is True
    assert client.get("/api/auth/status").json()["allow_signup"] is False
    # Blank secrets keep the stored values; invalid policy values are rejected.
    client.put("/api/settings", json={"ai": {"api_key": ""}, "notifications": {"email": {"password": ""}}})
    saved = AppConfig.load(config_path)
    assert saved.ai.api_key == "sk-secret" and saved.notifications.email.password == "smtp-secret"
    assert client.put("/api/settings", json={"web": {"email_verification": "sometimes"}}).status_code == 422
    assert client.put("/api/settings", json={"notifications": {"email": {"transport": "pigeon"}}}).status_code == 422


# ------------------------------------------------------ sign-up / accounts


def verification_token(data_dir: Path) -> str:
    import email

    files = sorted((data_dir / "outbox").glob("*.eml"))
    payload = email.message_from_bytes(files[-1].read_bytes()).get_payload(decode=True)
    assert isinstance(payload, bytes)
    return payload.decode().split("token=")[1].split()[0]


def test_signup_verification_and_per_user_accounts(tmp_path: Path) -> None:
    config_path = make_config(tmp_path, notifications={"desktop": False, "sound": False,
                                                       "email": {"transport": "outbox", "sender": "bot@example.com"}},
                              web={"public_url": "https://bot.example.com"})
    engine = build_engine(AppConfig.load(config_path))
    try:
        with TestClient(create_app(engine, str(config_path))) as client:
            status = client.get("/api/auth/status").json()
            assert status["allow_signup"] and status["verification_required"] and status["email_configured"]
            assert client.get("/signup").status_code == 200 and 'id="signupForm"' in client.get("/signup").text
            # Validation and duplicate detection.
            assert client.post("/api/auth/signup", json={"username": "bob", "password": "Sunny-Trail-42!"}).status_code == 422
            assert client.post("/api/auth/signup", json={"username": TEST_USER, "email": "x@example.com",
                                                         "password": "Sunny-Trail-42!"}).status_code == 409
            created = client.post("/api/auth/signup", json={"username": "bob", "email": "Bob@Example.com", "password": "Sunny-Trail-42!"})
            assert created.status_code == 201
            assert created.json() == {"authenticated": False, "verification_required": True, "email": "bob@example.com", "username": "bob"}
            assert "set-cookie" not in created.headers
            token = verification_token(tmp_path / "bot_data")
            files = list((tmp_path / "bot_data" / "outbox").glob("*.eml"))
            assert len(files) == 1 and "https://bot.example.com/verify?token=" in files[0].read_text()

            # Not verified: 403 with a hint; resend creates a new mail; bad token -> redirect with error.
            refused = client.post("/api/auth/login", json={"username": "bob", "password": "Sunny-Trail-42!"})
            assert refused.status_code == 403 and "verify your e-mail" in refused.json()["detail"]
            assert client.post("/api/auth/resend-verification", json={"identifier": "bob@example.com"}).json()["ok"] is True
            assert client.post("/api/auth/resend-verification", json={"identifier": "nobody"}).json()["ok"] is True  # no enumeration
            assert len(list((tmp_path / "bot_data" / "outbox").glob("*.eml"))) == 2
            bad = client.get("/verify?token=" + token, follow_redirects=False)  # superseded by the resend
            assert bad.status_code == 302 and bad.headers["location"].startswith("login?verify_error=")
            good = client.get("/verify?token=" + verification_token(tmp_path / "bot_data"), follow_redirects=False)
            assert good.status_code == 302 and good.headers["location"] == "login?verified=1"

            # Sign in with the e-mail address; Bob gets his own paper account and is no admin.
            sign_in(client, "BOB@example.com", "Sunny-Trail-42!")
            me = client.get("/api/auth/me").json()
            assert me["username"] == "bob" and me["role"] == "user" and me["is_admin"] is False and me["email_verified"]
            positions = client.get("/api/positions").json()
            assert positions["account"]["name"] == "bob" and positions["account"]["cash"] == 50_000
            assert positions["account"]["auto_trade"] is True  # follows trading.trading_enabled at registration
            assert client.get("/api/users").status_code == 403  # administrators only
            assert client.put("/api/settings", json={"scan_interval": 7}).status_code == 200  # shared engine settings
            # Signals execute in both accounts; each user sees only their own fills.
            assert client.post("/api/engine/scan").json()["ok"] is True
            assert engine.wait_for_workers(10.0)
            bob_fills = client.get("/api/positions").json()["fills"]
            assert len(bob_fills) == 2 and {f["account"] for f in bob_fills} == {"bob"}
            assert {f["account"] for f in engine.storage.recent_fills()} == {"bob", TEST_USER}
            assert sorted(engine.accounts.names()) == [TEST_USER, "bob"]
            assert (tmp_path / "bot_data" / "accounts" / "bob.json").exists()
            client.post("/api/auth/logout")

            # The administrator lists users and sees an independent account.
            sign_in(client)
            users = client.get("/api/users").json()
            assert [u["username"] for u in users] == [TEST_USER, "bob"] and users[1]["email"] == "bob@example.com"
            assert client.get("/api/positions").json()["account"]["name"] == TEST_USER
            assert len(client.get("/api/positions").json()["fills"]) == 2

            # Sign-up can be switched off.
            client.put("/api/settings", json={"web": {"allow_signup": False}})
            client.post("/api/auth/logout")
            assert client.post("/api/auth/signup", json={"username": "eve", "email": "e@example.com",
                                                         "password": "Sunny-Trail-42!"}).status_code == 403
            assert client.get("/signup", follow_redirects=False).status_code == 302
    finally:
        engine.close()


def test_signup_without_mail_server_activates_immediately(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, _ = web
    client.post("/api/auth/logout")
    assert client.get("/api/auth/status").json()["verification_required"] is False
    created = client.post("/api/auth/signup", json={"username": "carol", "password": "Sunny-Trail-42!"})
    assert created.status_code == 201 and created.json()["authenticated"] is True and created.json()["user"]["email"] is None
    assert client.get("/api/auth/me").json()["username"] == "carol"  # signed in straight away
    # "required" without a transport refuses new registrations instead of creating unusable accounts.
    engine.config.web.email_verification = "required"
    client.post("/api/auth/logout")
    refused = client.post("/api/auth/signup", json={"username": "dave", "email": "d@example.com", "password": "Sunny-Trail-42!"})
    assert refused.status_code == 503


# ------------------------------------------------------- history / insights


def seed_history(engine: TradingEngine, account: str) -> None:
    from datetime import UTC, datetime, timedelta

    from trading_bot.broker import Fill

    when = datetime(2024, 3, 4, 15, 0, tzinfo=UTC)
    for symbol, strategy, qty, entry, exit_ in [("AAPL", "MA Crossover", 10, 100.0, 106.0), ("MSFT", "Always Buy", 5, 300.0, 294.0),
                                                ("AAPL", "MA Crossover", 10, 106.0, 110.0)]:
        engine.storage.record_fill(Fill(symbol, "BUY", qty, entry, when, strategy), account)
        when += timedelta(hours=3)
        engine.storage.record_fill(Fill(symbol, "SELL", qty, exit_, when, strategy, (exit_ - entry) * qty), account)
        when += timedelta(hours=21)


def test_history_and_analytics_endpoints(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, _ = web
    empty = client.get("/api/history").json()
    assert empty["stats"]["trades"] == 0 and empty["fills"] == [] and empty["pnl_curve"] == []
    assert empty["account_summary"]["name"] == TEST_USER
    seed_history(engine, TEST_USER)
    seed_history(engine, "someone-else")  # must not leak into the admin's report
    history = client.get("/api/history?limit=4").json()
    assert history["account"] == TEST_USER and history["stats"]["trades"] == 3
    assert history["stats"]["total_pnl"] == 70.0 and history["stats"]["wins"] == 2
    assert [p["cumulative"] for p in history["pnl_curve"]] == [60.0, 30.0, 70.0]
    assert len(history["trades"]) == 3 and history["trades"][0]["symbol"] == "AAPL"
    assert len(history["fills"]) == 4 and history["fills"][0]["side"] == "SELL"  # newest first, limited
    assert client.get("/api/history?limit=0").status_code == 422

    analytics = client.get("/api/analytics").json()
    assert analytics["trades"] == [] and analytics["stats"]["trades"] == 3 and analytics["open_positions"] == 0
    assert analytics["ai_available"] is False and analytics["ai_model"] is None
    assert [r["name"] for r in analytics["by_strategy"]] == ["MA Crossover", "Always Buy"]
    assert analytics["by_strategy"][0]["total_pnl"] == 100.0
    assert analytics["by_symbol"][0]["name"] == "AAPL" and len(analytics["by_weekday"]) >= 1
    assert any(i["title"] == "Small sample" for i in analytics["insights"])
    assert client.post("/api/analytics/ai").status_code == 409  # not configured


def test_ai_analysis_endpoint(web: tuple[TestClient, TradingEngine, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    client, engine, _ = web
    seed_history(engine, TEST_USER)
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append(json.loads(request.content))
        if request.headers["authorization"] != "Bearer sk-test":
            return httpx.Response(401, text="bad key")
        return httpx.Response(200, json={"model": "gpt-test", "usage": {"total_tokens": 7},
                                         "choices": [{"message": {"content": "Solid start; mind the losers."}}]})

    app: Any = client.app
    monkeypatch.setattr(app.state.ai, "_client", httpx.Client(transport=httpx.MockTransport(handler)))
    client.put("/api/settings", json={"ai": {"enabled": True, "api_key": "sk-test", "model": "gpt-test",
                                             "base_url": "https://llm.example/v1"}})
    analytics = client.get("/api/analytics").json()
    assert analytics["ai_available"] is True and analytics["ai_model"] == "gpt-test"
    result = client.post("/api/analytics/ai")
    assert result.status_code == 200
    body = result.json()
    assert body["text"] == "Solid start; mind the losers." and body["model"] == "gpt-test" and body["generated_at"]
    prompt = calls[-1]["messages"][1]["content"]
    assert "MA Crossover" in prompt and "sk-test" not in prompt and TEST_USER not in prompt  # stats only, no secrets / PII
    client.put("/api/settings", json={"ai": {"api_key": "wrong"}})
    failed = client.post("/api/analytics/ai")
    assert failed.status_code == 502 and "HTTP 401" in failed.json()["detail"]


# ----------------------------------------------------------- deployment


def test_base_path_cookie_and_relative_redirects(tmp_path: Path) -> None:
    """Behind a reverse proxy under /bots/a the app must use relative URLs and a scoped cookie."""
    config_path = make_config(tmp_path, web={"base_path": "/bots/a"})
    engine = build_engine(AppConfig.load(config_path))
    try:
        with TestClient(create_app(engine, str(config_path)), root_path="/bots/a") as client:
            assert client.get("/", follow_redirects=False).headers["location"] == "login"
            page = client.get("/login")
            assert 'href="static/css/material.css"' in page.text  # relative assets
            login = client.post("/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD})
            assert "Path=/bots/a/" in login.headers["set-cookie"]  # scoped to the shard's prefix
            cookie = {"Cookie": f"tb_session={login.cookies['tb_session']}"}  # the browser sends it under /bots/a/
            assert client.get("/api/status", headers=cookie).status_code == 200
            assert client.get("/login", headers=cookie, follow_redirects=False).headers["location"] == "./"
            assert client.get("/api/openapi.json").json()["servers"][0]["url"] == "/bots/a"
    finally:
        engine.close()


def test_health_and_readiness(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, _, _ = web
    health = client.get("/api/health").json()
    assert health["ok"] is True and "version" in health
    ready = client.get("/api/ready")
    assert ready.status_code == 200 and ready.json()["ok"] is True and ready.json()["websocket_clients"] == 0
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert client.get("/api/ready").json()["websocket_clients"] == 1


def test_sessions_survive_restart_and_gzip(tmp_path: Path) -> None:
    """Stateless signed sessions: a cookie issued by one process is valid for another with the same secret."""
    config_path = make_config(tmp_path, web={"secret_key": "shared-secret"})
    engine = build_engine(AppConfig.load(config_path))
    try:
        with TestClient(create_app(engine, str(config_path))) as first:
            sign_in(first)
            cookie = first.cookies["tb_session"]
            response = first.get("/api/bars/AAPL", headers={"Accept-Encoding": "gzip"})
            assert response.headers.get("content-encoding") == "gzip"  # large JSON is compressed
            assert len(response.json()["bars"]) == 200
        with TestClient(create_app(engine, str(config_path))) as second:  # "another replica / after restart"
            second.cookies.set("tb_session", cookie)
            assert second.get("/api/status").status_code == 200
    finally:
        engine.close()


def test_symbol_search_and_names(web: tuple[TestClient, TradingEngine, Path]) -> None:
    client, engine, _ = web
    rows = client.get("/api/watchlist").json()
    assert rows[0]["name"] == "Apple Inc." and rows[1]["name"] == "Microsoft Corporation"

    results = client.get("/api/symbols/search?q=app").json()
    symbols = [r["symbol"] for r in results["results"]]
    assert symbols[0] == "APP" and "AAPL" in symbols and "AMAT" in symbols  # exact symbol first, then names
    assert all(r["source"] == "local" for r in results["results"])
    assert results["remote_supported"] is False  # demo provider has no remote search
    apple = next(r for r in results["results"] if r["symbol"] == "AAPL")
    assert apple["name"] == "Apple Inc." and apple["type"] == "stock"

    by_name = client.get("/api/symbols/search?q=micros&limit=3").json()["results"]
    assert by_name[0]["symbol"] == "MSFT"
    assert client.get("/api/symbols/search?q=").json()["results"] == []
    assert client.get("/api/symbols/search?q=zzzzzz").json()["results"] == []
    assert client.get("/api/symbols/AAPL").json()["name"] == "Apple Inc."
    assert client.get("/api/symbols/ZZZZ").status_code == 404
    assert client.get("/api/symbols/search?q=spy").json()["results"][0]["type"] == "etf"


def test_symbol_search_remote_fallback(tmp_path: Path) -> None:
    """Provider search is only used on explicit request and spends one request of the budget."""
    config_path = make_config(tmp_path, data={"provider": "twelvedata", "api_key": "k"})
    session = TwelveDataSession()
    engine = build_engine(AppConfig.load(config_path), provider=TwelveDataProvider("k", session=session))
    try:
        with TestClient(create_app(engine, str(config_path))) as client:
            sign_in(client)
            local = client.get("/api/symbols/search?q=zzqq").json()
            assert local["results"] == [] and local["remote_supported"] is True and not session.calls
            remote = client.get("/api/symbols/search?q=zzqq&remote=true").json()
            assert remote["results"] == [{"symbol": "ZZQQ", "name": "Zzqq Holdings", "type": "Common Stock",
                                          "source": "twelvedata", "region": "United States"}]
            assert session.calls[-1][1].endswith("/symbol_search")
            budget_after = client.get("/api/status").json()["budget"]["remaining_day"]
            assert budget_after == 800 - 1
    finally:
        engine.close()
