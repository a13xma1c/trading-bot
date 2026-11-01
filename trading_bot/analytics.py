"""Trade history analytics: closed trades, P&L curve, performance statistics and insights.

Insights come in two flavours: deterministic *heuristic* insights computed from
the statistics (always available) and, when an LLM provider is configured
(``ai`` section), a generated narrative analysis of the same numbers.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import AIConfig

logger = logging.getLogger(__name__)


@dataclass
class ClosedTrade:
    symbol: str
    strategy: str
    entry_time: str
    exit_time: str
    quantity: float
    entry_price: float
    exit_price: float
    pnl: float
    return_pct: float
    holding_minutes: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Lot:
    time: datetime
    quantity: float
    price: float


def closed_trades(fills: list[dict[str, Any]]) -> list[ClosedTrade]:
    """Match SELL fills against earlier BUY lots (FIFO) per symbol."""
    lots: dict[str, list[Lot]] = defaultdict(list)
    trades: list[ClosedTrade] = []
    for fill in fills:
        symbol = fill["symbol"]
        when = datetime.fromisoformat(fill["timestamp"])
        qty = float(fill["quantity"])
        price = float(fill["price"])
        if fill["side"] == "BUY":
            lots[symbol].append(Lot(when, qty, price))
            continue
        remaining = qty
        cost = 0.0
        matched = 0.0
        entry_time: datetime | None = None
        while remaining > 1e-9 and lots[symbol]:
            lot = lots[symbol][0]
            take = min(lot.quantity, remaining)
            cost += take * lot.price
            matched += take
            remaining -= take
            lot.quantity -= take
            entry_time = entry_time or lot.time
            if lot.quantity <= 1e-9:
                lots[symbol].pop(0)
        if matched <= 0:
            continue  # sell without a known entry (e.g. history before the account was tracked)
        entry_price = cost / matched
        reported = fill.get("realized_pnl")
        if reported is not None and float(reported) != 0.0:
            # The broker's figure is authoritative (it may use average-cost accounting); derive the
            # effective entry price from it so P&L and return % always agree.
            pnl = float(reported)
            entry_price = price - pnl / matched
        else:
            pnl = (price - entry_price) * matched
        assert entry_time is not None
        trades.append(ClosedTrade(
            symbol=symbol, strategy=fill.get("strategy") or "", entry_time=entry_time.isoformat(),
            exit_time=when.isoformat(), quantity=matched, entry_price=entry_price, exit_price=price, pnl=pnl,
            return_pct=(price / entry_price - 1.0) * 100.0 if entry_price else 0.0,
            holding_minutes=(when - entry_time).total_seconds() / 60.0,
        ))
    return trades


def pnl_curve(trades: list[ClosedTrade]) -> list[dict[str, Any]]:
    """Cumulative realized P&L after each closed trade."""
    total = 0.0
    points = []
    for trade in trades:
        total += trade.pnl
        points.append({"time": trade.exit_time, "pnl": round(trade.pnl, 2), "cumulative": round(total, 2)})
    return points


@dataclass
class PerformanceStats:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float | None = None
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    max_drawdown: float = 0.0
    avg_holding_minutes: float = 0.0
    avg_return_pct: float = 0.0
    current_streak: int = 0      # >0 consecutive wins, <0 consecutive losses
    longest_win_streak: int = 0
    longest_loss_streak: int = 0
    first_trade: str | None = None
    last_trade: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("total_pnl", "avg_win", "avg_loss", "expectancy", "largest_win", "largest_loss", "max_drawdown",
                    "avg_holding_minutes", "avg_return_pct", "win_rate_pct"):
            data[key] = round(data[key], 2)
        if self.profit_factor is not None:
            data["profit_factor"] = round(self.profit_factor, 2) if math.isfinite(self.profit_factor) else None
        return data


def performance_stats(trades: list[ClosedTrade]) -> PerformanceStats:
    stats = PerformanceStats(trades=len(trades))
    if not trades:
        return stats
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    stats.wins, stats.losses = len(wins), len(losses)
    stats.total_pnl = sum(t.pnl for t in trades)
    stats.win_rate_pct = 100.0 * len(wins) / len(trades)
    gross_profit, gross_loss = sum(wins), -sum(losses)
    stats.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (math.inf if gross_profit > 0 else None)
    stats.avg_win = gross_profit / len(wins) if wins else 0.0
    stats.avg_loss = -gross_loss / len(losses) if losses else 0.0
    stats.expectancy = stats.total_pnl / len(trades)
    stats.largest_win = max(wins) if wins else 0.0
    stats.largest_loss = min(losses) if losses else 0.0
    stats.avg_holding_minutes = sum(t.holding_minutes for t in trades) / len(trades)
    stats.avg_return_pct = sum(t.return_pct for t in trades) / len(trades)
    peak = cumulative = 0.0
    drawdown = 0.0
    for t in trades:
        cumulative += t.pnl
        peak = max(peak, cumulative)
        drawdown = min(drawdown, cumulative - peak)
    stats.max_drawdown = drawdown
    streak = best_win = best_loss = 0
    for t in trades:
        if t.pnl > 0:
            streak = streak + 1 if streak > 0 else 1
            best_win = max(best_win, streak)
        elif t.pnl < 0:
            streak = streak - 1 if streak < 0 else -1
            best_loss = min(best_loss, streak)
    stats.current_streak = streak
    stats.longest_win_streak = best_win
    stats.longest_loss_streak = -best_loss
    stats.first_trade, stats.last_trade = trades[0].exit_time, trades[-1].exit_time
    return stats


def breakdown(trades: list[ClosedTrade], key: str) -> list[dict[str, Any]]:
    """Per-group statistics (``key``: strategy | symbol | hour | weekday)."""
    groups: dict[str, list[ClosedTrade]] = defaultdict(list)
    for t in trades:
        if key == "hour":
            name = f"{datetime.fromisoformat(t.exit_time).astimezone().hour:02d}:00"
        elif key == "weekday":
            name = datetime.fromisoformat(t.exit_time).astimezone().strftime("%A")
        else:
            name = getattr(t, key) or "(none)"
        groups[name].append(t)
    rows: list[dict[str, Any]] = []
    for name, items in groups.items():
        s = performance_stats(items)
        rows.append({"name": name, "trades": s.trades, "wins": s.wins, "total_pnl": round(s.total_pnl, 2),
                     "win_rate_pct": round(s.win_rate_pct, 1), "avg_return_pct": round(s.avg_return_pct, 2),
                     "profit_factor": None if s.profit_factor is None or not math.isfinite(s.profit_factor)
                     else round(s.profit_factor, 2)})
    rows.sort(key=lambda r: float(r["total_pnl"]), reverse=True)
    return rows


@dataclass
class Insight:
    severity: str   # good | info | warning
    title: str
    detail: str
    metric: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def heuristic_insights(stats: PerformanceStats, by_strategy: list[dict[str, Any]], by_symbol: list[dict[str, Any]],
                       by_hour: list[dict[str, Any]], open_positions: int = 0) -> list[Insight]:
    """Rule-based observations; the baseline of the insights panel without an LLM."""
    out: list[Insight] = []
    if stats.trades == 0:
        out.append(Insight("info", "No closed trades yet",
                           "Start the engine with auto-trading enabled (or run a backtest) to collect trade history; "
                           "insights appear once trades have been closed.", "trades"))
        return out
    if stats.trades < 10:
        out.append(Insight("info", "Small sample", f"Only {stats.trades} closed trades so far - treat every statistic "
                           "below as preliminary until there are at least 30.", "trades"))
    if stats.profit_factor is not None and math.isfinite(stats.profit_factor):
        if stats.profit_factor >= 1.5:
            out.append(Insight("good", f"Healthy profit factor ({stats.profit_factor:.2f})",
                               "Gross profits exceed gross losses by a comfortable margin.", "profit_factor"))
        elif stats.profit_factor < 1.0:
            out.append(Insight("warning", f"Losing edge (profit factor {stats.profit_factor:.2f})",
                               "Gross losses exceed gross profits: the current strategy mix loses money. Review the "
                               "worst strategies below before trading more size.", "profit_factor"))
    if stats.win_rate_pct < 35 and stats.avg_win < 2 * abs(stats.avg_loss):
        out.append(Insight("warning", f"Low win rate ({stats.win_rate_pct:.0f}%) without large winners",
                           "A low hit rate only works with winners much larger than losers; consider wider take-profit "
                           "targets or tighter entries.", "win_rate_pct"))
    if stats.avg_loss and abs(stats.avg_loss) > 1.5 * max(stats.avg_win, 1e-9):
        out.append(Insight("warning", "Losses are larger than wins",
                           f"Average loss {stats.avg_loss:.2f} vs average win {stats.avg_win:.2f}: add or tighten stop "
                           "losses so single trades cannot undo several winners.", "avg_loss"))
    if stats.max_drawdown < 0 and stats.total_pnl > 0 and abs(stats.max_drawdown) > stats.total_pnl:
        out.append(Insight("warning", "Drawdown exceeds total profit",
                           f"Max drawdown {stats.max_drawdown:.2f} is larger than the total P&L {stats.total_pnl:.2f}; "
                           "returns are being earned with a lot of volatility.", "max_drawdown"))
    if stats.longest_loss_streak >= 5:
        out.append(Insight("info", f"Longest losing streak: {stats.longest_loss_streak}",
                           "Streaks of this length are expected for low win rates; make sure position sizing survives "
                           "them.", "longest_loss_streak"))
    if stats.current_streak <= -3:
        out.append(Insight("warning", f"Currently {abs(stats.current_streak)} losses in a row",
                           "Consider pausing auto-trading until the market regime that hurt these strategies changes.",
                           "current_streak"))
    if stats.avg_holding_minutes < 15 and stats.trades >= 10:
        out.append(Insight("info", "Very short holding times",
                           f"Trades are closed after {stats.avg_holding_minutes:.0f} minutes on average; intraday noise "
                           "and commissions dominate such outcomes. Longer cooldowns may help.", "avg_holding_minutes"))
    if by_strategy:
        best, worst = by_strategy[0], by_strategy[-1]
        if len(by_strategy) > 1 and worst["total_pnl"] < 0 and worst["trades"] >= 5:
            out.append(Insight("warning", f"'{worst['name']}' is the weakest strategy",
                               f"{worst['trades']} trades, {worst['win_rate_pct']:.0f}% win rate, P&L {worst['total_pnl']:.2f}. "
                               "Disable it or backtest a variant before it costs more.", "strategy"))
        if best["total_pnl"] > 0 and best["trades"] >= 5:
            out.append(Insight("good", f"'{best['name']}' contributes most of the profit",
                               f"{best['trades']} trades, {best['win_rate_pct']:.0f}% win rate, P&L {best['total_pnl']:.2f}.",
                               "strategy"))
    if by_symbol:
        worst = by_symbol[-1]
        if worst["total_pnl"] < 0 and worst["trades"] >= 5 and len(by_symbol) > 1:
            out.append(Insight("info", f"{worst['name']} drags performance",
                               f"{worst['trades']} trades with P&L {worst['total_pnl']:.2f}; the strategies may not fit "
                               "this symbol's behaviour.", "symbol"))
    if len(by_hour) >= 3:
        worst_hour = min(by_hour, key=lambda r: float(r["total_pnl"]))
        if worst_hour["total_pnl"] < 0 and worst_hour["trades"] >= 3:
            out.append(Insight("info", f"Weak hour: {worst_hour['name']}",
                               f"Trades closed around {worst_hour['name']} lost {abs(worst_hour['total_pnl']):.2f} in total; "
                               "session timing (e.g. the open) may be hurting.", "hour"))
    if open_positions and stats.current_streak <= -2:
        out.append(Insight("info", f"{open_positions} open position(s) during a losing streak",
                           "Check that stops are in place for the open positions.", "positions"))
    if not out:
        out.append(Insight("good", "Nothing stands out", "The statistics look balanced; keep collecting trades.", ""))
    return out


class AIInsightsClient:
    """Narrative analysis through an OpenAI-compatible chat completions endpoint."""

    def __init__(self, cfg: AIConfig, client: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self._client = client

    @property
    def configured(self) -> bool:
        """Enabled with a model and endpoint; an API key is required unless the endpoint is local (Ollama, LM Studio)."""
        if not (self.cfg.enabled and self.cfg.base_url and self.cfg.model):
            return False
        return bool(self.cfg.api_key) or self._is_local(self.cfg.base_url)

    @staticmethod
    def _is_local(url: str) -> bool:
        host = urlparse(url).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1", "host.docker.internal")

    def analyze(self, context: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("AI provider is not configured (ai.enabled, ai.api_key, ai.model)")
        prompt = (
            "You are an experienced quantitative trading coach. Analyse the following paper-trading statistics of a "
            "retail trading bot and write a concise, concrete assessment for the trader: 1) overall verdict, "
            "2) the two or three most important strengths/weaknesses with the numbers that support them, "
            "3) specific, actionable suggestions (strategy changes, risk management, what to backtest next). "
            "Use short paragraphs and bullet points; do not invent data that is not in the statistics; "
            "remind the reader that past performance does not predict future results.\n\nStatistics (JSON):\n"
            + json.dumps(context, indent=2, default=str)
        )
        payload = {"model": self.cfg.model, "temperature": 0.3,
                   "messages": [{"role": "system", "content": "You analyse trading performance data."},
                                {"role": "user", "content": prompt}]}
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        client = self._client or httpx.Client(timeout=self.cfg.timeout_seconds)
        try:
            response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"AI provider unreachable: {exc}") from exc
        finally:
            if self._client is None:
                client.close()
        if response.status_code >= 400:
            raise RuntimeError(f"AI provider returned HTTP {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"].strip()
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("AI provider returned an unexpected response") from exc
        usage = data.get("usage") or {}
        return {"text": text, "model": data.get("model", self.cfg.model), "usage": usage}


@dataclass
class AnalyticsReport:
    account: str
    stats: PerformanceStats
    trades: list[ClosedTrade]
    curve: list[dict[str, Any]]
    by_strategy: list[dict[str, Any]]
    by_symbol: list[dict[str, Any]]
    by_hour: list[dict[str, Any]]
    by_weekday: list[dict[str, Any]]
    insights: list[Insight] = field(default_factory=list)

    def to_dict(self, include_trades: bool = True) -> dict[str, Any]:
        return {
            "account": self.account, "stats": self.stats.to_dict(),
            "trades": [t.to_dict() for t in self.trades] if include_trades else [],
            "pnl_curve": self.curve, "by_strategy": self.by_strategy, "by_symbol": self.by_symbol,
            "by_hour": self.by_hour, "by_weekday": self.by_weekday, "insights": [i.to_dict() for i in self.insights],
        }

    def ai_context(self) -> dict[str, Any]:
        """Compact summary sent to the LLM (no raw fills, no personal data)."""
        return {"stats": self.stats.to_dict(), "by_strategy": self.by_strategy[:8], "by_symbol": self.by_symbol[:8],
                "by_hour": self.by_hour, "by_weekday": self.by_weekday, "recent_trades": [t.to_dict() for t in self.trades[-15:]],
                "heuristic_observations": [i.title for i in self.insights]}


def build_report(account: str, fills: list[dict[str, Any]], open_positions: int = 0) -> AnalyticsReport:
    trades = closed_trades(fills)
    stats = performance_stats(trades)
    by_strategy = breakdown(trades, "strategy")
    by_symbol = breakdown(trades, "symbol")
    by_hour = breakdown(trades, "hour")
    by_weekday = breakdown(trades, "weekday")
    insights = heuristic_insights(stats, by_strategy, by_symbol, by_hour, open_positions)
    return AnalyticsReport(account, stats, trades, pnl_curve(trades), by_strategy, by_symbol, by_hour, by_weekday, insights)
