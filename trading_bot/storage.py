"""SQLite persistence for signals and fills (history survives restarts)."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime
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
    executed INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS signals_dedupe ON signals (strategy, symbol, action, bar_time);
CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    strategy TEXT,
    realized_pnl REAL NOT NULL DEFAULT 0
);
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
            self._conn.commit()

    # ------------------------------------------------------------- signals
    def record_signal(self, signal: Signal, executed: bool = False) -> bool:
        """Store ``signal``; returns False if an identical signal was already stored."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO signals (id, created_at, bar_time, symbol, action, strategy, price, reason, executed)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (signal.id, signal.created_at.isoformat(), signal.bar_time.isoformat(), signal.symbol,
                     signal.action, signal.strategy, signal.price, signal.reason, int(executed)),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def has_signal(self, signal: Signal) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM signals WHERE strategy=? AND symbol=? AND action=? AND bar_time=?",
                (signal.strategy, signal.symbol, signal.action, signal.bar_time.isoformat()),
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
                created_at=datetime.fromisoformat(row["created_at"]), id=row["id"],
            ))
        return out

    # --------------------------------------------------------------- fills
    def record_fill(self, fill: Fill) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO fills (id, timestamp, symbol, side, quantity, price, strategy, realized_pnl)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fill.id, fill.timestamp.isoformat(), fill.symbol, fill.side, fill.quantity, fill.price,
                 fill.strategy, fill.realized_pnl),
            )
            self._conn.commit()

    def recent_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM fills ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
