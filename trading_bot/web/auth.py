"""Authentication primitives: password hashing, sessions and login throttling.

Passwords are stored as ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` (standard
library only).  Sessions are stateless HMAC-signed tokens carried in an
HttpOnly cookie; they expire after ``web.session_hours``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PBKDF2_ITERATIONS = 600_000
_ALGORITHM = "pbkdf2_sha256"


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([_ALGORITHM, str(iterations), _b64(salt), _b64(digest)])


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, digest = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        expected = base64.urlsafe_b64decode(digest + "=" * (-len(digest) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _unb64(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128
# Frequently leaked passwords (lower-cased, compared without symbols/digits stripped).
COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "passw0rd", "p@ssw0rd", "123456", "12345678", "123456789",
    "1234567890", "qwerty", "qwerty123", "qwertyuiop", "abc123", "letmein", "welcome", "welcome1", "admin",
    "admin123", "administrator", "iloveyou", "monkey", "dragon", "sunshine", "princess", "football", "baseball",
    "master", "login", "trading", "trader", "trading123", "bitcoin", "changeme", "secret", "test1234",
})

PASSWORD_RULES: list[tuple[str, str]] = [
    ("length", f"At least {MIN_PASSWORD_LENGTH} characters"),
    ("lowercase", "A lowercase letter"),
    ("uppercase", "An uppercase letter"),
    ("digit", "A digit"),
    ("symbol", "A symbol (e.g. ! ? - _ #)"),
    ("no_edges", "No leading or trailing spaces"),
    ("not_username", "Does not contain the username"),
    ("not_common", "Not a commonly used password"),
    ("max_length", f"At most {MAX_PASSWORD_LENGTH} characters"),
]


def password_rules(password: str, username: str | None = None) -> list[dict[str, Any]]:
    """Evaluate every password rule; mirrors ``static/js/passwords.js`` for live feedback."""
    user = (username or "").strip().lower()
    lowered = password.lower()
    results = {
        "length": len(password) >= MIN_PASSWORD_LENGTH,
        "lowercase": any(c.islower() for c in password),
        "uppercase": any(c.isupper() for c in password),
        "digit": any(c.isdigit() for c in password),
        "symbol": any(not c.isalnum() and not c.isspace() for c in password),
        "no_edges": password == password.strip(),
        "not_username": not user or len(user) < 3 or user not in lowered,
        "not_common": lowered not in COMMON_PASSWORDS and lowered.rstrip("0123456789!?.") not in COMMON_PASSWORDS,
        "max_length": len(password) <= MAX_PASSWORD_LENGTH,
    }
    return [{"id": rule_id, "label": label, "passed": results[rule_id]} for rule_id, label in PASSWORD_RULES]


def password_score(password: str, username: str | None = None) -> int:
    """0-4 strength estimate for the UI meter (rules met + length bonus)."""
    rules = password_rules(password, username)
    core = sum(1 for r in rules if r["passed"] and r["id"] in ("lowercase", "uppercase", "digit", "symbol"))
    if not all(r["passed"] for r in rules):
        return min(core, 2)
    if len(password) >= 16 and core == 4:
        return 4
    return 3 if core >= 3 else 2


def validate_password_strength(password: str, username: str | None = None) -> str | None:
    """Return a description of the unmet requirements, or None if the password is acceptable."""
    failed = [r["label"] for r in password_rules(password, username) if not r["passed"]]
    if not failed:
        return None
    return "Password requirements not met: " + "; ".join(failed)


class SessionSigner:
    """Stateless, HMAC-signed session tokens (no server-side session storage needed).

    A token carries the username, an expiry and a fingerprint of the password
    hash, so changing the password invalidates every session, and any instance
    that knows the secret can verify tokens (restarts and replicas keep users
    signed in).  Explicit logouts are remembered per process until they expire.
    """

    def __init__(self, secret: str | bytes, lifetime_seconds: float = 12 * 3600, clock=time.time) -> None:
        if not secret:
            raise ValueError("session secret must not be empty")
        self._secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self.lifetime = lifetime_seconds
        self._clock = clock
        self._revoked: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def fingerprint(password_hash: str) -> str:
        return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]

    def issue(self, username: str, password_hash: str) -> str:
        expires = int(self._clock() + self.lifetime)
        payload = f"{username}|{expires}|{self.fingerprint(password_hash)}|{secrets.token_hex(8)}"
        body = _b64(payload.encode("utf-8"))
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str | None, password_hash: str) -> str | None:
        """Return the username for a valid token, or None."""
        if not token or "." not in token:
            return None
        body, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(self._sign(body), signature):
            return None
        try:
            username, expires, fingerprint, _nonce = _unb64(body).decode("utf-8").split("|")
        except (ValueError, UnicodeDecodeError):
            return None
        now = self._clock()
        if int(expires) <= now or fingerprint != self.fingerprint(password_hash):
            return None
        with self._lock:
            for key in [k for k, until in self._revoked.items() if until <= now]:
                del self._revoked[key]
            if token in self._revoked:
                return None
        return username

    @staticmethod
    def peek_username(token: str | None) -> str | None:
        """Username claimed by a token (unverified - always follow up with :meth:`verify`)."""
        if not token or "." not in token:
            return None
        try:
            return _unb64(token.rsplit(".", 1)[0]).decode("utf-8").split("|")[0] or None
        except (ValueError, UnicodeDecodeError, IndexError):
            return None

    def revoke(self, token: str | None) -> None:
        if not token or "." not in token:
            return
        body = token.rsplit(".", 1)[0]
        try:
            expires = int(_unb64(body).decode("utf-8").split("|")[1])
        except (ValueError, UnicodeDecodeError, IndexError):
            return
        with self._lock:
            self._revoked[token] = float(expires)

    def _sign(self, body: str) -> str:
        return hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).hexdigest()


def load_or_create_secret(path: os.PathLike[str] | str) -> str:
    """Read the session secret from ``path``, creating a random one (mode 0600) if missing."""
    file = Path(path)
    if file.exists():
        secret = file.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    file.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    file.write_text(secret, encoding="utf-8")
    try:
        os.chmod(file, 0o600)
    except OSError:  # pragma: no cover - e.g. unsupported filesystems
        pass
    return secret


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0
    history: list[float] = field(default_factory=list)


class LoginThrottle:
    """Locks a client out for ``lock_seconds`` after ``max_failures`` failed logins."""

    def __init__(self, max_failures: int = 5, lock_seconds: float = 60.0, clock=time.time) -> None:
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._clock = clock
        self._attempts: dict[str, _Attempts] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> float:
        """Seconds until ``key`` may try again (0 when not locked)."""
        with self._lock:
            entry = self._attempts.get(key)
            if entry is None:
                return 0.0
            return max(0.0, entry.locked_until - self._clock())

    def record_failure(self, key: str) -> float:
        with self._lock:
            entry = self._attempts.setdefault(key, _Attempts())
            entry.failures += 1
            if entry.failures >= self.max_failures:
                entry.locked_until = self._clock() + self.lock_seconds
                entry.failures = 0
                return self.lock_seconds
            return 0.0

    def record_success(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
