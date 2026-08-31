"""Alert delivery. An alert nobody sees is not an alert.

This is the piece that makes the radar autonomous: without it the scanner
computes a CRITICAL_SETUP at 03:14, writes it to a database, and nobody hears
about it until they next open the dashboard.

Three rules hold for every channel here:

1. **Delivery never breaks the scan.** Every failure is caught, logged and
   reported as a per-channel outcome. A dead webhook is not allowed to stop the
   loop that produced the alert, and it is never retried indefinitely.
2. **Secrets are never logged, never echoed, never put in an error message.**
   What gets logged is the channel name and the failure class. A misconfigured
   channel is reported by naming the *setting* that is missing, never its value.
3. **A channel that cannot work says so at construction.** `describe()` reports
   configured / not-configured per channel, so `cryptopulse radar` can print at
   startup where alerts will actually go — rather than discovering at 03:14 that
   they went nowhere.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from cryptopulse.alerts.engine import Alert
from cryptopulse.config.settings import AlertSettings
from cryptopulse.core.logging import get_logger

log = get_logger("alerts.notify")

__all__ = [
    "Notifier",
    "ConsoleNotifier",
    "JsonlNotifier",
    "WebhookNotifier",
    "TelegramNotifier",
    "DiscordNotifier",
    "NotifierHub",
    "DeliveryResult",
]

# Platform message caps. Exceeding them is a 400, not a truncation, so trim here.
TELEGRAM_MAX_CHARS = 4096
DISCORD_MAX_CHARS = 2000


@dataclass(slots=True)
class DeliveryResult:
    channel: str
    delivered: int
    failed: int
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "channel": self.channel,
            "delivered": self.delivered,
            "failed": self.failed,
            "detail": self.detail,
        }


class Notifier(ABC):
    name: str

    @property
    def configured(self) -> bool:
        return True

    @property
    def missing_setting(self) -> str | None:
        """Which setting is absent, when the channel cannot run. Never a value."""
        return None

    @abstractmethod
    async def send(self, alerts: list[Alert]) -> DeliveryResult: ...

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


# --------------------------------------------------------------------------- #
# Local channels
# --------------------------------------------------------------------------- #


class ConsoleNotifier(Notifier):
    """Prints to stdout. The default, because it cannot be misconfigured."""

    name = "console"

    async def send(self, alerts: list[Alert]) -> DeliveryResult:
        for a in alerts:
            print("\n" + "=" * 60)
            print(a.format_text())
        return DeliveryResult(channel=self.name, delivered=len(alerts), failed=0)


class JsonlNotifier(Notifier):
    """Appends one JSON object per alert to a file.

    Append-only and line-delimited on purpose: it survives a crash mid-write with
    at most one truncated line, it can be tailed while the radar runs, and it is
    a record independent of the database — if the DB is wedged, the alerts are
    still on disk.
    """

    name = "jsonl"

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    async def send(self, alerts: list[Alert]) -> DeliveryResult:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                for a in alerts:
                    fh.write(json.dumps(a.to_dict(), default=str) + "\n")
            return DeliveryResult(channel=self.name, delivered=len(alerts), failed=0, detail=str(self.path))
        except OSError as exc:
            log.error("jsonl_write_failed", error=str(exc)[:200])
            return DeliveryResult(
                channel=self.name, delivered=0, failed=len(alerts), detail=f"{type(exc).__name__}"
            )


# --------------------------------------------------------------------------- #
# Network channels
# --------------------------------------------------------------------------- #


class _HttpNotifier(Notifier):
    """Shared POST plumbing. Subclasses supply the URL and the body."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout

    async def _post(self, url: str, payload: dict) -> tuple[bool, str]:
        """POST once. Returns (ok, detail). Detail never contains the URL."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                # The body can echo the token back on some platforms, so only the
                # status code is kept.
                return False, f"HTTP {resp.status_code}"
            return True, f"HTTP {resp.status_code}"
        except Exception as exc:
            return False, type(exc).__name__


class WebhookNotifier(_HttpNotifier):
    """Generic JSON POST — Slack-compatible endpoints, n8n, your own service.

    Sends the full alert dictionary rather than a rendered string, so whatever
    receives it can route on score, state or level without re-parsing prose.
    """

    name = "webhook"

    def __init__(self, url: str | None, timeout: float) -> None:
        super().__init__(timeout)
        self.url = url

    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def missing_setting(self) -> str | None:
        return None if self.url else "CP_ALERT_WEBHOOK_URL"

    async def send(self, alerts: list[Alert]) -> DeliveryResult:
        if not self.url:
            return DeliveryResult(self.name, 0, len(alerts), "not configured")
        delivered = failed = 0
        detail = None
        for a in alerts:
            ok, why = await self._post(self.url, {"source": "CRYPTO PULSE AI", "alert": a.to_dict()})
            if ok:
                delivered += 1
            else:
                failed += 1
                detail = why
        if failed:
            log.warning("webhook_delivery_failed", failed=failed, detail=detail)
        return DeliveryResult(self.name, delivered, failed, detail)


class TelegramNotifier(_HttpNotifier):
    """Telegram bot messages — the channel that actually reaches a phone."""

    name = "telegram"

    def __init__(self, bot_token: str | None, chat_id: str | None, timeout: float) -> None:
        super().__init__(timeout)
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    @property
    def missing_setting(self) -> str | None:
        if not self.bot_token:
            return "CP_ALERT_TELEGRAM_BOT_TOKEN"
        if not self.chat_id:
            return "CP_ALERT_TELEGRAM_CHAT_ID"
        return None

    async def send(self, alerts: list[Alert]) -> DeliveryResult:
        if not self.configured:
            return DeliveryResult(self.name, 0, len(alerts), "not configured")
        # The token is part of the path, which is why no code path may log a URL.
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        delivered = failed = 0
        detail = None
        for a in alerts:
            body = {
                "chat_id": self.chat_id,
                "text": a.format_text()[:TELEGRAM_MAX_CHARS],
                "disable_web_page_preview": True,
            }
            ok, why = await self._post(url, body)
            if ok:
                delivered += 1
            else:
                failed += 1
                detail = why
        if failed:
            log.warning("telegram_delivery_failed", failed=failed, detail=detail)
        return DeliveryResult(self.name, delivered, failed, detail)


class DiscordNotifier(_HttpNotifier):
    name = "discord"

    def __init__(self, webhook_url: str | None, timeout: float) -> None:
        super().__init__(timeout)
        self.webhook_url = webhook_url

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    @property
    def missing_setting(self) -> str | None:
        return None if self.webhook_url else "CP_ALERT_DISCORD_WEBHOOK_URL"

    async def send(self, alerts: list[Alert]) -> DeliveryResult:
        if not self.webhook_url:
            return DeliveryResult(self.name, 0, len(alerts), "not configured")
        delivered = failed = 0
        detail = None
        for a in alerts:
            content = f"```\n{a.format_text()}\n```"[:DISCORD_MAX_CHARS]
            ok, why = await self._post(self.webhook_url, {"content": content})
            if ok:
                delivered += 1
            else:
                failed += 1
                detail = why
        if failed:
            log.warning("discord_delivery_failed", failed=failed, detail=detail)
        return DeliveryResult(self.name, delivered, failed, detail)


# --------------------------------------------------------------------------- #
# Fan-out
# --------------------------------------------------------------------------- #


_BUILDERS = {"console", "jsonl", "webhook", "telegram", "discord"}


class NotifierHub:
    """Fans one batch of alerts out to every configured channel, independently."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers
        self.last_results: list[DeliveryResult] = []

    @classmethod
    def from_settings(cls, cfg: AlertSettings) -> NotifierHub:
        built: list[Notifier] = []
        for name in cfg.channels:
            name = name.strip().lower()
            if name == "console":
                built.append(ConsoleNotifier())
            elif name == "jsonl":
                built.append(JsonlNotifier(cfg.jsonl_path))
            elif name == "webhook":
                built.append(WebhookNotifier(cfg.webhook_url, cfg.delivery_timeout_seconds))
            elif name == "telegram":
                built.append(
                    TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.delivery_timeout_seconds)
                )
            elif name == "discord":
                built.append(DiscordNotifier(cfg.discord_webhook_url, cfg.delivery_timeout_seconds))
            elif name:
                log.warning("unknown_alert_channel", channel=name, known=sorted(_BUILDERS))
        return cls(built)

    def describe(self) -> list[dict]:
        """Where alerts will go, and which channels are configured but inert."""
        return [
            {
                "channel": n.name,
                "configured": n.configured,
                "missing_setting": n.missing_setting,
            }
            for n in self.notifiers
        ]

    async def dispatch(self, alerts: list[Alert]) -> list[DeliveryResult]:
        """Deliver to every channel. Never raises."""
        if not alerts or not self.notifiers:
            self.last_results = []
            return []

        import asyncio

        async def one(n: Notifier) -> DeliveryResult:
            try:
                return await n.send(alerts)
            except Exception as exc:  # a channel must never take the loop down
                log.error("notifier_crashed", channel=n.name, error=type(exc).__name__)
                return DeliveryResult(n.name, 0, len(alerts), f"crashed: {type(exc).__name__}")

        results = await asyncio.gather(*(one(n) for n in self.notifiers))
        self.last_results = list(results)
        log.info(
            "alerts_dispatched",
            alerts=len(alerts),
            channels={r.channel: r.delivered for r in results},
        )
        return self.last_results

    async def close(self) -> None:
        for n in self.notifiers:
            try:
                await n.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.warning("notifier_close_failed", channel=n.name)
