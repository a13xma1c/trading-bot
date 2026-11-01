"""Password hashing, sessions and login throttling."""

from __future__ import annotations

import pytest

from trading_bot.config import AppConfig
from trading_bot.web.auth import (
    LoginThrottle,
    SessionStore,
    hash_password,
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


def test_session_store_expiry_and_revocation() -> None:
    clock = FakeClock()
    store = SessionStore(lifetime_seconds=100, clock=clock)
    session = store.create("alice")
    assert store.get(session.token) is session and store.get(None) is None and store.get("nope") is None
    assert store.count() == 1
    clock.advance(99)
    assert store.get(session.token) is not None
    clock.advance(2)
    assert store.get(session.token) is None  # expired
    assert store.count() == 0
    s1, s2 = store.create("alice"), store.create("bob")
    store.revoke(s1.token)
    assert store.get(s1.token) is None and store.get(s2.token) is not None
    store.revoke_all("bob")
    assert store.count() == 0


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
