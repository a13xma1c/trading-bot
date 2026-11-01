"""Authentication primitives: password hashing, sessions and login throttling.

Passwords are stored as ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` (standard
library only).  Sessions live in memory and are identified by a random token
carried in an HttpOnly cookie; they expire after ``web.session_hours``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field

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


@dataclass
class Session:
    token: str
    username: str
    created_at: float
    expires_at: float
    last_seen: float = 0.0


class SessionStore:
    """Thread-safe in-memory session store."""

    def __init__(self, lifetime_seconds: float = 12 * 3600, clock=time.time) -> None:
        self.lifetime = lifetime_seconds
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> Session:
        now = self._clock()
        session = Session(secrets.token_urlsafe(32), username, now, now + self.lifetime, now)
        with self._lock:
            self._prune(now)
            self._sessions[session.token] = session
        return session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        now = self._clock()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if session.expires_at <= now:
                del self._sessions[token]
                return None
            session.last_seen = now
            return session

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all(self, username: str | None = None) -> None:
        with self._lock:
            for key in [k for k, s in self._sessions.items() if username is None or s.username == username]:
                del self._sessions[key]

    def count(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._sessions)

    def _prune(self, now: float) -> None:
        for key in [k for k, s in self._sessions.items() if s.expires_at <= now]:
            del self._sessions[key]


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
