# Trading Bot

A trading-signal bot with a web UI. It pulls real-time market data from a
public API, evaluates **custom, user-defined strategies** on every new bar,
shows the result on a **candlestick chart**, and **notifies you when a trade
should be executed** (browser notification, in-app snackbar, sound, log,
Slack/Discord-style webhook, e-mail). An optional paper broker executes the
signals so you can track a strategy's P&L with zero risk.

```
┌────┬─────────────────────────────────────────────────────────────────────┐
│ ▣  │ Trading Bot   ● Engine running   Market open · refresh every 2 min  │
│Dash│                                             [⚡ Scan now] [■ Stop]  │
│    ├────────────────┬────────────────────────────────────────────────────┤
│ ▤  │ Watchlist      │ AAPL  [Overlay strategy ▾] [Reset view] [Refresh]  │
│Stra│ AAPL  220.17   │  candlestick chart + volume + indicator panes      │
│    │ MSFT  408.23   │  + overlays + buy/sell markers (canvas)            │
│ ◫  ├────────────────┴────────────────────────────────────────────────────┤
│Pos │ Signals  (time · symbol · BUY/SELL · price · strategy · executed)   │
│ ⚙  │                                                                     │
└────┴─────────────────────────────────────────────────────────────────────┘
```

The UI follows **Material Design 3** (navigation rail, top app bar, cards,
tonal surfaces, outlined text fields, switches, chips, data tables, dialogs,
snackbar) and is implemented without a front-end framework, so it has no
build step and works offline. The backend is FastAPI; live updates arrive
over a WebSocket.

## Features

* **Live data** from Alpha Vantage or Twelve Data (free API keys), with a
  request budget that is paced so the free tier lasts the whole day, an on-disk
  counter that survives restarts, and stale-data fallback when the quota is gone.
  A `demo` provider (deterministic synthetic prices) and a `csv` provider
  (your own files) work fully offline.
* **Web UI with a candlestick chart** (canvas): wheel zoom, drag pan, crosshair
  with OHLCV read-out, volume pane, moving averages/bands drawn over the
  candles, oscillators (RSI, MACD, …) in their own panes, buy/sell markers for
  live signals, strategy previews and backtests. Runs in any modern browser.
* **Custom strategies** written in a small, safe rule language, e.g.
  `crossover(ema(12), ema(26)) and rsi(14) < 70`, edited, validated and
  backtested inside the GUI. Power users can drop Python `Strategy` subclasses
  into `strategies/`.
* **Notifications**: browser notification, in-app snackbar, sound, rotating log
  file, JSON webhook (Slack/Discord/Mattermost compatible) and SMTP e-mail.
  Signals are de-duplicated per bar and persisted in SQLite, so restarting never
  re-sends old alerts.
* **Paper trading** with stop-loss / take-profit monitoring, position sizing and
  a `Broker` interface for plugging in a real broker.
* **Backtester** (next-open fills, intrabar stop/target, commissions) with
  equity curve, trade list and summary statistics.
* **REST + WebSocket API** (`/api/docs`), optional API token, headless mode
  for servers (`trading-bot run`) plus `scan`, `backtest` and `check` commands.
* **Test suite**: ~200 fully typed tests covering indicators, the rule language
  (including sandbox escapes), providers (recorded API payloads), rate limiting,
  engine, broker, backtester, notifications, config migration, CLI, the HTTP /
  WebSocket API and end-to-end browser tests (Playwright).

## Installation

Python 3.12 or newer.

```bash
# with uv (recommended)
uv sync --extra dev
uv run trading-bot            # starts the web server and opens http://127.0.0.1:8080/

# or with pip
python -m venv .venv && source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements-dev.txt
python -m trading_bot
```

The server binds to `127.0.0.1:8080` by default. To reach it from other
machines set `web.host` (e.g. `0.0.0.0`) **and** an API token (`web.token` or
`TRADING_BOT_WEB_TOKEN`); the UI asks for the token once and stores it in the
browser. Put a TLS-terminating reverse proxy in front of it for anything but a
private network.

### API key

