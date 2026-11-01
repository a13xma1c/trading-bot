"""User accounts: registration, e-mail verification and authentication.

Users live in the SQLite store.  The first account is the administrator; it is
created on the first-run setup page or migrated from the legacy
``web.username`` / ``web.password_hash`` configuration.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..brokers import BROKER_PROVIDERS
from ..config import AppConfig
from ..storage import Storage
from .auth import hash_password, validate_password_strength, verify_password
from .mail import Mailer

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VERIFICATION_HOURS = 24


class UserError(ValueError):
    """Invalid input (reported to the client as HTTP 422 / 409)."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class User:
    id: int
    username: str
    email: str | None
    password_hash: str
    role: str
    email_verified: bool
    auto_trade: bool
    created_at: str
    last_login: str | None = None
    broker_provider: str = "paper"      # execution venue of this user's account (see trading_bot.brokers)
    broker_key: str | None = None
    broker_secret: str | None = None
    broker_paper: bool = True           # external broker in paper/sandbox mode

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def broker_connected(self) -> bool:
        return self.broker_provider != "paper" and bool(self.broker_key)

    def broker_public(self) -> dict[str, Any]:
        key = self.broker_key or ""
        return {"provider": self.broker_provider, "paper": self.broker_paper, "connected": self.broker_connected,
                "key_hint": f"…{key[-4:]}" if len(key) >= 4 else ""}

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "username": self.username, "email": self.email, "role": self.role, "is_admin": self.is_admin,
                "email_verified": self.email_verified, "auto_trade": self.auto_trade, "created_at": self.created_at,
                "last_login": self.last_login, "broker": self.broker_public()}

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> User:
        return cls(id=int(row["id"]), username=row["username"], email=row.get("email"), password_hash=row["password_hash"],
                   role=row.get("role") or "user", email_verified=bool(row.get("email_verified")),
                   auto_trade=bool(row.get("auto_trade", 1)), created_at=row.get("created_at") or "",
                   last_login=row.get("last_login"), broker_provider=row.get("broker_provider") or "paper",
                   broker_key=row.get("broker_key"), broker_secret=row.get("broker_secret"),
                   broker_paper=bool(row.get("broker_paper", 1)))


