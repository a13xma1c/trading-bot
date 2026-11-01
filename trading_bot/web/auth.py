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


def validate_password_strength(password: str) -> str | None:
    """Return a problem description or None if the password is acceptable."""
    if len(password) < 8:
        return "Password must be at least 8 characters long"
    if password.strip() != password:
        return "Password must not start or end with whitespace"
    return None


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
