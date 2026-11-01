"""Local symbol directory used for ticker suggestions and display names.

A bundled CSV (``symbols.csv``: major US stocks and ETFs) answers look-ups
instantly and offline, so typing in the watchlist never spends the data
provider's request budget.  Users can merge their own list through
``data.symbols_file`` (``symbol,name[,type]``).  Provider search remains
available as an explicit fallback (see the ``/api/symbols/search`` endpoint).
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BUNDLED_FILE = Path(__file__).parent / "symbols.csv"


@dataclass(frozen=True)
class SymbolEntry:
    symbol: str
    name: str
    type: str = "stock"
    source: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SymbolDirectory:
    def __init__(self, entries: list[SymbolEntry]) -> None:
        self._entries: dict[str, SymbolEntry] = {}
        for entry in entries:
            self._entries[entry.symbol.upper()] = entry

    # ------------------------------------------------------------- loading
    @classmethod
    def load(cls, extra_file: str | os.PathLike[str] | None = None) -> SymbolDirectory:
        entries = _read_csv(BUNDLED_FILE)
        if extra_file:
            path = Path(extra_file)
            if path.exists():
                entries.extend(_read_csv(path))
            else:
                logger.warning("Symbols file %s not found; using the bundled directory only", path)
        return cls(entries)

    def __len__(self) -> int:
        return len(self._entries)

    # --------------------------------------------------------------- lookup
    def get(self, symbol: str) -> SymbolEntry | None:
        return self._entries.get(symbol.strip().upper())

    def name(self, symbol: str) -> str | None:
        entry = self.get(symbol)
        return entry.name if entry else None

    def search(self, query: str, limit: int = 8) -> list[SymbolEntry]:
        """Rank matches: exact symbol, symbol prefix, name word start, then substrings."""
        q = query.strip().lower()
        if not q:
            return []
        scored: list[tuple[int, str, SymbolEntry]] = []
        for entry in self._entries.values():
            symbol = entry.symbol.lower()
            name = entry.name.lower()
            if symbol == q:
                score = 0
            elif symbol.startswith(q):
                score = 1
            elif name.startswith(q):
                score = 2
            elif any(word.startswith(q) for word in name.replace("(", " ").replace(".", " ").split()):
                score = 3
            elif q in symbol:
                score = 4
            elif q in name:
                score = 5
            else:
                continue
            scored.append((score, symbol, entry))
        scored.sort(key=lambda item: (item[0], len(item[1]), item[1]))
        return [entry for _, _, entry in scored[:limit]]


def _read_csv(path: Path) -> list[SymbolEntry]:
    entries: list[SymbolEntry] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return entries
        fields = {name.lower().strip(): name for name in reader.fieldnames}
        if "symbol" not in fields or "name" not in fields:
            raise ValueError(f"{path} needs 'symbol' and 'name' columns")
        for row in reader:
            symbol = (row.get(fields["symbol"]) or "").strip().upper()
            name = (row.get(fields["name"]) or "").strip()
            if not symbol or not name:
                continue
            kind = (row.get(fields["type"]) or "stock").strip().lower() if "type" in fields else "stock"
            entries.append(SymbolEntry(symbol, name, kind))
    return entries
