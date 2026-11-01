"""Notification channels.

A :class:`Notifier` fans every signal out to all configured channels.  A
failing channel is logged and never prevents the others from delivering, and
never interrupts the trading loop.
"""

from __future__ import annotations

import json
import logging
import smtplib
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any, Protocol

import requests

from .config import EmailConfig, NotificationConfig
from .strategy.base import Signal

logger = logging.getLogger(__name__)


class HttpPostSession(Protocol):
    """Subset of :class:`requests.Session` used by the webhook channel."""

    def post(self, url: str, *, data: Any = ..., headers: dict[str, str] | None = ...,
             timeout: float | None = ...) -> Any: ...


class NotificationChannel(ABC):
    name: str = "channel"

    @abstractmethod
    def send(self, signal: Signal) -> None: ...


class LogChannel(NotificationChannel):
    name = "log"

    def send(self, signal: Signal) -> None:
        logger.warning("TRADE SIGNAL: %s", signal.describe().replace("\n", " | "))


class CallbackChannel(NotificationChannel):
    """Delivers signals to an in-process callback (used by the GUI)."""

    name = "callback"

    def __init__(self, callback: Callable[[Signal], None]):
        self.callback = callback

    def send(self, signal: Signal) -> None:
        self.callback(signal)


class WebhookChannel(NotificationChannel):
    """POSTs a JSON payload compatible with Slack/Discord/Mattermost style webhooks."""

    name = "webhook"

    def __init__(self, url: str, session: HttpPostSession | None = None, timeout: float = 10.0):
        if not url:
            raise ValueError("webhook url is required")
        self.url = url
        self.session: HttpPostSession
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout

    def payload(self, signal: Signal) -> dict[str, Any]:
        text = signal.describe()
        return {"text": text, "content": text, "signal": signal.to_dict()}

    def send(self, signal: Signal) -> None:
        resp = self.session.post(self.url, data=json.dumps(self.payload(signal)),
                                 headers={"Content-Type": "application/json"}, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"webhook returned HTTP {resp.status_code}: {resp.text[:200]}")


class EmailChannel(NotificationChannel):
    name = "email"

    def __init__(self, cfg: EmailConfig, smtp_factory: Callable[[], Any] | None = None):
        if (cfg.transport == "smtp" and not cfg.host) or not cfg.recipients:
            raise ValueError("email host and recipients are required")
        self.cfg = cfg
        self._smtp_factory = smtp_factory or (lambda: smtplib.SMTP(cfg.host, cfg.port, timeout=15))

    def build_message(self, signal: Signal) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"[Trading Bot] {signal.headline()}"
        msg["From"] = self.cfg.sender or self.cfg.username
        msg["To"] = ", ".join(self.cfg.recipients)
        msg.set_content(signal.describe())
        return msg

    def send(self, signal: Signal) -> None:
        msg = self.build_message(signal)
        with self._smtp_factory() as smtp:
            if self.cfg.use_tls:
                smtp.starttls()
            if self.cfg.username:
                smtp.login(self.cfg.username, self.cfg.password)
            smtp.send_message(msg)


class Notifier:
    def __init__(self, channels: list[NotificationChannel] | None = None):
        self._channels: list[NotificationChannel] = list(channels or [])
        self._lock = threading.Lock()
        self.failures: dict[str, int] = {}

    def add_channel(self, channel: NotificationChannel) -> None:
        with self._lock:
            self._channels.append(channel)

    def remove_channel(self, channel: NotificationChannel) -> None:
        with self._lock:
            if channel in self._channels:
                self._channels.remove(channel)

    @property
    def channels(self) -> list[NotificationChannel]:
        with self._lock:
            return list(self._channels)

    def notify(self, signal: Signal) -> int:
        """Send ``signal`` to every channel; returns the number of successful deliveries."""
        delivered = 0
        for channel in self.channels:
            try:
                channel.send(signal)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - a broken channel must not stop the others
                self.failures[channel.name] = self.failures.get(channel.name, 0) + 1
                logger.error("Notification via %s failed: %s", channel.name, exc)
        return delivered


def build_notifier(cfg: NotificationConfig) -> Notifier:
    """Create the headless channels described by the configuration."""
    notifier = Notifier([LogChannel()])
    if cfg.webhook_url:
        notifier.add_channel(WebhookChannel(cfg.webhook_url))
    if cfg.email.enabled:
        try:
            notifier.add_channel(EmailChannel(cfg.email))
        except ValueError as exc:
            logger.error("Email notifications disabled: %s", exc)
    return notifier
