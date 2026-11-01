"""Password hashing, sessions and login throttling."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.config import AppConfig
from trading_bot.web.auth import (
    LoginThrottle,
    SessionSigner,
    hash_password,
    load_or_create_secret,
    validate_password_strength,
    verify_password,
)

from .conftest import FakeClock


def test_hash_and_verify_password() -> None:
    stored = hash_password("s3cret passphrase", iterations=2000)
    assert stored.startswith("pbkdf2_sha256$2000$")
    assert verify_password("s3cret passphrase", stored)
    assert not verify_password("S3cret passphrase", stored)
    assert hash_password("x", iterations=1000) != hash_password("x", iterations=1000)  # random salt
    assert not verify_password("anything", "garbage")
    assert not verify_password("anything", "md5$1$salt$hash")
    assert not verify_password("anything", "")
    with pytest.raises(ValueError):
        hash_password("")


def test_password_strength() -> None:
    assert validate_password_strength("short") is not None
    assert validate_password_strength(" padded pass ") is not None
    assert validate_password_strength("long enough") is None


def test_session_signer_roundtrip_expiry_and_binding(tmp_path: Path) -> None:
    clock = FakeClock()
    signer = SessionSigner("secret", lifetime_seconds=100, clock=clock)
    password_hash = hash_password("pw", iterations=1000)
    token = signer.issue("alice", password_hash)
    assert signer.verify(token, password_hash) == "alice"
    assert signer.verify(None, password_hash) is None and signer.verify("garbage", password_hash) is None
    assert signer.verify(token + "x", password_hash) is None          # tampered signature
    body, sig = token.rsplit(".", 1)
    assert signer.verify(f"{body}x.{sig}", password_hash) is None     # tampered body
    assert SessionSigner("other secret", 100, clock=clock).verify(token, password_hash) is None
    assert signer.verify(token, hash_password("changed", iterations=1000)) is None  # bound to the password
    clock.advance(99)
    assert signer.verify(token, password_hash) == "alice"
    clock.advance(2)
    assert signer.verify(token, password_hash) is None  # expired

    fresh = signer.issue("alice", password_hash)
    signer.revoke(fresh)
    assert signer.verify(fresh, password_hash) is None  # logout on this instance
    signer.revoke("not a token")
    with pytest.raises(ValueError):
        SessionSigner("", 10)


def test_secret_is_generated_and_persisted(tmp_path: Path) -> None:
    path = tmp_path / "state" / "session.secret"
    first = load_or_create_secret(path)
    assert len(first) >= 40 and path.exists()
    assert load_or_create_secret(path) == first  # stable across restarts
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_login_throttle() -> None:
    clock = FakeClock()
    throttle = LoginThrottle(max_failures=3, lock_seconds=30, clock=clock)
    assert throttle.retry_after("1.2.3.4") == 0
    assert throttle.record_failure("1.2.3.4") == 0
    assert throttle.record_failure("1.2.3.4") == 0
    assert throttle.record_failure("1.2.3.4") == 30  # third failure locks
    assert throttle.retry_after("1.2.3.4") == pytest.approx(30)
    assert throttle.retry_after("other") == 0
    clock.advance(31)
    assert throttle.retry_after("1.2.3.4") == 0
    throttle.record_success("1.2.3.4")
    assert throttle.record_failure("1.2.3.4") == 0  # counter was reset


def test_env_password_is_hashed_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_BOT_WEB_PASSWORD", "from the environment")
    cfg = AppConfig.from_dict({"data": {"provider": "demo"}})
    assert cfg.web.password_hash.startswith("pbkdf2_sha256$")
    assert verify_password("from the environment", cfg.web.password_hash)


def test_env_overrides_any_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_bot.config import ConfigError

    monkeypatch.setenv("TRADING_BOT__DATA__PROVIDER", "demo")
    monkeypatch.setenv("TRADING_BOT__WATCHLIST", "aapl, msft")
    monkeypatch.setenv("TRADING_BOT__WEB__PORT", "9000")
    monkeypatch.setenv("TRADING_BOT__WEB__HOST", "0.0.0.0")
    monkeypatch.setenv("TRADING_BOT__MARKET_HOURS__ENABLED", "false")
    monkeypatch.setenv("TRADING_BOT__DATA__REQUESTS_PER_DAY", "77")
    monkeypatch.setenv("TRADING_BOT__TRADING__POSITION_SIZE_PCT", "0.2")
    cfg = AppConfig.from_dict({"watchlist": ["IBM"]})
    assert cfg.data.provider == "demo" and cfg.watchlist == ["AAPL", "MSFT"]
    assert cfg.web.port == 9000 and cfg.web.host == "0.0.0.0"
    assert cfg.market_hours.enabled is False and cfg.data.requests_per_day == 77
    assert cfg.trading.position_size_pct == 0.2
    monkeypatch.setenv("TRADING_BOT__WEB__PORT", "lots")
    with pytest.raises(ConfigError, match="expected a number"):
        AppConfig.from_dict({})
    monkeypatch.delenv("TRADING_BOT__WEB__PORT")
    monkeypatch.setenv("TRADING_BOT__NOPE__X", "1")
    with pytest.raises(ConfigError, match="unknown configuration section"):
        AppConfig.from_dict({})
    monkeypatch.delenv("TRADING_BOT__NOPE__X")
    monkeypatch.setenv("TRADING_BOT__WEB__NOPE", "1")
    with pytest.raises(ConfigError, match="unknown setting"):
        AppConfig.from_dict({})
