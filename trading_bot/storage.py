"""SQLite persistence: signals, fills, the transaction ledger and user accounts (history survives restarts)."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .broker import Fill
from .strategy.base import Signal

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    bar_time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    strategy TEXT NOT NULL,
    price REAL NOT NULL,
    reason TEXT,
    executed INTEGER NOT NULL DEFAULT 0,
    account TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    strategy TEXT,
    realized_pnl REAL NOT NULL DEFAULT 0,
    account TEXT NOT NULL DEFAULT '',
    commission REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    email_verified INTEGER NOT NULL DEFAULT 0,
    verification_token TEXT,
    verification_expires TEXT,
    auto_trade INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login TEXT,
    broker_provider TEXT NOT NULL DEFAULT 'paper',
    broker_key TEXT,
    broker_secret TEXT,
    broker_paper INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- deposit | withdrawal | buy | sell | adjustment
    status TEXT NOT NULL DEFAULT 'completed',  -- pending | completed | failed | cancelled
    amount REAL NOT NULL,               -- signed cash change in the account currency (net of fees)
    currency TEXT NOT NULL DEFAULT 'USD',
    fee REAL NOT NULL DEFAULT 0,
    symbol TEXT,
    quantity REAL,
    price REAL,
    description TEXT NOT NULL DEFAULT '',
    reference TEXT,                     -- fill id, payment session id, payout id ...
    source_amount REAL,                 -- original amount / currency of a deposit before conversion
    source_currency TEXT,
    balance_after REAL,
    metadata TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS transactions_account ON transactions (account, timestamp);
CREATE INDEX IF NOT EXISTS transactions_reference ON transactions (reference);
"""

TRANSACTION_KINDS = ("deposit", "withdrawal", "buy", "sell", "adjustment")
TRANSACTION_STATUSES = ("pending", "completed", "failed", "cancelled")

_MIGRATIONS = [
    # (table, column, definition) - added when missing so older databases keep working.
    ("fills", "account", "TEXT NOT NULL DEFAULT ''"),
    ("signals", "account", "TEXT NOT NULL DEFAULT ''"),
    ("fills", "commission", "REAL NOT NULL DEFAULT 0"),
    ("users", "broker_provider", "TEXT NOT NULL DEFAULT 'paper'"),
    ("users", "broker_key", "TEXT"),
    ("users", "broker_secret", "TEXT"),
    ("users", "broker_paper", "INTEGER NOT NULL DEFAULT 1"),
]
_INDEXES = """
DROP INDEX IF EXISTS signals_dedupe;
CREATE UNIQUE INDEX IF NOT EXISTS signals_dedupe_v2 ON signals (strategy, symbol, action, bar_time, account);
"""


