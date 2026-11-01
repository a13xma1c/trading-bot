"""Trade analytics: FIFO trade matching, performance statistics, heuristic insights and the AI client."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from trading_bot.analytics import (
    AIInsightsClient,
    breakdown,
    build_report,
    closed_trades,
    heuristic_insights,
    performance_stats,
    pnl_curve,
)
from trading_bot.config import AIConfig

T0 = datetime(2024, 3, 4, 15, 0, tzinfo=UTC)  # a Monday


def fill(symbol: str, side: str, qty: float, price: float, minutes: int, strategy: str = "s",
         realized: float | None = None) -> dict[str, Any]:
    row = {"symbol": symbol, "side": side, "quantity": qty, "price": price, "strategy": strategy,
           "timestamp": (T0 + timedelta(minutes=minutes)).isoformat()}
    if realized is not None:
        row["realized_pnl"] = realized
    return row


def round_trips(results: list[float], *, gap: int = 60) -> list[dict[str, Any]]:
    """One BUY/SELL pair per entry in ``results`` (the P&L of a 10-share trade entered at 100)."""
    fills = []
    for i, pnl in enumerate(results):
        start = i * gap * 2
        fills.append(fill("AAPL", "BUY", 10, 100.0, start))
        fills.append(fill("AAPL", "SELL", 10, 100.0 + pnl / 10, start + gap, realized=pnl))
    return fills


# ------------------------------------------------------------ trade matching


def test_closed_trades_fifo_matching() -> None:
    fills = [
        fill("AAPL", "BUY", 10, 100.0, 0),
        fill("AAPL", "BUY", 10, 110.0, 10),
        fill("MSFT", "BUY", 5, 200.0, 15),
        fill("AAPL", "SELL", 15, 120.0, 30),            # 10 @ 100 + 5 @ 110 -> entry 103.33
        fill("MSFT", "SELL", 5, 190.0, 40, realized=-50.0),
        fill("AAPL", "SELL", 5, 90.0, 50),              # remaining 5 @ 110
        fill("TSLA", "SELL", 1, 10.0, 60),              # no known entry: ignored
    ]
    trades = closed_trades(fills)
    assert [(t.symbol, t.quantity) for t in trades] == [("AAPL", 15), ("MSFT", 5), ("AAPL", 5)]
    first = trades[0]
    assert first.entry_price == pytest.approx((10 * 100 + 5 * 110) / 15)
    assert first.pnl == pytest.approx(15 * 120 - (10 * 100 + 5 * 110))
    assert first.entry_time == fills[0]["timestamp"] and first.holding_minutes == 30
    assert first.return_pct == pytest.approx((120 / first.entry_price - 1) * 100)
    assert trades[1].pnl == -50.0  # the broker's figure wins when present
    assert trades[1].entry_price == pytest.approx(200.0)
    assert trades[2].pnl == pytest.approx(5 * (90 - 110)) and trades[2].entry_time == fills[1]["timestamp"]
    assert closed_trades([]) == []


def test_broker_pnl_keeps_return_consistent() -> None:
    """Average-cost brokers report a P&L that differs from FIFO lots; return % must agree with it."""
    fills = [fill("AAPL", "BUY", 10, 100.0, 0), fill("AAPL", "BUY", 10, 200.0, 10),
             fill("AAPL", "SELL", 10, 180.0, 20, realized=300.0)]  # avg cost 150 -> +30 * 10
    trade = closed_trades(fills)[0]
    assert trade.pnl == 300.0 and trade.entry_price == pytest.approx(150.0)
    assert trade.return_pct == pytest.approx(20.0)
    assert (trade.pnl > 0) == (trade.return_pct > 0)


# -------------------------------------------------------------- statistics


def test_performance_stats_and_curve() -> None:
    assert performance_stats([]).trades == 0 and performance_stats([]).profit_factor is None
    trades = closed_trades(round_trips([60, -30, 40, -20, 35, -10, -10]))
    stats = performance_stats(trades)
    assert stats.trades == 7 and stats.wins == 3 and stats.losses == 4
    assert stats.total_pnl == pytest.approx(65)
    assert stats.win_rate_pct == pytest.approx(300 / 7)
    assert stats.profit_factor == pytest.approx(135 / 70)
    assert stats.avg_win == pytest.approx(45) and stats.avg_loss == pytest.approx(-17.5)
    assert stats.expectancy == pytest.approx(65 / 7)
    assert stats.largest_win == 60 and stats.largest_loss == -30
    assert stats.max_drawdown == pytest.approx(-30)  # 60 -> 30 (-30); 100 -> 80 -> 115 -> 105 -> 95 (-20)
    assert stats.avg_holding_minutes == 60
    assert stats.current_streak == -2 and stats.longest_win_streak == 1 and stats.longest_loss_streak == 2
    assert stats.first_trade == trades[0].exit_time and stats.last_trade == trades[-1].exit_time
    as_dict = stats.to_dict()
    assert as_dict["profit_factor"] == round(135 / 70, 2) and as_dict["total_pnl"] == 65.0

    curve = pnl_curve(trades)
    assert [p["cumulative"] for p in curve] == [60, 30, 70, 50, 85, 75, 65]
    assert curve[0]["time"] == trades[0].exit_time and curve[1]["pnl"] == -30

    only_wins = performance_stats(closed_trades(round_trips([10, 20])))
    assert only_wins.profit_factor == math.inf and only_wins.to_dict()["profit_factor"] is None
    assert only_wins.max_drawdown == 0 and only_wins.avg_loss == 0


def test_breakdowns() -> None:
    fills = [fill("AAPL", "BUY", 1, 100, 0, "alpha"), fill("AAPL", "SELL", 1, 110, 30, "alpha"),
             fill("MSFT", "BUY", 1, 100, 60, "beta"), fill("MSFT", "SELL", 1, 95, 90, "beta"),
             fill("AAPL", "BUY", 1, 100, 24 * 60, "alpha"), fill("AAPL", "SELL", 1, 97, 24 * 60 + 30, "alpha")]
    trades = closed_trades(fills)
    by_strategy = breakdown(trades, "strategy")
    assert [r["name"] for r in by_strategy] == ["alpha", "beta"]  # sorted by total P&L
    assert by_strategy[0] == {"name": "alpha", "trades": 2, "wins": 1, "total_pnl": 7.0, "win_rate_pct": 50.0,
                              "avg_return_pct": 3.5, "profit_factor": round(10 / 3, 2)}
    assert by_strategy[1]["profit_factor"] == 0.0  # no wins
    assert breakdown(closed_trades(round_trips([5, 5])), "strategy")[0]["profit_factor"] is None  # no losses: infinite
    assert [r["name"] for r in breakdown(trades, "symbol")] == ["AAPL", "MSFT"]
    weekdays = breakdown(trades, "weekday")
    assert {r["name"] for r in weekdays} <= {"Monday", "Tuesday"} and sum(r["trades"] for r in weekdays) == 3
    hours = breakdown(trades, "hour")
    assert all(len(r["name"]) == 5 and r["name"].endswith(":00") for r in hours)
    assert breakdown([], "strategy") == []
    nameless = closed_trades([fill("X", "BUY", 1, 1, 0, ""), fill("X", "SELL", 1, 2, 1, "")])
    assert breakdown(nameless, "strategy")[0]["name"] == "(none)"


# ---------------------------------------------------------------- insights


def titles(insights: list[Any]) -> list[str]:
    return [i.title for i in insights]


def test_heuristic_insights() -> None:
    empty = heuristic_insights(performance_stats([]), [], [], [])
    assert [i.severity for i in empty] == ["info"] and "No closed trades" in empty[0].title

    few = closed_trades(round_trips([10, 20, 30]))
    stats = performance_stats(few)
    out = heuristic_insights(stats, breakdown(few, "strategy"), breakdown(few, "symbol"), breakdown(few, "hour"))
    assert any(t.startswith("Small sample") for t in titles(out))
    assert not any("profit factor" in t.lower() for t in titles(out))  # infinite PF (no losses) is not reported

    losing = closed_trades(round_trips([-50, 10, -60, 10, -70, 10, 10, -80, -90, -100, -110, -120]))
    stats = performance_stats(losing)
    out = heuristic_insights(stats, breakdown(losing, "strategy"), breakdown(losing, "symbol"), breakdown(losing, "hour"),
                             open_positions=2)
    got = titles(out)
    assert any(t.startswith("Losing edge") for t in got)
    assert any(t.startswith("Low win rate") for t in got)
    assert "Losses are larger than wins" in got
    assert any(t.startswith("Longest losing streak: 5") for t in got)
    assert any(t.startswith("Currently 5 losses in a row") for t in got)
    assert any("open position(s) during a losing streak" in t for t in got)
    assert all(i.severity in ("good", "info", "warning") for i in out)

    balanced = closed_trades(round_trips([20, -10] * 10, gap=30))
    stats = performance_stats(balanced)
    out = heuristic_insights(stats, breakdown(balanced, "strategy"), breakdown(balanced, "symbol"), breakdown(balanced, "hour"))
    assert any("Healthy profit factor" in t for t in titles(out))
    assert any("contributes most of the profit" in t for t in titles(out))


def test_strategy_symbol_and_hour_insights() -> None:
    fills: list[dict[str, Any]] = []
    for i in range(6):  # "good" strategy wins on AAPL, "bad" loses on TSLA, all closed at the same hour
        start = i * 600
        fills += [fill("AAPL", "BUY", 1, 100, start, "good"), fill("AAPL", "SELL", 1, 105, start + 30, "good"),
                  fill("TSLA", "BUY", 1, 100, start + 60, "bad"), fill("TSLA", "SELL", 1, 93, start + 90, "bad")]
    trades = closed_trades(fills)
    report = build_report("bob", fills, open_positions=0)
    got = titles(report.insights)
    assert "'bad' is the weakest strategy" in got and "'good' contributes most of the profit" in got
    assert "TSLA drags performance" in got
    assert report.by_strategy[0]["name"] == "good" and report.by_symbol[-1]["name"] == "TSLA"
    data = report.to_dict()
    assert data["account"] == "bob" and len(data["trades"]) == len(trades) == 12 and len(data["pnl_curve"]) == 12
    assert data["stats"]["trades"] == 12 and data["insights"][0]["severity"] in ("good", "info", "warning")
    assert build_report("bob", fills).to_dict(include_trades=False)["trades"] == []
    context = report.ai_context()
    assert set(context) == {"stats", "by_strategy", "by_symbol", "by_hour", "by_weekday", "recent_trades",
                            "heuristic_observations"}
    assert len(context["recent_trades"]) == 12 and "'bad' is the weakest strategy" in context["heuristic_observations"]
    json.dumps(context)  # serialisable as sent to the model


# --------------------------------------------------------------------- AI


def test_ai_client_roundtrip_and_errors() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("authorization") != "Bearer sk-test":
            return httpx.Response(401, json={"error": "bad key"})
        payload = json.loads(request.content)
        if payload["model"] == "broken":
            return httpx.Response(200, json={"unexpected": True})
        if payload["model"] == "down":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"model": payload["model"], "usage": {"total_tokens": 42},
                                         "choices": [{"message": {"role": "assistant", "content": "  Verdict: fine.  "}}]})

    cfg = AIConfig(enabled=True, base_url="https://llm.example/v1/", api_key="sk-test", model="test-model")
    client = AIInsightsClient(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.configured
    result = client.analyze({"stats": {"trades": 3}})
    assert result == {"text": "Verdict: fine.", "model": "test-model", "usage": {"total_tokens": 42}}
    request = seen[-1]
    assert str(request.url) == "https://llm.example/v1/chat/completions"
    body = json.loads(request.content)
    assert body["messages"][1]["role"] == "user" and '"trades": 3' in body["messages"][1]["content"]
    assert "past performance" in body["messages"][1]["content"]

    cfg.model = "broken"
    with pytest.raises(RuntimeError, match="unexpected response"):
        client.analyze({})
    cfg.model = "down"
    with pytest.raises(RuntimeError, match="unreachable"):
        client.analyze({})
    cfg.model = "test-model"
    cfg.api_key = "wrong"
    with pytest.raises(RuntimeError, match="HTTP 401"):
        client.analyze({})

    assert AIInsightsClient(AIConfig(enabled=False, api_key="x")).configured is False
    assert AIInsightsClient(AIConfig(enabled=True, api_key="")).configured is False
    assert AIInsightsClient(AIConfig(enabled=True, api_key="", base_url="http://localhost:11434/v1")).configured is True
    with pytest.raises(RuntimeError, match="not configured"):
        AIInsightsClient(AIConfig()).analyze({})
