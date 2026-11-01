"""User accounts: registration, e-mail verification, login by username or e-mail, and the mailer."""

from __future__ import annotations

import email
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from trading_bot.config import AppConfig, EmailConfig
from trading_bot.storage import Storage
from trading_bot.web.auth import hash_password, verify_password
from trading_bot.web.mail import Mailer
from trading_bot.web.users import UserError, UserService

GOOD_PASSWORD = "Sunny-Trail-42!"


def make_service(tmp_path: Path, *, transport: str | None = "outbox", **web: Any) -> tuple[UserService, Mailer, Storage]:
    config = AppConfig.from_dict({"web": {"open_browser": False, **web}, "trading": {"trading_enabled": True},
                                  "notifications": {"email": {"transport": transport or "smtp"}}})
    mailer = Mailer(config.notifications.email, outbox_dir=tmp_path / "outbox")
    storage = Storage(":memory:")
    return UserService(storage, config, mailer), mailer, storage


def outbox_messages(tmp_path: Path) -> list[email.message.Message]:
    files = sorted((tmp_path / "outbox").glob("*.eml"))
    return [email.message_from_bytes(f.read_bytes()) for f in files]


def token_from(message: email.message.Message) -> str:
    body = message.get_payload(decode=True)
    assert isinstance(body, bytes)
    text = body.decode()
    return text.split("token=")[1].split()[0]


# ------------------------------------------------------------------- mailer


def test_mailer_outbox_and_smtp(tmp_path: Path) -> None:
    unconfigured = Mailer(EmailConfig(), outbox_dir=None)
    assert unconfigured.configured is False
    with pytest.raises(RuntimeError, match="not configured"):
        unconfigured.send("a@b.c", "s", "b")

    outbox = Mailer(EmailConfig(transport="outbox", sender="bot@example.com"), outbox_dir=tmp_path / "outbox")
    assert outbox.configured
    outbox.send("Bob <bob@example.com>", "Hello", "Line one\nhttps://example.com/verify?token=" + "x" * 90 + "\n")
    outbox.send("bob@example.com", "Again", "second")
    messages = outbox_messages(tmp_path)
    assert len(messages) == 2 and messages[0]["Subject"] == "Hello" and messages[0]["From"] == "bot@example.com"
    body = messages[0].get_payload(decode=True)
    assert isinstance(body, bytes) and ("x" * 90) in body.decode()  # long URLs are not soft-wrapped

    class FakeSMTP:
        calls: list[tuple[str, Any]] = []

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *exc: object) -> None:
            FakeSMTP.calls.append(("quit", None))

        def starttls(self) -> None:
            FakeSMTP.calls.append(("starttls", None))

        def login(self, user: str, password: str) -> None:
            FakeSMTP.calls.append(("login", (user, password)))

        def send_message(self, msg: Any) -> None:
            FakeSMTP.calls.append(("send", (msg["To"], msg["Subject"])))

    cfg = EmailConfig(host="smtp.example.com", username="u", password="p", sender="bot@example.com")
    smtp = Mailer(cfg, smtp_factory=FakeSMTP)
    assert smtp.configured
    smtp.send("bob@example.com", "Subject", "Body")
    assert FakeSMTP.calls == [("starttls", None), ("login", ("u", "p")), ("send", ("bob@example.com", "Subject")),
                              ("quit", None)]


# --------------------------------------------------------------- registration