class Storage:
    def __init__(self, path: str | os.PathLike[str] = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            for table, column, definition in _MIGRATIONS:
                columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self._conn.executescript(_INDEXES)
            self._conn.commit()

    # ------------------------------------------------------------- signals
    def record_signal(self, signal: Signal, executed: bool = False) -> bool:
        """Store ``signal``; returns False if an identical signal was already stored."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO signals (id, created_at, bar_time, symbol, action, strategy, price, reason, executed,"
                    " account) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (signal.id, signal.created_at.isoformat(), signal.bar_time.isoformat(), signal.symbol,
                     signal.action, signal.strategy, signal.price, signal.reason, int(executed), signal.account),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def has_signal(self, signal: Signal) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM signals WHERE strategy=? AND symbol=? AND action=? AND bar_time=? AND account=?",
                (signal.strategy, signal.symbol, signal.action, signal.bar_time.isoformat(), signal.account),
            ).fetchone()
        return row is not None

    def mark_executed(self, signal_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE signals SET executed=1 WHERE id=?", (signal_id,))
            self._conn.commit()

    def recent_signals(self, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM signals"
        params: tuple = ()
        if symbol:
            query += " WHERE symbol=?"
            params = (symbol.upper(),)
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, params + (limit,)).fetchall()
        return [dict(r) for r in rows]

    def signals_as_objects(self, limit: int = 100) -> list[Signal]:
        out = []
        for row in self.recent_signals(limit):
            out.append(Signal(
                symbol=row["symbol"], action=row["action"], strategy=row["strategy"], price=row["price"],
                bar_time=datetime.fromisoformat(row["bar_time"]), reason=row["reason"] or "",
                created_at=datetime.fromisoformat(row["created_at"]), id=row["id"], account=row.get("account") or "",
            ))
        return out

    # --------------------------------------------------------------- fills
    def record_fill(self, fill: Fill, account: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO fills (id, timestamp, symbol, side, quantity, price, strategy, realized_pnl, account,"
                " commission) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fill.id, fill.timestamp.isoformat(), fill.symbol, fill.side, fill.quantity, fill.price,
                 fill.strategy, fill.realized_pnl, account, fill.commission),
            )
            self._conn.commit()

    def recent_fills(self, limit: int = 100, account: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM fills", []
        if account is not None:
            query += " WHERE account = ?"
            params.append(account)
        query += " ORDER BY timestamp DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (*params, limit)).fetchall()
        return [dict(r) for r in rows]

    def fills_for_account(self, account: str) -> list[dict[str, Any]]:
        """All fills of an account, oldest first (for trade history / P&L curves)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM fills WHERE account = ? ORDER BY timestamp ASC, rowid ASC",
                                      (account,)).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- users
    def create_user(self, username: str, email: str | None, password_hash: str, role: str = "user",
                    email_verified: bool = False, verification_token: str | None = None,
                    verification_expires: datetime | None = None, auto_trade: bool = True) -> dict[str, Any]:
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "INSERT INTO users (username, email, password_hash, role, email_verified, verification_token,"
                    " verification_expires, auto_trade, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (username, email or None, password_hash, role, int(email_verified), verification_token,
                     verification_expires.isoformat() if verification_expires else None, int(auto_trade),
                     datetime.now(UTC).isoformat()),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("username or e-mail already registered") from exc
            user_id = cursor.lastrowid
        user = self.get_user(int(user_id or 0))
        assert user is not None
        return user

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def find_user(self, identifier: str) -> dict[str, Any] | None:
        """Look a user up by username or e-mail (case-insensitive)."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE username = ? OR email = ?",
                                     (identifier.strip(), identifier.strip())).fetchone()
        return dict(row) if row else None

    def find_user_by_token(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE verification_token = ?", (token,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def count_users(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def update_user(self, user_id: int, **fields: Any) -> None:
        allowed = {"username", "email", "password_hash", "email_verified", "verification_token",
                   "verification_expires", "auto_trade", "last_login", "role", "broker_provider", "broker_key",
                   "broker_secret", "broker_paper"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update {', '.join(sorted(unknown))}")
        if not fields:
            return
        values = []
        for value in fields.values():
            if isinstance(value, bool):
                value = int(value)
            elif isinstance(value, datetime):
                value = value.isoformat()
            values.append(value)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            try:
                self._conn.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*values, user_id))
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("username or e-mail already registered") from exc

    # -------------------------------------------------------- transactions
    def record_transaction(self, account: str, kind: str, amount: float, *, currency: str = "USD",
                           status: str = "completed", fee: float = 0.0, symbol: str | None = None,
                           quantity: float | None = None, price: float | None = None, description: str = "",
                           reference: str | None = None, source_amount: float | None = None,
                           source_currency: str | None = None, balance_after: float | None = None,
                           metadata: dict[str, Any] | None = None, timestamp: datetime | None = None,
                           transaction_id: str | None = None) -> dict[str, Any]:
        """Append an entry to the account ledger (deposits, withdrawals, trades, adjustments)."""
        if kind not in TRANSACTION_KINDS:
            raise ValueError(f"unknown transaction kind {kind!r}")
        if status not in TRANSACTION_STATUSES:
            raise ValueError(f"unknown transaction status {status!r}")
        when = (timestamp or datetime.now(UTC)).isoformat()
        tx_id = transaction_id or uuid.uuid4().hex[:16]
        with self._lock:
            self._conn.execute(
                "INSERT INTO transactions (id, account, timestamp, kind, status, amount, currency, fee, symbol, quantity,"
                " price, description, reference, source_amount, source_currency, balance_after, metadata, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tx_id, account, when, kind, status, float(amount), currency, float(fee), symbol, quantity, price,
                 description, reference, source_amount, source_currency, balance_after, json.dumps(metadata or {}), when),
            )
            self._conn.commit()
        row = self.transaction(tx_id)
        assert row is not None
        return row

    def update_transaction(self, transaction_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"status", "amount", "fee", "description", "reference", "balance_after", "metadata", "price",
                   "quantity", "source_amount", "source_currency"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update {', '.join(sorted(unknown))}")
        if "status" in fields and fields["status"] not in TRANSACTION_STATUSES:
            raise ValueError(f"unknown transaction status {fields['status']!r}")
        if "metadata" in fields:
            fields["metadata"] = json.dumps(fields["metadata"] or {})
        fields["updated_at"] = datetime.now(UTC).isoformat()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._conn.execute(f"UPDATE transactions SET {assignments} WHERE id = ?", (*fields.values(), transaction_id))
            self._conn.commit()
        return self.transaction(transaction_id)

    def transaction(self, transaction_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
        return _transaction_row(row) if row else None

    def transaction_by_reference(self, reference: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM transactions WHERE reference = ? ORDER BY timestamp DESC LIMIT 1",
                                     (reference,)).fetchone()
        return _transaction_row(row) if row else None

    def transactions(self, account: str, *, kind: str | None = None, status: str | None = None, limit: int = 500,
                     since: datetime | None = None, newest_first: bool = True) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM transactions WHERE account = ?", [account]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        if status:
            query += " AND status = ?"
            params.append(status)
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(since.isoformat())
        query += f" ORDER BY timestamp {'DESC' if newest_first else 'ASC'}, rowid {'DESC' if newest_first else 'ASC'} LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (*params, limit)).fetchall()
        return [_transaction_row(r) for r in rows]

    def transaction_totals(self, account: str) -> dict[str, float]:
        """Completed cash flows by kind (deposits, withdrawals, trading, fees) for the wallet summary."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, SUM(amount) AS amount, SUM(fee) AS fee, COUNT(*) AS count FROM transactions"
                " WHERE account = ? AND status = 'completed' GROUP BY kind", (account,)).fetchall()
        totals = {"deposits": 0.0, "withdrawals": 0.0, "trading": 0.0, "fees": 0.0, "count": 0.0}
        for row in rows:
            if row["kind"] == "deposit":
                totals["deposits"] += float(row["amount"] or 0)
            elif row["kind"] == "withdrawal":
                totals["withdrawals"] += -float(row["amount"] or 0)
            else:
                totals["trading"] += float(row["amount"] or 0)
            totals["fees"] += float(row["fee"] or 0)
            totals["count"] += int(row["count"])
        return totals

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _transaction_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["metadata"] = json.loads(data.get("metadata") or "{}")
    except ValueError:
        data["metadata"] = {}
    return data