1. Get a free key from [Alpha Vantage](https://www.alphavantage.co/support/#api-key)
   (25 requests/day, 5/minute) or [Twelve Data](https://twelvedata.com/pricing)
   (800 requests/day, 8/minute — a much better fit for intraday scanning).
2. Provide it through the environment (preferred, never ends up in git):
   ```bash
   export TRADING_BOT_API_KEY=your_key
   ```
   or put it in `config.json` under `data.api_key`, or enter it in *Settings*.

> **Budget reality check.** Free tiers are small, so the engine *paces* requests:
> the daily budget is spread evenly over the trading session (09:30–16:00
> New York by default, see `market_hours`) and nothing is requested while the
> market is closed. The toolbar always shows the resulting refresh interval,
> e.g. with Twelve Data (800/day) and 5 symbols each symbol refreshes every
> ~2.5 minutes; with Alpha Vantage (25/day) only every ~80 minutes. Use
> **Scan now** or the chart's **Refresh** button to fetch immediately regardless
> of pacing (the hard per-minute/per-day limits still apply). For 24-hour
> markets such as crypto disable market hours in *Settings*.

### Try it without a key

```bash
uv run trading-bot --config config.demo.json
```

The demo provider generates realistic, deterministic bars, so every part of the
application can be explored offline (and the test-suite runs against it).

## Usage

| Command | What it does |
| --- | --- |
| `trading-bot` / `trading-bot serve [--host H] [--port P] [--no-browser]` | Web application (API + UI) |
| `trading-bot run` | Headless scanner; notifications via log / webhook / e-mail |
| `trading-bot scan` | One scan, print new signals as JSON, exit |
| `trading-bot backtest AAPL [--strategy NAME] [--bars 1000]` | Backtest strategies on one symbol |
| `trading-bot check` | Validate config and strategies |
| `--config path` | Use another config file (default `config.json`) |

Use `python -m trading_bot …` if the console script is not on your `PATH`.

### Web UI walk-through

* **Start** (top app bar) launches the scan loop. Every `scan_interval` seconds
  the engine refreshes each watchlist symbol (subject to the API budget pacing),
  evaluates all enabled strategies on the newest bar and raises signals.
  The chips show the engine state, market status / refresh interval and the
  remaining API budget.
* **Scan now** forces a refresh of every symbol; **Refresh** (above the chart)
  refreshes just the charted symbol. A symbol whose last fetch failed shows
  *error* in red in the watchlist (hover for the reason) and in the chart
  status line; cached data keeps being served meanwhile.
* **Dashboard** – watchlist (add with Enter, click a row to chart it, × to
  remove), candlestick chart with an *Overlay strategy* selector that draws a
  strategy's indicators and marks where it would have fired, and the signal
  list. New signals show a snackbar, an optional browser notification and a
  sound.
* **Strategies** – create / duplicate / delete strategies, edit rules and
  parameters, *Validate* (syntax check + evaluation on the charted data),
  *Show on chart*, *Backtest…* (stats, equity curve, trade list; trades can be
  marked on the chart). The switch in the list enables a strategy for live
  scanning.
* **Positions** – paper-broker account, open positions with P&L, stops and
  targets, fills; reset button.
* **Settings** – provider, API key, interval, budget, market hours,
  notifications, trading. Provider / API key changes need a server restart.

### HTTP API

Everything the UI does is available at `/api/*` (interactive docs at
`/api/docs`): status and engine control, watchlist, bars and overlays,
strategies (validate / save / enable / delete), backtests, signals, positions,
settings. `/ws` streams events (`bars`, `signal`, `scan`, `fetch_failed`,
`engine`, `positions`, `status`) as JSON. With a token configured, send
`Authorization: Bearer <token>` (or `?token=` for the WebSocket).

## Writing strategies

A strategy has a **buy rule** and an optional **sell rule**. Rules are boolean
expressions over the bar series `open, high, low, close, volume`, numeric
parameters you define (so you can tune them without editing the rule), and the
functions below.

```text
crossover(sma(fast), sma(slow))                       # parameters fast/slow
rsi(14) < 30 and close > ema(200)
close < bb_lower(20, 2) and volume > sma(20, volume) * 1.5
pct_change(close, 5) > 2 and bars_since(crossover(macd(), macd_signal())) < 3
```

| Function | Meaning |
| --- | --- |
| `sma(n, source=close)`, `ema(n, …)`, `wma(n, …)` | Moving averages |
| `rsi(n=14, source=close)` | Relative strength index (Wilder) |
| `macd(fast=12, slow=26, signal=9)`, `macd_signal(...)`, `macd_hist(...)` | MACD |
| `bb_upper(n=20, std=2)`, `bb_middle(...)`, `bb_lower(...)` | Bollinger bands |
| `atr(n=14)` | Average true range |
| `stoch_k(k=14, d=3)`, `stoch_d(...)` | Stochastic oscillator |
| `vwap()`, `obv()` | Volume-weighted average price, on-balance volume |
| `highest(source, n)`, `lowest(source, n)` | Rolling extremes |
| `prev(source, n=1)`, `change(source, n=1)`, `pct_change(source, n=1)` | Lookback helpers |
| `crossover(a, b)`, `crossunder(a, b)` | True on the bar where `a` crosses `b` |
| `bars_since(condition)` | Bars since the condition was last true |
| `abs(x)`, `min(a, b, …)`, `max(a, b, …)` | Element-wise math |

Operators: `and or not < <= > >= == != + - * / % **`. The language is parsed
with Python's `ast` module and evaluated by a whitelist interpreter: no
attribute access, indexing, imports, lambdas or builtins are reachable, so a
rule cannot execute arbitrary code.

Per strategy you can also set **symbols** (empty = whole watchlist),
**stop loss / take profit** percentages (used by the backtester and by the
paper broker's protective exits) and a **cooldown** (minimum bars between
two signals of the same kind).

### Python strategies

For logic that does not fit the rule language, create a file in `strategies/`:

```python
import pandas as pd
from trading_bot.strategy import Strategy
from trading_bot import indicators as ind

class Breakout(Strategy):
    name = "Breakout"
    stop_loss_pct = 3.0

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        buy = df["close"] > ind.highest(df["high"], 20).shift(1)
        sell = df["close"] < ind.lowest(df["low"], 10).shift(1)
        return pd.DataFrame({"buy": buy.fillna(False), "sell": sell.fillna(False)})
```

See `strategies/example_momentum.py`. Plugins appear in the GUI next to rule
strategies and can be enabled/disabled there.

## Notifications

| Channel | Configuration |
| --- | --- |
| Browser notification + in-app snackbar + sound | `notifications.desktop`, `notifications.sound` (web UI) |
| Log | always on (`bot_data/trading_bot.log`, rotating) |
| Webhook | `notifications.webhook_url` or `TRADING_BOT_WEBHOOK_URL`; POSTs `{"text": ..., "content": ..., "signal": {...}}` |
| E-mail | `notifications.email.*`; password may come from `TRADING_BOT_SMTP_PASSWORD` |

A signal is identified by *(strategy, symbol, action, bar time)*; the same
signal is never notified twice, even across restarts.

## Configuration reference (`config.json`)

```jsonc
{
  "watchlist": ["AAPL", "MSFT"],
  "scan_interval": 60,             // seconds between scan loops
  "data_dir": "bot_data",          // sqlite history, paper broker state, rate-limit counter, logs
  "strategies_file": "strategies.json",
  "plugins_dir": "strategies",     // Python plugin strategies
  "log_level": "INFO",
  "web": {"host": "127.0.0.1", "port": 8080, "token": "", "open_browser": true},
  "data": {
    "provider": "alphavantage",    // alphavantage | twelvedata | csv | demo
    "api_key": "",                 // or TRADING_BOT_API_KEY
    "interval": "5min",            // 1min 5min 15min 30min 60min 1day
    "bars": 300,                   // history per symbol (indicators need warm-up)
    "requests_per_minute": null,   // null = provider free-tier default
    "requests_per_day": null,
    "cache_ttl_seconds": 60,
    "csv_directory": "data",       // for the csv provider: <SYMBOL>.csv with time,open,high,low,close,volume
    "closed_bars_only": false      // true = ignore the still-forming intraday bar
  },
  "market_hours": {                // session used for budget pacing; polling pauses when closed
    "enabled": true,               // false = 24h markets (crypto/forex), pace over the whole day
    "timezone": "America/New_York",
    "open": "09:30",
    "close": "16:00",
    "weekdays": [0, 1, 2, 3, 4]    // Monday = 0
  },
  "notifications": { "desktop": true, "sound": true, "webhook_url": "", "email": { ... } },
  "trading": {
    "trading_enabled": false,      // execute signals through the (paper) broker
    "paper": true,
    "initial_cash": 100000,
    "position_size_pct": 0.1,      // fraction of equity per position
    "max_positions": 5
  }
}
```

Relative paths (`data_dir`, `strategies_file`, `csv_directory`) are resolved
relative to the config file, so a project directory can be moved or copied as a
whole. The previous flat layout (`api_key`, `rsi_period`, …) is migrated
automatically.

## Architecture

```
trading_bot/
  config.py          dataclass config, env overrides, legacy migration
  indicators.py      vectorised pandas indicators (TA-Lib conventions)
  strategy/
    expression.py    safe rule language (ast whitelist interpreter, memoised indicators)
    base.py          Signal, StrategyDefinition, Strategy, RuleStrategy
    registry.py      JSON store, defaults, Python plugin discovery
  data/
    providers.py     AlphaVantage, TwelveData, CSV, Demo (UTC OHLCV frames)
    ratelimit.py     persisted sliding-window limiter (per minute / per day)
    service.py       cache + budget-aware fetching with stale fallback
  engine.py          scan loop, de-duplication, protective exits, event bus
  notify.py          log / webhook / e-mail / callback channels
  broker.py          Broker interface + PaperBroker (persisted)
  storage.py         SQLite signal & fill history
  backtest.py        event-driven backtester
  app.py             composition root; __main__.py CLI
  web/
    server.py        FastAPI app: REST API, WebSocket broadcaster, static files
    charting.py      chart payloads (bars, overlays grouped into panes, markers)
    static/          Material Design single-page app (index.html, css/material.css,
                     js/app.js, js/chart.js, js/ui.js) - no build step
```

The engine runs on background threads and publishes events on an `EventBus`;
the web server forwards them to WebSocket clients, so the same engine powers
the web app and the headless runner.

## Development

```bash
uv sync --extra dev
uv run playwright install chromium  # once, for the browser tests
uv run pytest                 # full suite
uv run ruff check .           # lint
uv run mypy tests trading_bot # type check (tests are fully annotated and checked strictly)
```

### Test layers

| Layer | Where | What |
| --- | --- | --- |
| Unit | `tests/test_indicators.py`, `test_expression.py`, `test_strategy.py`, `test_backtest.py`, `test_broker_storage.py`, `test_notify_config.py`, `test_data_service.py`, `test_providers.py` | Pure logic; providers use recorded API payloads and a fake HTTP session |
| Integration | `tests/test_engine.py`, `tests/test_cli.py` | Scan loop, pacing, market hours, paper trading, CLI commands incl. `serve` |
| API | `tests/test_web_api.py` | Every REST endpoint, token auth and the WebSocket (FastAPI `TestClient`, no browser) |
| End-to-end | `tests/test_web_e2e.py` | Playwright + headless Chromium against a live server: layout/MD tokens, chart pixels, zoom/pan, watchlist, engine & notifications, editor, backtest dialog, settings, error states, token dialog |

Browser tests need a Chromium for Playwright: `uv run playwright install chromium`
(they are skipped with a message if it is missing). All widgets used by the
automated tests carry a stable HTML `id` (`startButton`, `watchlistTable`,
`buyRuleInput`, ...).

The tests are fully type-annotated; `mypy` checks them with
`disallow_untyped_defs` and the package with `check_untyped_defs`.

## Going live

`trading_enabled: true` routes signals to the **paper** broker. To trade real
money implement `trading_bot.broker.Broker` for your brokerage and wire it in
`trading_bot/app.py`. Before doing so, backtest, paper trade for a while, and
keep `closed_bars_only` enabled to avoid acting on unfinished bars.

## Disclaimer

This software is for educational purposes. Trading involves substantial risk;
past performance of a strategy is not indicative of future results. Nothing here
is financial advice.

## License

MIT
