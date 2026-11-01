"""Outbound e-mail: SMTP in production, a local "outbox" directory for development and tests."""

from __future__ import annotations

import itertools
import logging
import re
import smtplib
import threading
import time
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from ..config import EmailConfig

logger = logging.getLogger(__name__)


class Mailer:
    """Sends e-mail through the configured transport.

    * ``transport = "smtp"`` (default): uses ``notifications.email`` (host, port, credentials, TLS).
    * ``transport = "outbox"``: writes ``.eml`` files into ``<data_dir>/outbox`` - lets you try the
      sign-up / verification flow without a mail server.
    """

    def __init__(self, cfg: EmailConfig, outbox_dir: Path | None = None,
                 smtp_factory: Callable[[], Any] | None = None) -> None:
        self.cfg = cfg
        self.outbox_dir = outbox_dir
        self._smtp_factory = smtp_factory or (lambda: smtplib.SMTP(cfg.host, cfg.port, timeout=15))
        self._lock = threading.Lock()
        self._counter = itertools.count(1)

    @property
    def configured(self) -> bool:
        if self.cfg.transport == "outbox":
            return self.outbox_dir is not None
        return bool(self.cfg.host)

    def send(self, to: str, subject: str, body: str) -> None:
        if not self.configured:
            raise RuntimeError("e-mail delivery is not configured (notifications.email.host / transport)")
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.cfg.sender or self.cfg.username or "trading-bot@localhost"
        msg["To"] = to
        # 8bit keeps long URLs on one line (quoted-printable would soft-wrap them), which matters for the outbox files.
        msg.set_content(body, cte="8bit")
        if self.cfg.transport == "outbox":
            assert self.outbox_dir is not None
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", to)
            with self._lock:
                path = self.outbox_dir / f"{int(time.time() * 1000)}-{next(self._counter):04d}-{safe}.eml"
                path.write_text(msg.as_string(), encoding="utf-8")
            logger.info("E-mail to %s written to %s", to, path)
            return
        with self._smtp_factory() as smtp:
            if self.cfg.use_tls:
                smtp.starttls()
            if self.cfg.username:
                smtp.login(self.cfg.username, self.cfg.password)
            smtp.send_message(msg)
        logger.info("E-mail sent to %s (%s)", to, subject)
