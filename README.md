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
  a `Broker` interface for plugging in a real broker. **Multi-user**: people can
  sign up themselves (optionally with e-mail verification), every user gets an
  independent paper account and decides whether signals are executed in it.
* **Manual trading & broker connection**: an order ticket (market / limit,
  stop-loss / take-profit, cash-based sizing) to add or close positions in your
  own account, resting limit orders, and a per-user **broker connection** —
  route orders and auto-traded signals to **Alpaca** (paper or live) instead of
  the built-in paper account through the pluggable `Broker` interface.
* **Execution simulation**: every order can be dry-run first — expected fill
  after slippage (fixed half-spread or square-root market-impact model),
  commissions, resulting cash/position, feasibility warnings. The same model
  prices the paper account's fills so the P&L history already includes costs.
* **Wallet**: add or withdraw money through a **payment provider** (built-in
  sandbox checkout or **Stripe Checkout** + webhooks), deposits in any currency
  converted at the current rate, and a full **transaction ledger** (deposits,
  withdrawals, trades, fees, running balance) with filters and CSV export.
* **Exchange rates** page: live rate table (ECB reference rates via
  Frankfurter, no key needed), historical chart per pair and a converter.
* **Trade history & analytics**: per-user fill and closed-trade history with a
  cumulative P&L chart, performance statistics (win rate, profit factor,
  expectancy, drawdown, streaks), breakdowns by strategy / symbol / hour /
  weekday, rule-based insights and — with any OpenAI-compatible endpoint
  configured — an **AI-generated written assessment**.
* **Backtester** (next-open fills, intrabar stop/target, commissions) with
  equity curve, trade list and summary statistics.
* **REST + WebSocket API** (`/api/docs`), optional API token, headless mode
  for servers (`trading-bot run`) plus `scan`, `backtest` and `check` commands.
* **Test suite**: ~290 fully typed tests covering indicators, the rule language
  (including sandbox escapes), providers (recorded API payloads), rate limiting,
  engine, brokers (paper + a fake Alpaca API), execution simulation, payments
  (sandbox + Stripe signatures/webhooks), FX, backtester, analytics,
  notifications, user accounts and e-mail, config migration, CLI, the HTTP /
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

The server binds to `127.0.0.1:8080` by default.

### Login, accounts and sign-up

The web UI requires a login. On the very first start no account exists yet,
so the browser shows a **first-run setup page** where you create the
administrator account (username, optional e-mail, password) and can optionally
choose the market-data provider and enter its API key (both can be changed
later under *Settings*). Alternatively set the administrator password from the
command line or the environment — this also **resets** a forgotten password at
the next start:

```bash
uv run trading-bot set-password --username admin      # prompts for the password
TRADING_BOT_WEB_PASSWORD=... uv run trading-bot serve # or supply it via the environment (containers)
```

Passwords must have at least 10 characters with lower- and uppercase letters, a
digit and a symbol, must not contain the username and must not be a commonly
used password; every password form checks these live while you type (the
server enforces the same rules) and has a *reveal* (eye) button. Accounts are
stored in the SQLite database (`bot_data/history.sqlite3`, PBKDF2-SHA256
hashes). Sessions use an HttpOnly cookie bound to the password hash and expire
after `web.session_hours`; five failed logins lock the client out for a
minute. Users sign in with their **username or e-mail address**. Scripts can
authenticate with a bearer token instead (`web.token` /
`TRADING_BOT_WEB_TOKEN`, `Authorization: Bearer <token>`); the token acts as the
administrator.

**Sign-up.** After logging out (or on the login page) visitors can *Create
account* while `web.allow_signup` is on (default; administrators toggle it
under *Settings → Sign-up & e-mail*). New users get their own paper account
with `trading.initial_cash`, see only their own positions, fills and
analytics, and choose under *Settings → My account* whether signals are
executed in their account ("auto-trade"). Strategies, the watchlist and the
market-data connection are shared.

**E-mail verification** (`web.email_verification`):

| Value | Behaviour |
| --- | --- |
| `auto` (default) | Required when e-mail delivery is configured, otherwise accounts are active immediately |
| `required` | Always required (sign-up is refused until e-mail delivery works) |
| `off` | Never |

