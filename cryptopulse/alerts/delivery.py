"""Alert delivery — getting a signal out of the dashboard and onto a phone.

An alert that only exists inside a web page nobody is watching is not an alert.
`CP_ALERT_WEBHOOK_URL` existed as a setting long before anything sent to it; this
module is what makes that setting real.

Four rules, each of which exists because the obvious implementation gets it
wrong:

1. **The URL is a credential and is never logged.** A Discord or Slack webhook
   URL contains its own bearer token — anyone holding the string can post as
   you. It never appears in a log line, an error message, an API response or an
   exception trace. `redacted()` is the only representation that leaves this
   module, and it keeps the host and nothing else.

2. **A delivery failure never breaks a scan.** The scanner's job is to scan. A
   webhook that 500s, hangs, or points at a host that no longer resolves must
   degrade to a recorded failure, not a lost scan cycle. Every path returns a
   `DeliveryReport` instead of raising.

3. **A failure is never silent.** The last outcome — including the reason and
   the HTTP status — is surfaced on `/api/health`, so a webhook that quietly
   stopped working is visible rather than mistaken for a quiet market.

4. **Alert payloads carry the same caveats as the dashboard.** A notification is
   the one place a user reads a score with no surrounding context, so the score
   goes out labelled `84/100`, never `84%`, and the synthetic-data warning
   travels with it. A DEMO alert that looks like a live one is worse than no
   alert.

Three formats are supported, chosen from the URL so there is nothing to
configure: Discord, Slack, and a generic JSON POST that carries the full alert
dictionary for anything else.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from cryptopulse.core.logging import get_logger

log = get_logger("alerts.delivery")

__all__ = ["DeliveryReport", "AlertDelivery", "redacted", "webhook_flavour"]

# A notification should be readable at a glance. Beyond this the important line
# scrolls off a phone screen.
MAX_ALERTS_PER_MESSAGE = 5
MAX_REASONS_PER_ALERT = 3

_LEVEL_EMOJI = {"CRITICAL_SETUP": "🔴", "HIGH": "🟠", "WATCH": "🟡", "INFO": "🔵"}


def redacted(url: str | None) -> str | None:
    """The only representation of a webhook URL allowed to leave this module.

    Keeps the host so a misconfiguration is diagnosable, drops the path and
    query, which is where the secret lives.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.hostname:
        return "<unparseable url>"
    return f"{parts.scheme}://{parts.hostname}/…"


def webhook_flavour(url: str) -> str:
    """Which payload shape the endpoint expects, inferred from its host."""
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("discord.com") or host.endswith("discordapp.com"):
        return "discord"
    if host.endswith("slack.com"):
        return "slack"
    return "generic"


@dataclass(slots=True)
class DeliveryReport:
    """What happened on the last attempt. Never contains the URL."""

    attempted: bool = False
    delivered: int = 0
    ok: bool = False
    status_code: int | None = None
    error: str | None = None
    target: str | None = None  # redacted host only
    flavour: str | None = None

    def to_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "delivered": self.delivered,
            "ok": self.ok,
            "status_code": self.status_code,
            "error": self.error,
            "target": self.target,
            "format": self.flavour,
        }


@dataclass(slots=True)
class AlertDelivery:
    """Posts alerts to a configured webhook. Silent no-op when unconfigured."""

    webhook_url: str | None = None
    timeout: float = 10.0
    synthetic: bool = False
    last: DeliveryReport = field(default_factory=DeliveryReport)

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    async def send(self, alerts: list) -> DeliveryReport:
        """Deliver `alerts`. Returns a report; never raises, never logs the URL."""
        if not self.webhook_url or not alerts:
            self.last = DeliveryReport(attempted=False, target=redacted(self.webhook_url))
            return self.last

        flavour = webhook_flavour(self.webhook_url)
        target = redacted(self.webhook_url)
        batch = alerts[:MAX_ALERTS_PER_MESSAGE]
        payload = self._payload(batch, flavour)

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.webhook_url, json=payload)
        except Exception as exc:
            # str(exc) on an httpx error can embed the request URL, so the
            # exception type is reported and the message deliberately is not.
            self.last = DeliveryReport(
                attempted=True, ok=False, error=f"{type(exc).__name__} while posting to the webhook",
                target=target, flavour=flavour,
            )
            log.warning("alert_delivery_failed", target=target, error=type(exc).__name__)
            return self.last

        ok = 200 <= resp.status_code < 300
        self.last = DeliveryReport(
            attempted=True,
            delivered=len(batch) if ok else 0,
            ok=ok,
            status_code=resp.status_code,
            error=None if ok else f"webhook returned HTTP {resp.status_code}",
            target=target,
            flavour=flavour,
        )
        log.info(
            "alert_delivery" if ok else "alert_delivery_rejected",
            target=target, status=resp.status_code, alerts=len(batch),
        )
        return self.last

    # ------------------------------------------------------------- payloads #

    def _payload(self, alerts: list, flavour: str) -> dict:
        if flavour == "discord":
            return {"content": self._text(alerts)[:1900]}
        if flavour == "slack":
            return {"text": self._text(alerts)[:2900]}
        # Generic: the full structured alert, so a downstream consumer is not
        # forced to parse prose back into numbers.
        return {
            "source": "CRYPTO PULSE AI",
            "synthetic_data": self.synthetic,
            "warning": _SYNTHETIC_WARNING if self.synthetic else None,
            "disclaimer": _DISCLAIMER,
            "count": len(alerts),
            "alerts": [a.to_dict() for a in alerts],
        }

    def _text(self, alerts: list) -> str:
        lines: list[str] = []
        if self.synthetic:
            # First line, before anything a reader might act on.
            lines += [f"⚠️ {_SYNTHETIC_WARNING}", ""]
        lines.append(f"CRYPTO PULSE AI — {len(alerts)} signal(s)")
        for a in alerts:
            emoji = _LEVEL_EMOJI.get(a.level.value, "•")
            # "84/100", never "84%": the score is a ranking with no calibration.
            lines += [
                "",
                f"{emoji} {a.symbol} — {a.level.value} — {a.final_score:.0f}/100",
                f"   {a.headline}",
                f"   price {a.price:.8g} · {a.state} · liquidity {a.liquidity} "
                f"· maturity {a.pump_maturity:.0f}/100 · confidence {a.data_confidence:.0f}/100",
            ]
            lines += [f"   + {w}" for w in a.why[:MAX_REASONS_PER_ALERT]]
            lines += [f"   ! {r}" for r in a.risks[:MAX_REASONS_PER_ALERT]]
            if a.invalidation:
                lines.append(f"   ✗ invalidated if: {a.invalidation}")
        lines += ["", _DISCLAIMER]
        return "\n".join(lines)


_DISCLAIMER = (
    "Les scores sont des classements de 0 à 100, pas des probabilités, et ne sont pas "
    "des conseils. Ce logiciel ne passe jamais aucun ordre."
)
_SYNTHETIC_WARNING = (
    "DÉMO — ces alertes proviennent de bougies SYNTHÉTIQUES générées, pas d'un vrai marché."
)


async def send_alerts(delivery: AlertDelivery, alerts: list) -> DeliveryReport:
    """Fire-and-report helper that also survives cancellation of the caller."""
    try:
        return await delivery.send(alerts)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # the send path is already guarded; this is belt-and-braces
        log.error("alert_delivery_unexpected", error=type(exc).__name__)
        return DeliveryReport(attempted=True, ok=False, error=f"unexpected {type(exc).__name__}")