def test_register_with_verification_and_login_by_email(tmp_path: Path) -> None:
    users, mailer, storage = make_service(tmp_path)
    assert users.setup_required() and users.verification_required  # outbox transport counts as configured
    admin, pending = users.register("admin", None, GOOD_PASSWORD, role="admin")
    assert admin.is_admin and admin.email_verified and pending is False  # administrators never need verification
    assert not users.setup_required()

    bob, pending = users.register(" Bob ", "Bob@Example.COM", GOOD_PASSWORD, base_url="https://bot.example.com/app")
    assert pending is True and bob.username == "Bob" and bob.email == "bob@example.com" and not bob.email_verified
    assert bob.auto_trade is True  # follows trading.trading_enabled
    messages = outbox_messages(tmp_path)
    assert len(messages) == 1 and messages[0]["To"] == "bob@example.com"
    body = messages[0].get_payload(decode=True)
    assert isinstance(body, bytes) and "https://bot.example.com/app/verify?token=" in body.decode()

    # Unverified accounts cannot sign in (403), wrong passwords are 401 regardless.
    with pytest.raises(UserError, match="verify your e-mail") as unverified:
        users.authenticate("bob", GOOD_PASSWORD)
    assert unverified.value.status == 403
    with pytest.raises(UserError, match="Invalid") as wrong:
        users.authenticate("bob", "nope")
    assert wrong.value.status == 401
    with pytest.raises(UserError, match="invalid or has already been used"):
        users.verify("bogus-token")
    with pytest.raises(UserError):
        users.verify("")

    verified = users.verify(token_from(messages[0]))
    assert verified.email_verified and verified.id == bob.id
    with pytest.raises(UserError, match="invalid or has already been used"):
        users.verify(token_from(messages[0]))  # one-shot
    # Login works with the username or the e-mail address, case-insensitively, and records last_login.
    assert users.authenticate("BOB", GOOD_PASSWORD).id == bob.id
    signed_in = users.authenticate("bob@example.com", GOOD_PASSWORD)
    assert signed_in.id == bob.id and signed_in.last_login is not None
    assert [u.username for u in users.list()] == ["admin", "Bob"]
    assert users.find("") is None and users.find("  ") is None


def test_resend_and_expired_token(tmp_path: Path) -> None:
    users, mailer, storage = make_service(tmp_path)
    users.register("admin", None, GOOD_PASSWORD, role="admin")
    bob, _ = users.register("bob", "bob@example.com", GOOD_PASSWORD)
    first = token_from(outbox_messages(tmp_path)[0])
    assert users.resend_verification("bob@example.com") is True
    second = token_from(outbox_messages(tmp_path)[1])
    assert second != first
    with pytest.raises(UserError):
        users.verify(first)  # superseded
    # Expired links are refused with a distinct message.
    storage.update_user(bob.id, verification_expires=datetime.now(UTC) - timedelta(minutes=1))
    with pytest.raises(UserError, match="expired"):
        users.verify(second)
    assert users.resend_verification("nobody") is False
    assert users.resend_verification("admin") is False  # already verified / no e-mail
    assert users.verify(token_from(outbox_messages(tmp_path)[-1]) if users.resend_verification("bob") else "").email_verified
    assert users.resend_verification("bob") is False  # verified now


def test_registration_validation(tmp_path: Path) -> None:
    users, mailer, storage = make_service(tmp_path)
    users.register("admin", None, GOOD_PASSWORD, role="admin")
    with pytest.raises(UserError, match="Username must be"):
        users.register("a", "a@example.com", GOOD_PASSWORD)
    with pytest.raises(UserError, match="Username must be"):
        users.register("bad name!", "a@example.com", GOOD_PASSWORD)
    with pytest.raises(UserError, match="Username must be"):
        users.validate_username("a@b.c")  # no '@': usernames and e-mails cannot collide at login
    with pytest.raises(UserError, match="E-mail address is required"):
        users.register("bob", "", GOOD_PASSWORD)  # verification is on
    with pytest.raises(UserError, match="Invalid e-mail"):
        users.register("bob", "not-an-email", GOOD_PASSWORD)
    with pytest.raises(UserError, match="At least 10 characters"):
        users.register("bob", "bob@example.com", "Short1!")
    with pytest.raises(UserError, match="username"):
        users.register("bob", "bob@example.com", "Bob-bob-bob-1!")
    users.register("bob", "bob@example.com", GOOD_PASSWORD)
    with pytest.raises(UserError, match="already registered") as dup:
        users.register("BOB", "other@example.com", GOOD_PASSWORD)
    assert dup.value.status == 409
    with pytest.raises(UserError, match="already registered"):
        users.register("other", "BOB@example.com", GOOD_PASSWORD)
    assert len(outbox_messages(tmp_path)) == 1  # only the successful registration sent a mail