A verification link (valid for 24 hours) is sent through
`notifications.email`: `transport: "smtp"` uses the SMTP settings (host, port,
user, password — `TRADING_BOT_SMTP_PASSWORD` —, sender, TLS);
`transport: "outbox"` writes `.eml` files into `bot_data/outbox/` instead,
which is handy for trying the flow without a mail server. Until the link is
opened the login page refuses the account with a *Resend verification e-mail*
button. Set `web.public_url` (e.g. `https://bot.example.com`) when the bot
runs behind a proxy so the links carry the right address. Administrators can
list users at `GET /api/users`.

To reach the UI from other machines set `web.host` (e.g. `0.0.0.0`) and put a
TLS-terminating reverse proxy in front of it for anything but a private
network (the session cookie is only marked `Secure` when served over HTTPS).

### API key

The app starts and is fully usable without a key: the first-run page lets you
pick the data provider and (optionally) enter its key, and the dashboard shows
a banner with *Enter API key* / *Use demo data* until a key is configured.
Everything entered under *Settings* is applied immediately — no restart.

1. Get a free key from [Alpha Vantage](https://www.alphavantage.co/support/#api-key)
   (25 requests/day, 5/minute) or [Twelve Data](https://twelvedata.com/pricing)
   (800 requests/day, 8/minute — a much better fit for intraday scanning).
2. Enter it on the setup page or under *Settings → Market data*, or provide it
   through the environment (never ends up in git):
   ```bash
   export TRADING_BOT_API_KEY=your_key
   ```
   or put it in `config.json` under `data.api_key`.

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
| `trading-bot set-password [--username U] [--password P]` | Set / reset the administrator password (applied at the next start) |
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
* **Dashboard** – watchlist with ticker suggestions while you type (symbol +
  company name from a bundled directory of major US stocks and ETFs — instant
  and free; an explicit *Search provider* row queries the data provider at the
  cost of one API request), click a row to chart it, × to remove; candlestick chart with an *Overlay strategy* selector that draws a
  strategy's indicators and marks where it would have fired, and the signal
  list. New signals show a snackbar, an optional browser notification and a
  sound.
* **Strategies** – create / duplicate / delete strategies, edit rules and
  parameters, *Validate* (syntax check + evaluation on the charted data),
  *Show on chart*, *Backtest…* (stats, equity curve, trade list; trades can be
  marked on the chart). The switch in the list enables a strategy for live
  scanning.
* **Positions** – *your* account (cash, equity, venue, auto-trade state), open
  positions with P&L, stops and targets (× closes a position at market),
  resting orders, recent fills; reset button for the paper account.
* **Trade** – order ticket: symbol (with suggestions), buy/sell, quantity *or*
  cash amount, market or limit, optional stop-loss / take-profit %. **Simulate**
  shows the expected fill price after slippage, commission, cash and position
  after the trade, realized P&L for sells and warnings (cash usage, share of
  average volume, non-marketable limit) — nothing is sent. **Place order**
  executes in your account after a confirmation (live brokerage orders are
  flagged). Limit orders that are not marketable rest until a bar trades
  through the limit (paper) or at the brokerage; cancel them from the table.
* **Wallet** – balance KPIs, **Add money** (amount + currency with a live
  conversion quote → payment provider checkout → back to the wallet with the
  result), **Withdraw** (only uninvested, unreserved cash), and the
  **transaction ledger** with type/period filters and *Export CSV*.
* **FX** – exchange-rate table for a chosen base currency, history chart
  (1M / 3M / 1Y / 5Y) for the selected pair and a converter.
* **History** – KPI cards (realized P&L, closed trades, win rate, profit
  factor, expectancy, max drawdown, average holding time), a **cumulative P&L
  chart** (green above / red below zero, one dot per closed trade), the list of
  closed trades (entry/exit, return %, holding time — sells are matched to
  buys FIFO) and every fill of your account.
* **Insights** – the same statistics plus rule-based observations ("'X' is the
  weakest strategy", "losses are larger than wins", weak hours, streaks …),
  breakdown tables by strategy, symbol, hour of exit and weekday, and a
  **Generate analysis** button that asks the configured LLM for a written
  assessment (see [AI insights](#ai-insights)).
* **Settings** – provider, API key, interval, budget, market hours,
  notifications, trading; *My account* (e-mail, auto-trade switch, password
  change); *Broker connection* (paper account or your Alpaca keys, verified
  before saving); for administrators also *Execution simulation* (slippage /
  commission model), *Payments & exchange rates* (provider, account currency,
  limits, Stripe keys, FX provider), *AI insights* and *Sign-up & e-mail*
  (sign-up on/off, verification policy, public URL, SMTP / outbox transport).
  Changes are applied live; provider / key / budget changes rebuild the data
  connection on the spot.

### HTTP API

Everything the UI does is available at `/api/*` (interactive docs at
`/api/docs`): status and engine control, watchlist, bars and overlays,
strategies (validate / save / enable / delete), backtests, signals, positions
(`DELETE /api/positions/{symbol}` closes one), orders (`POST /api/orders/simulate`,
`POST /api/orders`, `GET/DELETE /api/orders`), broker connection
(`GET/PUT /api/broker/connection`, `/api/broker/providers`), wallet
(`/api/wallet`, `/deposit`, `/withdraw`, `/deposits/{id}/sync`, `/quote`),
transactions (`/api/transactions?kind=&status=&days=&format=csv`), payments
(`POST /api/payments/webhook`, sandbox `/api/payments/mock/*`), FX
(`/api/fx/latest|history|convert|currencies`), history (`/api/history`),
analytics (`/api/analytics`, `POST /api/analytics/ai`), settings, users
(`/api/users`, admin) plus `/api/auth/*` (status, setup, signup,
resend-verification, login, logout, password, me, profile) and
`/verify?token=` for e-mail links. `/ws`
streams events (`bars`, `signal`, `scan`, `fetch_failed`, `engine`,
`positions`, `order`, `wallet`, `status`) as JSON. Requests are authenticated by the session
cookie or, for scripts, `Authorization: Bearer <token>` (`?token=` for the
WebSocket).

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

## Trading: manual orders, brokers and execution simulation

### Order ticket and position management

Besides acting on strategy signals, every user can **add or remove positions**
by hand on the *Trade* page (or `POST /api/orders`): market or limit orders,
sized in shares or in cash, with optional stop-loss / take-profit percentages
that the engine then monitors like for automatic entries. `× Close` on a
position (`DELETE /api/positions/{symbol}`) sells it at the current price.
Manual fills appear in the fill history, the transaction ledger and the
analytics like any other trade (strategy `manual`).

Limit orders that cannot fill immediately **rest**: in the paper account they
are checked against every new bar (a buy fills when the bar's low trades
through the limit, a sell when the high does) and optionally expire after
`execution.limit_order_expiry_bars`; at a connected brokerage they rest there.
Cash needed by resting buy orders is reserved — it cannot be withdrawn or spent
by another limit order.

### Execution simulation

*Simulate* (`POST /api/orders/simulate`) dry-runs an order against the
account's current cash and positions using recent bars:

| Setting (`execution.*`) | Meaning |
| --- | --- |
| `slippage_model` | `none` (fill at the last price), `fixed` (half-spread of `slippage_bps` against the trader), `volume` (fixed + square-root market impact: `impact_coefficient × volatility × √(quantity / average bar volume)`) |
| `commission_per_order`, `commission_per_share`, `commission_pct`, `min_commission` | Commission schedule |
| `max_participation_pct` | Warn when an order exceeds this share of the average bar volume |
| `limit_order_expiry_bars` | Resting paper limit orders expire after this many bars (0 = never) |

The result lists the expected fill price, slippage cost, commission, cash and
position after the trade, the realized P&L of a sell, stop/target levels and
warnings (insufficient cash, position too small, non-marketable limit, heavy
participation, high commission ratio, more positions than
`trading.max_positions`). The **same model prices every paper fill** — automatic
signals, manual orders and resting limit orders — so the paper P&L is net of
realistic costs. The defaults (`none`, no commissions) reproduce idealised
fills; `config.demo.json` enables the volume model with a 1.00 commission.

### Connecting a brokerage (Alpaca)

Each user chooses their execution venue under *Settings → Broker connection*
(`PUT /api/broker/connection`): the built-in paper account or **Alpaca**
(`provider: alpaca`, API key + secret, paper/live switch). Credentials are
verified against the brokerage before they are saved (a typo never leaves the
account without a venue) and stored in the bot's database — protect `bot_data/`
accordingly and prefer Alpaca's *paper* keys until you trust the setup.
With a brokerage connected:

* manual orders and auto-traded signals (if *auto-trade* is on) are sent to it
  — market orders as `day` orders, stops/targets as bracket legs, limit orders
  as `gtc`; fills are polled for up to 10 s and then recorded;
* positions, cash and equity on *Positions* / *Trade* / *Wallet* come from the
  brokerage; protective exits are left to the bracket orders;
* deposits and withdrawals are disabled (fund the account at the broker);
* the fill history and ledger are still kept locally for History / Insights.

Other brokerages can be added by implementing `trading_bot.broker.Broker`
(`submit_market_order`, `positions`, `cash`, optionally `submit_limit_order`,
`pending_orders`, `cancel_order`, `close_position`, `test_connection`) and
registering it in `trading_bot/brokers/__init__.py`.

## Wallet: deposits, withdrawals and the transaction ledger

Paper accounts can be funded and drained through a **payment provider**
(`payments.provider`):

| Provider | Behaviour |
| --- | --- |
| `mock` (default) | Built-in sandbox: *Add money* redirects to a local checkout page (`/pay/mock/<session>`) where the payment is confirmed or cancelled; payouts are paid instantly. Perfect for demos and tests — no real money. |
| `stripe` | Creates a [Stripe Checkout](https://stripe.com/docs/payments/checkout) session and redirects to Stripe; the account is credited when the `checkout.session.completed` webhook arrives at `POST /api/payments/webhook` (signature verified with `payments.stripe.webhook_secret`) or when the session is found paid on return. Withdrawals create Stripe payouts (`payout.paid` / `payout.failed` webhooks complete or refund them). Keys: `payments.stripe.secret_key` / `TRADING_BOT_STRIPE_SECRET_KEY`, `webhook_secret` / `TRADING_BOT_STRIPE_WEBHOOK_SECRET`. |
| `none` | Funding disabled (the wallet page still shows the ledger). |

Deposits may be made in any currency the FX provider knows: the amount is
converted into the account currency (`payments.currency`) at the current rate,
the original amount and rate are kept on the ledger entry, and the wallet shows
a live quote while typing. `payments.min_amount` / `max_amount` bound every
deposit and withdrawal. Only uninvested cash that is not reserved by resting
buy orders can be withdrawn; a failed payout refunds the account.

Every cash movement is recorded in the **transaction ledger** (`GET
/api/transactions`, *Wallet → Transactions*): deposits, withdrawals, buys and
sells (with commission, symbol, quantity, price and the fill id as reference),
each with a status (`pending`, `completed`, `failed`, `cancelled`) and the
running balance. Filter by type, status or period, or download it as CSV.
Amount mismatches and replayed webhooks are detected and ignored.

## Exchange rates

The *FX* page (`/api/fx/*`) shows a rate table for any base currency, a
history chart of the selected pair and a converter. `fx.provider`:

* `frankfurter` (default): [Frankfurter](https://www.frankfurter.app) serves
  the European Central Bank reference rates — free, no API key, ~30 currencies,
  updated once per working day (not intraday). Responses are cached for
  `fx.cache_ttl_seconds`; if the service is down the last table is served and
  flagged as stale.
* `demo`: deterministic synthetic rates for offline use (used by the tests and
  `config.demo.json`).

The same service converts foreign-currency deposits.

## AI insights

The *Insights* page always shows deterministic, rule-based observations. To
add a narrative analysis configure any **OpenAI-compatible chat-completions
endpoint** under *Settings → AI insights* (or in `config.json`):

```jsonc
"ai": {
  "enabled": true,
  "base_url": "https://api.openai.com/v1",   // or Azure/Groq/Together…, or http://localhost:11434/v1 for Ollama
  "api_key": "",                             // or TRADING_BOT_AI_API_KEY; optional for localhost endpoints
  "model": "gpt-4o-mini",
  "timeout_seconds": 45
}
```

*Generate analysis* sends only the aggregated statistics of your account
(stats, breakdowns, the last 15 closed trades, the heuristic observations) —
never credentials, e-mail addresses or raw fills — and renders the model's
verdict, strengths/weaknesses and suggestions. Nothing is sent unless you
click the button.

## Deployment & scaling

### Container

```bash
docker build -t trading-bot .
docker run -d -p 8080:8080 -v trading-bot-data:/data \
  -e TRADING_BOT_WEB_PASSWORD=change-me -e TRADING_BOT__DATA__PROVIDER=demo trading-bot
```

The image is multi-stage (dependencies installed from the lock file with uv,
slim runtime, non-root user, `HEALTHCHECK` on `/api/health`). All state lives in
`/data` (config, user accounts and signal history (SQLite), paper accounts, rate-limit
counters, session secret), so the container is disposable. Every setting can be
supplied through the environment: `TRADING_BOT__<SECTION>__<FIELD>`
(`TRADING_BOT__WEB__HOST`, `TRADING_BOT__WATCHLIST=AAPL,MSFT`,
`TRADING_BOT__MARKET_HOURS__ENABLED=false`, …) plus the secrets
`TRADING_BOT_API_KEY`, `TRADING_BOT_WEB_PASSWORD`, `TRADING_BOT_WEB_TOKEN`,
`TRADING_BOT_AI_API_KEY`, `TRADING_BOT_SMTP_PASSWORD`, `TRADING_BOT_STRIPE_SECRET_KEY`,
`TRADING_BOT_STRIPE_WEBHOOK_SECRET`.

`docker compose up -d` starts a bot shard behind an **nginx** reverse proxy
(`deploy/nginx.conf`: TLS-ready, gzip, WebSocket upgrade, rate limiting of the
login endpoint and the API).

### How it scales

The unit of scale is a **shard**: one engine with its own watchlist, API key
(and therefore its own provider budget), strategies, paper account and history.
Within a shard:

* the API is stateless — sessions are HMAC-signed cookies (`web.secret_key`,
  generated into `/data` if unset), so restarts and rolling updates keep users
  signed in and no session store is needed;
* serialized bar payloads are memoized per fetch, responses are gzip-compressed
  and handlers run in a thread pool (`web.worker_threads`);
* WebSocket fan-out uses bounded per-client queues: one slow browser cannot
  build up memory or stall the others (dropped events are counted in the log);
* `/api/health` (liveness) and `/api/ready` (readiness, incl. the number of
  WebSocket clients) serve orchestrators.

To scale **out**, run more shards behind the proxy, each under its own base
path (`TRADING_BOT__WEB__BASE_PATH=/bots/b`, see the commented `bot-b` service
in `docker-compose.yml` and the matching `location` in `deploy/nginx.conf`).
Sharding by watchlist is also what the data providers' per-key budgets call
for. Running several replicas of the *same* shard is not supported: the engine
owns the scan loop, the paper broker and the SQLite history.

### Scalability testing

`python -m trading_bot.tools.loadtest` simulates many browser sessions against
a running server — N long-lived WebSocket subscribers, M concurrent HTTP users
cycling through the dashboard endpoints, and a forced scan whose event must
reach every subscriber — and reports throughput, latency percentiles and
fan-out latency. Thresholds make it a pass/fail gate:

```bash
python -m trading_bot.tools.loadtest --url http://localhost:8080 --password ... \
  --ws-clients 300 --users 30 --duration 30 --max-p95-ms 750 --max-fanout-ms 2000 --min-rps 100
```

`docker-compose.ci.yml` runs exactly that against the freshly built image
(`docker compose -f docker-compose.ci.yml up --abort-on-container-exit --exit-code-from loadtest`);
a smaller version is part of the regular test suite (`tests/test_scalability.py`).

### CI

`.github/workflows/ci.yml` runs on every push / pull request:

| Job | What |
| --- | --- |
| `quality` | ruff, mypy |
| `tests` | the full pytest suite inside the official Playwright container (browser tests included) |
| `container` | builds the runtime and test images, smoke-tests the image (starts with no config, health/ready, login page, anonymous API refused, unprivileged user), runs the unit/API/scalability tests inside the container and the containerized load test with thresholds; reports are uploaded as artifacts |
| `publish` | pushes the image to GHCR on `main` and version tags |

## Configuration reference (`config.json`)

```jsonc
{
  "watchlist": ["AAPL", "MSFT"],
  "scan_interval": 60,             // seconds between scan loops
  "data_dir": "bot_data",          // sqlite history, paper broker state, rate-limit counter, logs
  "strategies_file": "strategies.json",
  "plugins_dir": "strategies",     // Python plugin strategies
  "log_level": "INFO",
  "web": {                         // web server + login
    "host": "127.0.0.1", "port": 8080, "open_browser": true,
    "username": "admin", "password_hash": "",   // legacy/recovery: `trading-bot set-password` puts a hash here,
                                                // it is moved into the user database at the next start
    "token": "",                   // optional bearer token for scripts (or TRADING_BOT_WEB_TOKEN)
    "secret_key": "",              // signs session cookies; generated into data_dir when empty
    "session_hours": 12,
    "allow_signup": true,          // visitors may create their own account
    "email_verification": "auto",  // auto | required | off (see "Login, accounts and sign-up")
    "public_url": "",              // external URL used in e-mails, e.g. https://bot.example.com
    "base_path": "",               // e.g. "/bots/a" when served under a prefix by a reverse proxy
    "worker_threads": 64
  },
  "data": {
    "provider": "alphavantage",    // alphavantage | twelvedata | csv | demo
    "api_key": "",                 // or TRADING_BOT_API_KEY
    "interval": "5min",            // 1min 5min 15min 30min 60min 1day
    "bars": 300,                   // history per symbol (indicators need warm-up)
    "requests_per_minute": null,   // null = provider free-tier default
    "requests_per_day": null,
    "cache_ttl_seconds": 60,
    "csv_directory": "data",       // for the csv provider: <SYMBOL>.csv with time,open,high,low,close,volume
    "closed_bars_only": false,     // true = ignore the still-forming intraday bar
    "symbols_file": ""             // optional "symbol,name[,type]" CSV merged into the ticker suggestions
  },
  "market_hours": {                // session used for budget pacing; polling pauses when closed
    "enabled": true,               // false = 24h markets (crypto/forex), pace over the whole day
    "timezone": "America/New_York",
    "open": "09:30",
    "close": "16:00",
    "weekdays": [0, 1, 2, 3, 4]    // Monday = 0
  },
  "notifications": {
    "desktop": true, "sound": true, "webhook_url": "",
    "email": {                     // signal e-mails and account verification links
      "enabled": false,            // send trade signals by e-mail
      "transport": "smtp",         // smtp | outbox (writes .eml files to <data_dir>/outbox)
      "host": "", "port": 587, "username": "", "password": "", "sender": "", "recipients": [], "use_tls": true
    }
  },
  "ai": { "enabled": false, "base_url": "https://api.openai.com/v1", "api_key": "", "model": "gpt-4o-mini", "timeout_seconds": 45 },
  "execution": {                   // fill simulation for paper accounts + the order ticket's Simulate
    "slippage_model": "none",      // none | fixed | volume
    "slippage_bps": 5, "impact_coefficient": 0.1, "max_participation_pct": 10,
    "commission_per_order": 0, "commission_per_share": 0, "commission_pct": 0, "min_commission": 0,
    "limit_order_expiry_bars": 0   // resting paper limit orders expire after N bars (0 = never)
  },
  "payments": {                    // funding of paper accounts
    "provider": "mock",            // mock (sandbox checkout) | stripe | none
    "currency": "USD",             // account currency
    "min_amount": 10, "max_amount": 1000000,
    "stripe": { "secret_key": "", "webhook_secret": "", "api_base": "https://api.stripe.com" }
  },
  "fx": {                          // exchange rates (FX page, deposit conversion)
    "provider": "frankfurter",     // frankfurter (ECB, free) | demo
    "base_currency": "USD",
    "quote_currencies": ["EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "CNY", "SEK", "NOK", "MXN", "INR", "BRL"],
    "cache_ttl_seconds": 3600
  },
  "trading": {
    "trading_enabled": false,      // execute signals through the (paper) broker
    "paper": true,
    "initial_cash": 100000,        // starting cash of every user's paper account
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
  engine.py          scan loop, de-duplication, protective exits, resting orders, event bus, per-account execution
  notify.py          log / webhook / e-mail / callback channels
  broker.py          Broker interface + PaperBroker (market/limit orders, deposits, persisted) + AccountManager
  brokers/alpaca.py  Alpaca Trading API adapter (account, positions, market/limit/bracket orders, close)
  execution.py       ExecutionSimulator: slippage / commission models, order dry-runs
  trading.py         OrderService: manual orders, simulate, close position, cancel (ledger + events)
  payments.py        payment providers: sandbox checkout, Stripe (Checkout Sessions, payouts, webhooks)
  wallet.py          WalletService: deposits (with FX conversion), withdrawals, ledger, webhook handling
  fx.py              exchange rates: Frankfurter (ECB) / demo providers, caching FxService
  storage.py         SQLite signal & fill history, transaction ledger, user accounts
  analytics.py       closed trades (FIFO), P&L curve, statistics, heuristic insights, LLM client
  backtest.py        event-driven backtester
  app.py             composition root; __main__.py CLI
  tools/loadtest.py  load / scalability test (also used by CI in a container)
  web/
    server.py        FastAPI app: REST API, WebSocket broadcaster, static files
    routes_trading.py / routes_wallet.py / routes_fx.py   API routers (orders, broker connection, wallet, FX)
    charting.py      chart payloads (bars, overlays grouped into panes, markers)
    auth.py          PBKDF2 password hashing, password rules, signed sessions, login throttling
    users.py         registration, e-mail verification, authentication, profiles
    mail.py          SMTP / outbox mailer
    static/          Material Design single-page app (index.html, login.html, signup.html, pay.html,
                     css/material.css, js/app.js, js/chart.js, js/ui.js, js/login.js, js/signup.js, js/pay.js,
                     js/passwords.js, js/autocomplete.js) - no build step
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
| Unit | `tests/test_indicators.py`, `test_expression.py`, `test_strategy.py`, `test_backtest.py`, `test_broker_storage.py`, `test_execution.py`, `test_alpaca.py`, `test_payments.py`, `test_fx.py`, `test_analytics.py`, `test_users.py`, `test_notify_config.py`, `test_data_service.py`, `test_providers.py` | Pure logic; providers, the Alpaca API, Stripe, Frankfurter and the LLM client run against `httpx.MockTransport` fakes; e-mails go to the outbox |
| Integration | `tests/test_engine.py`, `tests/test_cli.py` | Scan loop, pacing, market hours, paper trading in several accounts with slippage/commissions, resting limit orders, manual order service, CLI commands incl. `serve` |
| Scalability | `tests/test_scalability.py` | Load-test tool run in-process: 60 WebSocket subscribers + 8 HTTP users, fan-out and latency thresholds |
| API | `tests/test_web_api.py`, `tests/test_web_auth.py`, `tests/test_web_trading_api.py` | Every REST endpoint, login/session/setup/throttling, sign-up + verification, per-user accounts, orders / broker connection (fake Alpaca), wallet + sandbox checkout + Stripe webhook, transactions CSV, FX, history/analytics/AI, bearer token and the WebSocket (FastAPI `TestClient`, no browser) |
| End-to-end | `tests/test_web_e2e.py` | Playwright + headless Chromium against a live server: login (reveal toggle, sign-up link), first-run setup, sign-up with verification e-mail and login by e-mail, layout/MD tokens, chart pixels, zoom/pan, watchlist, engine & notifications, editor, backtest dialog, settings, profile + password change, history (P&L canvas) and insights pages, order ticket (simulate → place → limit → cancel → close), wallet deposit through the sandbox checkout page + withdrawal + ledger filters, FX table/chart/converter, broker connection card, error states |

Browser tests need a Chromium for Playwright: `uv run playwright install chromium`
(they are skipped with a message if it is missing). All widgets used by the
automated tests carry a stable HTML `id` (`startButton`, `watchlistTable`,
`buyRuleInput`, ...).

The tests are fully type-annotated; `mypy` checks them with
`disallow_untyped_defs` and the package with `check_untyped_defs`.

## Going live

By default signals execute in the **paper** accounts. To trade real money
connect a brokerage under *Settings → Broker connection* (Alpaca ships in the
box — start with its paper environment; the live switch is explicit and every
live order asks for confirmation) or implement `trading_bot.broker.Broker` for
another brokerage. Before doing so, backtest, paper trade for a while with a
realistic `execution` model, and keep `closed_bars_only` enabled to avoid
acting on unfinished bars.

## Disclaimer

This software is for educational purposes. Trading involves substantial risk;
past performance of a strategy is not indicative of future results. Nothing here
is financial advice.

## License

MIT