class UserService:
    def __init__(self, storage: Storage, config: AppConfig, mailer: Mailer | None = None) -> None:
        self.storage = storage
        self.config = config
        self.mailer = mailer

    # ------------------------------------------------------------ queries
    def count(self) -> int:
        return self.storage.count_users()

    def setup_required(self) -> bool:
        return self.count() == 0

    def get(self, user_id: int) -> User | None:
        row = self.storage.get_user(user_id)
        return User.from_row(row) if row else None

    def find(self, identifier: str) -> User | None:
        if not identifier or not identifier.strip():
            return None
        row = self.storage.find_user(identifier)
        return User.from_row(row) if row else None

    def list(self) -> list[User]:
        return [User.from_row(row) for row in self.storage.list_users()]

    # ----------------------------------------------------------- policies
    @property
    def verification_required(self) -> bool:
        mode = self.config.web.email_verification
        if mode == "off":
            return False
        if mode == "required":
            return True
        return bool(self.mailer and self.mailer.configured)

    def migrate_legacy_admin(self) -> User | None:
        """Apply credentials from the configuration (``web.password_hash``: set by ``trading-bot set-password``
        or ``TRADING_BOT_WEB_PASSWORD``).

        With an empty user table this creates the administrator (upgrade path from single-user versions);
        otherwise it resets the administrator's password - the documented recovery procedure.  The hash is
        cleared from the configuration afterwards (the caller persists the config).
        """
        hashed = self.config.web.password_hash
        if not hashed:
            return None
        username = (self.config.web.username or "admin").strip()
        existing = self.find(username)
        result: User | None = None
        if existing is None and self.count() == 0:
            row = self.storage.create_user(username, None, hashed, role="admin", email_verified=True,
                                           auto_trade=self.config.trading.trading_enabled)
            result = User.from_row(row)
            logger.info("Migrated administrator %r from the configuration file", username)
        elif existing is not None and existing.is_admin:
            if existing.password_hash != hashed:
                self.storage.update_user(existing.id, password_hash=hashed)
                logger.warning("Password of administrator %r was reset from the configuration", username)
            result = self.get(existing.id)
        else:
            logger.warning("Ignoring web.password_hash: %r is not an administrator account", username)
        self.config.web.password_hash = ""
        return result

    # ---------------------------------------------------------- registration
    def validate_username(self, username: str) -> str:
        username = username.strip()
        if not USERNAME_RE.match(username):  # no '@' either, so usernames and e-mails cannot collide at login
            raise UserError("Username must be 2-32 characters: letters, digits, '.', '_' or '-'")
        return username

    def validate_email(self, email: str | None, required: bool) -> str | None:
        email = (email or "").strip().lower()
        if not email:
            if required:
                raise UserError("E-mail address is required")
            return None
        if not EMAIL_RE.match(email) or len(email) > 254:
            raise UserError("Invalid e-mail address")
        return email

    def register(self, username: str, email: str | None, password: str, *, role: str = "user",
                 base_url: str = "") -> tuple[User, bool]:
        """Create an account.  Returns the user and whether e-mail verification is pending."""
        username = self.validate_username(username)
        needs_verification = role != "admin" and self.verification_required
        email_value = self.validate_email(email, required=needs_verification)
        problem = validate_password_strength(password, username)
        if problem:
            raise UserError(problem)
        if self.find(username) or (email_value and self.find(email_value)):
            raise UserError("Username or e-mail already registered", status=409)
        if needs_verification and not (self.mailer and self.mailer.configured):
            raise UserError("E-mail verification is required but e-mail delivery is not configured", status=503)
        token = secrets.token_urlsafe(32) if needs_verification else None
        expires = datetime.now(UTC) + timedelta(hours=VERIFICATION_HOURS) if needs_verification else None
        row = self.storage.create_user(username, email_value, hash_password(password), role=role,
                                       email_verified=not needs_verification, verification_token=token,
                                       verification_expires=expires, auto_trade=self.config.trading.trading_enabled)
        user = User.from_row(row)
        if needs_verification:
            self._send_verification(user, token or "", base_url)
        logger.info("User %r registered (%s, verification %s)", user.username, user.role,
                    "pending" if needs_verification else "not required")
        return user, needs_verification

    def resend_verification(self, identifier: str, base_url: str = "") -> bool:
        user = self.find(identifier)
        if user is None or user.email_verified or not user.email:
            return False
        token = secrets.token_urlsafe(32)
        self.storage.update_user(user.id, verification_token=token,
                                 verification_expires=datetime.now(UTC) + timedelta(hours=VERIFICATION_HOURS))
        self._send_verification(user, token, base_url)
        return True

    def verify(self, token: str) -> User:
        row = self.storage.find_user_by_token(token) if token else None
        if row is None:
            raise UserError("This verification link is invalid or has already been used")
        expires = row.get("verification_expires")
        if expires and datetime.fromisoformat(expires) < datetime.now(UTC):
            raise UserError("This verification link has expired; request a new one")
        self.storage.update_user(int(row["id"]), email_verified=True, verification_token=None, verification_expires=None)
        user = self.get(int(row["id"]))
        assert user is not None
        logger.info("E-mail of user %r verified", user.username)
        return user

    def _send_verification(self, user: User, token: str, base_url: str) -> None:
        assert self.mailer is not None and user.email
        link = f"{(base_url or '').rstrip('/')}/verify?token={token}"
        body = (f"Hello {user.username},\n\nConfirm your e-mail address to activate your Trading Bot account:\n\n"
                f"{link}\n\nThe link is valid for {VERIFICATION_HOURS} hours. If you did not sign up, ignore this message.\n")
        self.mailer.send(user.email, "Verify your Trading Bot account", body)

    # ------------------------------------------------------------- sign in
    def authenticate(self, identifier: str, password: str) -> User:
        """Return the user for valid credentials; raise UserError otherwise."""
        user = self.find(identifier)
        if user is None or not verify_password(password, user.password_hash):
            raise UserError("Invalid username/e-mail or password", status=401)
        if not user.email_verified:
            raise UserError("Please verify your e-mail address first (check your inbox or request a new link)",
                            status=403)
        self.storage.update_user(user.id, last_login=datetime.now(UTC))
        return user

    # -------------------------------------------------------------- broker
    def set_broker_connection(self, user: User, provider: str, key: str | None, secret: str | None,
                              paper: bool = True) -> User:
        """Store the user's brokerage credentials (blank key/secret keep the stored ones)."""
        provider = (provider or "paper").lower()
        if provider not in BROKER_PROVIDERS:
            raise UserError(f"Unknown broker provider {provider!r}")
        fields: dict[str, Any] = {"broker_provider": provider, "broker_paper": bool(paper)}
        if provider == "paper":
            fields.update(broker_key=None, broker_secret=None, broker_paper=True)
        else:
            new_key = (key or "").strip() or user.broker_key
            new_secret = (secret or "").strip() or user.broker_secret
            if not new_key or not new_secret:
                raise UserError("API key and secret are required to connect a brokerage")
            fields.update(broker_key=new_key, broker_secret=new_secret)
        self.storage.update_user(user.id, **fields)
        updated = self.get(user.id)
        assert updated is not None
        return updated

    # -------------------------------------------------------------- profile
    def change_password(self, user: User, current_password: str, new_password: str) -> User:
        if not verify_password(current_password, user.password_hash):
            raise UserError("Current password is incorrect", status=401)
        problem = validate_password_strength(new_password, user.username)
        if problem:
            raise UserError(problem)
        self.storage.update_user(user.id, password_hash=hash_password(new_password))
        updated = self.get(user.id)
        assert updated is not None
        return updated

    def update_profile(self, user: User, *, username: str | None = None, email: str | None = None,
                       auto_trade: bool | None = None) -> User:
        fields: dict[str, Any] = {}
        if username is not None and username.strip() and username.strip() != user.username:
            fields["username"] = self.validate_username(username)
        if email is not None and (email or "").strip().lower() != (user.email or ""):
            fields["email"] = self.validate_email(email, required=False)
        if auto_trade is not None:
            fields["auto_trade"] = auto_trade
        if fields:
            try:
                self.storage.update_user(user.id, **fields)
            except ValueError as exc:
                raise UserError(str(exc), status=409) from exc
        updated = self.get(user.id)
        assert updated is not None
        return updated