def test_verification_policy(tmp_path: Path) -> None:
    # "auto" without any mail transport: accounts are active immediately and e-mail is optional.
    users, mailer, storage = make_service(tmp_path, transport=None)
    assert users.verification_required is False
    users.register("admin", None, GOOD_PASSWORD, role="admin")
    bob, pending = users.register("bob", None, GOOD_PASSWORD)
    assert pending is False and bob.email_verified and bob.email is None
    assert users.authenticate("bob", GOOD_PASSWORD).id == bob.id
    # "required" without a transport refuses registration instead of creating unusable accounts.
    strict, _, _ = make_service(tmp_path, transport=None, email_verification="required")
    strict.register("admin", None, GOOD_PASSWORD, role="admin")
    with pytest.raises(UserError, match="not configured") as exc:
        strict.register("bob", "bob@example.com", GOOD_PASSWORD)
    assert exc.value.status == 503
    # "off" with a transport: no verification even though mail could be sent.
    relaxed, _, _ = make_service(tmp_path, email_verification="off")
    assert relaxed.verification_required is False
    relaxed.register("admin", None, GOOD_PASSWORD, role="admin")
    assert relaxed.register("bob", "bob@example.com", GOOD_PASSWORD)[1] is False
    assert outbox_messages(tmp_path) == []


# ------------------------------------------------------------------- profile


def test_change_password_and_profile(tmp_path: Path) -> None:
    users, mailer, storage = make_service(tmp_path, transport=None)
    admin, _ = users.register("admin", None, GOOD_PASSWORD, role="admin")
    with pytest.raises(UserError, match="Current password is incorrect") as wrong:
        users.change_password(admin, "nope", "Another-pass-9!")
    assert wrong.value.status == 401
    with pytest.raises(UserError, match="At least 10"):
        users.change_password(admin, GOOD_PASSWORD, "short")
    changed = users.change_password(admin, GOOD_PASSWORD, "Another-pass-9!")
    assert verify_password("Another-pass-9!", changed.password_hash) and changed.password_hash != admin.password_hash

    updated = users.update_profile(changed, email="Admin@Example.com", auto_trade=False)
    assert updated.email == "admin@example.com" and updated.auto_trade is False
    assert users.authenticate("admin@example.com", "Another-pass-9!").id == admin.id
    users.register("bob", "bob@example.com", GOOD_PASSWORD)
    with pytest.raises(UserError, match="already") as taken:
        users.update_profile(updated, email="bob@example.com")
    assert taken.value.status == 409
    with pytest.raises(UserError, match="Invalid e-mail"):
        users.update_profile(updated, email="nope")
    cleared = users.update_profile(updated, email="")
    assert cleared.email is None
    same = users.update_profile(cleared)  # nothing to change
    assert same.public()["email"] is None and same.public()["is_admin"] is True and "password_hash" not in same.public()


def test_migrate_legacy_admin_and_password_reset(tmp_path: Path) -> None:
    """The old config-file credentials create the admin once, later they reset the admin password."""
    config = AppConfig.from_dict({"web": {"open_browser": False, "username": "owner", "password_hash": hash_password("Legacy-pass-1!", iterations=1000)},
                                  "trading": {"trading_enabled": False}})
    storage = Storage(":memory:")
    users = UserService(storage, config, None)
    migrated = users.migrate_legacy_admin()
    assert migrated is not None and migrated.username == "owner" and migrated.is_admin and migrated.email_verified
    assert migrated.auto_trade is False and config.web.password_hash == ""  # cleared: the store is authoritative
    assert users.authenticate("owner", "Legacy-pass-1!").id == migrated.id
    assert users.migrate_legacy_admin() is None  # nothing pending

    # `trading-bot set-password` writes a new hash -> applied as a reset at the next start.
    config.web.password_hash = hash_password("Reset-pass-2!", iterations=1000)
    reset = users.migrate_legacy_admin()
    assert reset is not None and users.authenticate("owner", "Reset-pass-2!").id == migrated.id
    with pytest.raises(UserError):
        users.authenticate("owner", "Legacy-pass-1!")

    # A hash for a non-admin user name is ignored (and cleared) rather than creating a second admin.
    users.register("bob", None, GOOD_PASSWORD)
    config.web.username = "bob"
    config.web.password_hash = hash_password("Whatever-3!", iterations=1000)
    assert users.migrate_legacy_admin() is None and config.web.password_hash == ""
    assert users.authenticate("bob", GOOD_PASSWORD).username == "bob"
