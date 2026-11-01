from __future__ import annotations

import json
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from trading_bot.config import AppConfig, ConfigError, EmailConfig, NotificationConfig
from trading_bot.notify import (
    CallbackChannel,
    EmailChannel,
    LogChannel,
    NotificationChannel,
    Notifier,
    WebhookChannel,
    build_notifier,
)
from trading_bot.strategy import ACTION_BUY, Signal

from .conftest import FakeResponse, FakeSession


def make_signal() -> Signal:
    return Signal("AAPL", ACTION_BUY, "strat", 123.456, datetime(2024, 1, 1, 15, 0, tzinfo=UTC), "because")


# ------------------------------------------------------------------ notify

def test_webhook_channel_posts_json() -> None:
    session = FakeSession([FakeResponse(200, {"ok": True})])
    ch = WebhookChannel("https://hooks.example/abc", session=session)
    ch.send(make_signal())
    method, url, body = session.calls[0]
    payload = json.loads(body)
    assert method == "POST" and url == "https://hooks.example/abc"
    assert payload["text"].startswith("BUY AAPL @ 123.46")
    assert payload["signal"]["symbol"] == "AAPL"
    with pytest.raises(ValueError):
        WebhookChannel("")


def test_webhook_channel_raises_on_http_error() -> None:
    session = FakeSession([FakeResponse(500, text="nope")])
    with pytest.raises(RuntimeError):
        WebhookChannel("https://x", session=session).send(make_signal())


def test_email_channel_builds_and_sends() -> None:
    sent: dict[str, Any] = {}

    class FakeSMTP:
        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def starttls(self) -> None:
            sent["tls"] = True

        def login(self, u: str, p: str) -> None:
            sent["login"] = (u, p)

        def send_message(self, msg: EmailMessage) -> None:
            sent["msg"] = msg

    cfg = EmailConfig(enabled=True, host="smtp.x", username="u", password="p", sender="bot@x", recipients=["a@x", "b@x"])
    ch = EmailChannel(cfg, smtp_factory=lambda: FakeSMTP())
    ch.send(make_signal())
    assert sent["tls"] and sent["login"] == ("u", "p")
    assert sent["msg"]["Subject"] == "[Trading Bot] BUY AAPL @ 123.46"
    assert sent["msg"]["To"] == "a@x, b@x"
    assert "because" in sent["msg"].get_content()
    with pytest.raises(ValueError):
        EmailChannel(EmailConfig(enabled=True))


def test_notifier_isolates_failures(caplog: pytest.LogCaptureFixture) -> None:
    class Boom(NotificationChannel):
        name = "boom"

        def send(self, signal: Signal) -> None:
            raise RuntimeError("down")

    received: list[Signal] = []
    n = Notifier([Boom(), CallbackChannel(received.append), LogChannel()])
    with caplog.at_level("WARNING"):
        delivered = n.notify(make_signal())
    assert delivered == 2
    assert received and received[0].symbol == "AAPL"
    assert n.failures == {"boom": 1}
    assert any("TRADE SIGNAL" in r.message for r in caplog.records)
    boom = n.channels[0]
    n.remove_channel(boom)
    assert len(n.channels) == 2


def test_build_notifier_from_config() -> None:
    cfg = NotificationConfig(webhook_url="https://hook", email=EmailConfig(enabled=True, host="h", recipients=["a"]))
    names = [c.name for c in build_notifier(cfg).channels]
    assert names == ["log", "webhook", "email"]
    cfg = NotificationConfig(email=EmailConfig(enabled=True))  # incomplete email config is skipped
    assert [c.name for c in build_notifier(cfg).channels] == ["log"]


# ------------------------------------------------------------------ config

def test_config_defaults_and_roundtrip(tmp_path: Path) -> None:
    cfg = AppConfig()
    path = tmp_path / "config.json"
    cfg.save(path)
    loaded = AppConfig.load(path)
    assert loaded.to_dict() == cfg.to_dict()
    assert loaded.data.budget() == (5, 25)
    assert loaded.requires_api_key()


def test_config_legacy_migration(tmp_path: Path) -> None:
    legacy = {
        "api_key": "LEGACYKEY", "scan_interval": 30, "profit_threshold": 0.02, "max_stocks_to_analyze": 50,
        "trading_enabled": True, "tickers_source": "alphavantage", "strategy": "combined", "rsi_period": 14,
        "rsi_oversold": 30, "rsi_overbought": 70, "short_ma_period": 5, "long_ma_period": 20,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(legacy))
    cfg = AppConfig.load(path)
    assert cfg.data.api_key == "LEGACYKEY"
    assert cfg.data.provider == "alphavantage"
    assert cfg.scan_interval == 30
    assert cfg.trading.trading_enabled is True
    assert "rsi_period" not in cfg.to_dict()


def test_config_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_BOT_API_KEY", "ENVKEY")
    monkeypatch.setenv("TRADING_BOT_WEBHOOK_URL", "https://env-hook")
    monkeypatch.setenv("TRADING_BOT_SMTP_PASSWORD", "secret")
    cfg = AppConfig.from_dict({"data": {"api_key": "file"}})
    assert cfg.data.api_key == "ENVKEY"
    assert cfg.notifications.webhook_url == "https://env-hook"
    assert cfg.notifications.email.password == "secret"
    assert AppConfig.load(tmp_path / "missing.json").data.api_key == "ENVKEY"


@pytest.mark.parametrize("raw, match", [
    ({"scan_interval": 0}, "scan_interval"),
    ({"data": {"interval": "2min"}}, "interval"),
    ({"data": {"provider": "bloomberg"}}, "provider"),
    ({"data": {"bars": 5}}, "bars"),
    ({"trading": {"position_size_pct": 2}}, "position_size_pct"),
    ({"trading": {"initial_cash": 0}}, "initial_cash"),
    ({"watchlist": ["AA PL"]}, "Invalid symbol"),
])
def test_config_validation_errors(raw: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        AppConfig.from_dict(raw)


def test_config_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        AppConfig.load(path)
    path.write_text("[]")
    with pytest.raises(ConfigError):
        AppConfig.load(path)


def test_config_unknown_keys_are_ignored(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        cfg = AppConfig.from_dict({"data": {"provider": "demo", "mystery": 1}, "watchlist": [" aapl ", ""]})
    assert cfg.watchlist == ["AAPL"]
    assert cfg.data.provider == "demo" and not cfg.requires_api_key()
    assert any("mystery" in r.message for r in caplog.records)


def test_config_paths_resolve_relative_to_config_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "config.json").write_text(json.dumps({"data_dir": "state", "strategies_file": "strats.json",
                                                     "data": {"provider": "csv", "csv_directory": "bars"}}))
    cfg = AppConfig.load(project / "config.json")
    assert cfg.data_dir_path == project / "state"
    assert cfg.strategies_path == project / "strats.json"
    assert cfg.csv_directory_path == project / "bars"
    assert cfg.data_dir == "state"  # stored values stay relative so save() keeps the file portable
    cfg.save(project / "config.json")
    assert "base_dir" not in json.loads((project / "config.json").read_text())
    (project / "abs.json").write_text(json.dumps({"data_dir": str(tmp_path / "elsewhere")}))
    assert AppConfig.load(project / "abs.json").data_dir_path == tmp_path / "elsewhere"
    assert AppConfig().data_dir_path == Path("bot_data")  # in-memory config: relative to the cwd
