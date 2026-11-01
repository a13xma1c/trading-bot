"""Application configuration.

Configuration lives in a JSON file (``config.json`` by default).  Secrets such
as API keys may also be supplied through environment variables so they never
have to be committed to version control:

* ``TRADING_BOT_API_KEY`` - overrides ``data.api_key``
* ``TRADING_BOT_WEBHOOK_URL`` - overrides ``notifications.webhook_url``
* ``TRADING_BOT_SMTP_PASSWORD`` - overrides ``notifications.email.password``
* ``TRADING_BOT_WEB_TOKEN`` - overrides ``web.token`` (API token for scripted access)
* ``TRADING_BOT_WEB_PASSWORD`` - plain-text login password (hashed in memory; handy for containers)
* ``TRADING_BOT__<SECTION>__<FIELD>`` - any other setting, e.g. ``TRADING_BOT__DATA__PROVIDER=demo``

The loader also understands the legacy flat layout used by the first version of
this project (``api_key``, ``scan_interval``, ``rsi_period`` ...) and migrates
it transparently.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_WATCHLIST = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]

# Free-tier request budgets of the supported providers.  They are deliberately
# conservative; users on paid plans can raise them in the config file.
PROVIDER_BUDGETS: dict[str, tuple[int, int]] = {
    "alphavantage": (5, 25),
    "twelvedata": (8, 800),
    "csv": (10_000, 1_000_000),
    "demo": (10_000, 1_000_000),
}

VALID_INTERVALS = ("1min", "5min", "15min", "30min", "60min", "1day")
INTERVAL_SECONDS = {"1min": 60, "5min": 300, "15min": 900, "30min": 1800, "60min": 3600, "1day": 86400}


class ConfigError(ValueError):
    """Raised when the configuration is invalid."""


@dataclass
class DataConfig:
    provider: str = "alphavantage"
    api_key: str = ""
    interval: str = "5min"
    bars: int = 300
    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    cache_ttl_seconds: int = 60
    csv_directory: str = "data"
    closed_bars_only: bool = False  # ignore the still-forming last intraday bar

    def budget(self) -> tuple[int, int]:
        per_min, per_day = PROVIDER_BUDGETS.get(self.provider, (5, 500))
        return (
            self.requests_per_minute or per_min,
            self.requests_per_day or per_day,
        )


@dataclass
class MarketHoursConfig:
    """Trading session used to pace the request budget and to pause polling when closed."""

    enabled: bool = True
    timezone: str = "America/New_York"
    open: str = "09:30"
    close: str = "16:00"
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Monday = 0

    def session_seconds(self) -> int:
        (oh, om), (ch, cm) = _parse_hhmm(self.open), _parse_hhmm(self.close)
        return (ch * 60 + cm - oh * 60 - om) * 60

    def validate(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"market_hours.timezone {self.timezone!r} is not a valid IANA time zone") from exc
        if self.session_seconds() <= 0:
            raise ConfigError("market_hours.close must be later than market_hours.open")
        if not self.weekdays or any(d not in range(7) for d in self.weekdays):
            raise ConfigError("market_hours.weekdays must contain values 0 (Monday) .. 6 (Sunday)")


def _parse_hhmm(text: str) -> tuple[int, int]:
    try:
        hours, minutes = str(text).split(":")
        h, m = int(hours), int(minutes)
    except ValueError as exc:
        raise ConfigError(f"Invalid time {text!r}; expected HH:MM") from exc
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ConfigError(f"Invalid time {text!r}; expected HH:MM")
    return h, m


@dataclass
class EmailConfig:
    enabled: bool = False
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    use_tls: bool = True


@dataclass
class NotificationConfig:
    desktop: bool = True
    sound: bool = True
    webhook_url: str = ""
    email: EmailConfig = field(default_factory=EmailConfig)


@dataclass
class WebConfig:
    """Settings of the built-in web server.

    The UI requires a login.  The password is stored as a PBKDF2 hash
    (``trading-bot set-password``); until one is configured the web UI shows a
    first-run setup page.  ``token`` is an optional bearer token for scripted
    API access.
    """

    host: str = "127.0.0.1"   # bind to localhost only unless you know what you are doing
    port: int = 8080
    username: str = "admin"
    password_hash: str = ""   # set with `trading-bot set-password` (or TRADING_BOT_WEB_PASSWORD)
    token: str = ""           # optional API token (or TRADING_BOT_WEB_TOKEN)
    secret_key: str = ""      # signs session cookies; generated and persisted in data_dir when empty
    session_hours: float = 12.0
    base_path: str = ""       # external URL prefix when served behind a reverse proxy, e.g. "/bots/a"
    worker_threads: int = 64  # thread pool used for (blocking) API handlers
    open_browser: bool = True


@dataclass
class TradingConfig:
    trading_enabled: bool = False
    paper: bool = True
    initial_cash: float = 100_000.0
    position_size_pct: float = 0.10
    max_positions: int = 5


@dataclass
class AppConfig:
    watchlist: list[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))
    scan_interval: int = 60
    data_dir: str = "bot_data"
    strategies_file: str = "strategies.json"
    plugins_dir: str = "strategies"  # Python Strategy subclasses (*.py) are discovered here
    log_level: str = "INFO"
    data: DataConfig = field(default_factory=DataConfig)
    market_hours: MarketHoursConfig = field(default_factory=MarketHoursConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    web: WebConfig = field(default_factory=WebConfig)

    # ------------------------------------------------------------------ io
    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppConfig:
        raw = _migrate_legacy(dict(raw))
        cfg = cls(
            watchlist=[s.strip().upper() for s in raw.get("watchlist", DEFAULT_WATCHLIST) if s.strip()],
            scan_interval=int(raw.get("scan_interval", 60)),
            data_dir=str(raw.get("data_dir", "bot_data")),
            strategies_file=str(raw.get("strategies_file", "strategies.json")),
            plugins_dir=str(raw.get("plugins_dir", "strategies")),
            log_level=str(raw.get("log_level", "INFO")),
            data=_build(DataConfig, raw.get("data", {})),
            market_hours=_build(MarketHoursConfig, raw.get("market_hours", {})),
            notifications=_build_notifications(raw.get("notifications", {})),
            trading=_build(TradingConfig, raw.get("trading", {})),
            web=_build(WebConfig, raw.get("web", {})),
        )
        cfg.apply_env()
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | os.PathLike[str] = "config.json") -> AppConfig:
        path = Path(path)
        if not path.exists():
            logger.warning("Config file %s not found, using defaults", path)
            cfg = cls()
            cfg.apply_env()
            cfg.validate()
            cfg.base_dir = path.parent  # relative paths live next to the config file once it is created
            return cfg
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        cfg = cls.from_dict(raw)
        cfg.base_dir = path.parent
        return cfg

    # Relative paths in the config file are interpreted relative to the file itself
    # (``base_dir``); a config built in memory uses the current working directory.
    base_dir: Path | None = field(default=None, repr=False, compare=False)

    def resolve(self, value: str | os.PathLike[str]) -> Path:
        path = Path(value)
        if path.is_absolute() or self.base_dir is None:
            return path
        return self.base_dir / path

    @property
    def data_dir_path(self) -> Path:
        return self.resolve(self.data_dir)

    @property
    def strategies_path(self) -> Path:
        return self.resolve(self.strategies_file)

    @property
    def plugins_path(self) -> Path:
        return self.resolve(self.plugins_dir)

    @property
    def csv_directory_path(self) -> Path:
        return self.resolve(self.data.csv_directory)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("base_dir", None)
        return data

    def save(self, path: str | os.PathLike[str] = "config.json") -> None:
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)

    # ------------------------------------------------------------ helpers
    def apply_env(self) -> None:
        """Apply environment overrides.

        Besides the dedicated secret variables, any setting can be overridden with
        ``TRADING_BOT__<SECTION>__<FIELD>`` (double underscores), e.g.
        ``TRADING_BOT__DATA__PROVIDER=demo``, ``TRADING_BOT__WEB__HOST=0.0.0.0`` or
        ``TRADING_BOT__WATCHLIST=AAPL,MSFT``.  Values are converted to the field's
        type (bool/int/float/list).
        """
        for name, value in sorted(os.environ.items()):
            if name.startswith("TRADING_BOT__"):
                self._apply_override(name[len("TRADING_BOT__"):].lower().split("__"), value, name)
        if key := os.environ.get("TRADING_BOT_API_KEY"):
            self.data.api_key = key
        if url := os.environ.get("TRADING_BOT_WEBHOOK_URL"):
            self.notifications.webhook_url = url
        if pw := os.environ.get("TRADING_BOT_SMTP_PASSWORD"):
            self.notifications.email.password = pw
        if token := os.environ.get("TRADING_BOT_WEB_TOKEN"):
            self.web.token = token
        if password := os.environ.get("TRADING_BOT_WEB_PASSWORD"):
            from .web.auth import hash_password

            self.web.password_hash = hash_password(password)
        self.watchlist = [s.strip().upper() for s in self.watchlist if s.strip()]

    def _apply_override(self, path: list[str], value: str, name: str) -> None:
        target: Any = self
        for part in path[:-1]:
            target = getattr(target, part, None)
            if target is None or not hasattr(target, "__dataclass_fields__"):
                raise ConfigError(f"{name}: unknown configuration section {'.'.join(path[:-1])!r}")
        field_name = path[-1]
        field_info = next((f for f in fields(target) if f.name == field_name), None)
        if field_info is None:
            raise ConfigError(f"{name}: unknown setting {'.'.join(path)!r}")
        setattr(target, field_name, _coerce(value, field_info.type, name))

    def validate(self) -> None:
        if self.scan_interval < 1:
            raise ConfigError("scan_interval must be >= 1 second")
        if self.data.interval not in VALID_INTERVALS:
            raise ConfigError(
                f"data.interval must be one of {', '.join(VALID_INTERVALS)}; got {self.data.interval!r}"
            )
        if self.data.provider not in PROVIDER_BUDGETS:
            raise ConfigError(
                f"data.provider must be one of {', '.join(PROVIDER_BUDGETS)}; got {self.data.provider!r}"
            )
        if self.data.bars < 30:
            raise ConfigError("data.bars must be >= 30 so indicators have enough history")
        if not 0 < self.trading.position_size_pct <= 1:
            raise ConfigError("trading.position_size_pct must be in (0, 1]")
        if self.trading.initial_cash <= 0:
            raise ConfigError("trading.initial_cash must be positive")
        self.market_hours.validate()
        if not 0 < self.web.port < 65536:
            raise ConfigError("web.port must be between 1 and 65535")
        if not self.web.username.strip():
            raise ConfigError("web.username must not be empty")
        if self.web.session_hours <= 0:
            raise ConfigError("web.session_hours must be positive")
        if self.web.base_path and (not self.web.base_path.startswith("/") or self.web.base_path.endswith("/")):
            raise ConfigError("web.base_path must start with '/' and not end with '/' (e.g. /bots/a)")
        if self.web.worker_threads < 1:
            raise ConfigError("web.worker_threads must be >= 1")
        for sym in self.watchlist:
            if not sym.replace(".", "").replace("-", "").isalnum():
                raise ConfigError(f"Invalid symbol in watchlist: {sym!r}")

    def requires_api_key(self) -> bool:
        return self.data.provider in ("alphavantage", "twelvedata")


def _coerce(value: str, type_hint: Any, name: str) -> Any:
    """Convert an environment string to the type of a dataclass field."""
    hint = str(type_hint)
    if "bool" in hint:
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
        raise ConfigError(f"{name}: expected a boolean, got {value!r}")
    if "list" in hint:
        return [item.strip() for item in value.split(",") if item.strip()]
    try:
        if hint.startswith("int") or hint == "int | None":
            return None if value.strip() == "" else int(value)
        if hint.startswith("float"):
            return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name}: expected a number, got {value!r}") from exc
    return value


def _build(cls, raw: dict[str, Any]):
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        logger.warning("Ignoring unknown %s keys: %s", cls.__name__, ", ".join(sorted(unknown)))
    return cls(**{k: v for k, v in raw.items() if k in known})


def _build_notifications(raw: dict[str, Any]) -> NotificationConfig:
    raw = dict(raw)
    email_raw = raw.pop("email", {}) or {}
    cfg = _build(NotificationConfig, raw)
    cfg.email = _build(EmailConfig, email_raw)
    return cfg


def _migrate_legacy(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert the v0 flat config layout into the nested layout."""
    legacy_keys = {"api_key", "tickers_source", "strategy", "rsi_period", "rsi_oversold",
                   "rsi_overbought", "short_ma_period", "long_ma_period", "profit_threshold",
                   "max_stocks_to_analyze", "trading_enabled"}
    if not legacy_keys & set(raw):
        return raw
    logger.info("Migrating legacy configuration layout")
    data = dict(raw.get("data", {}))
    if "api_key" in raw and "api_key" not in data:
        data["api_key"] = raw["api_key"]
    data.setdefault("provider", "alphavantage")
    raw["data"] = data
    trading = dict(raw.get("trading", {}))
    if "trading_enabled" in raw:
        trading.setdefault("trading_enabled", bool(raw["trading_enabled"]))
    raw["trading"] = trading
    for key in legacy_keys:
        raw.pop(key, None)
    return raw
