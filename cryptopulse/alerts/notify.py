"""Android notifications — getting an alert onto the phone's lock screen.

WHY THIS EXISTS SEPARATELY FROM `delivery.py`

`alerts/delivery.py` posts to a webhook. That works from anywhere, but it needs a
Discord or Slack account, it needs a URL that is a credential, and it routes a
private signal through a third party. On a phone running the scanner in Termux
none of that is necessary: the device the alert is *for* is the device producing
it, and Android can be told directly.

So this is a second, independent delivery channel rather than a replacement.
Both may be on, either may be off, and neither can break the other.

WHAT IT ACTUALLY TALKS TO

`termux-notification`, the CLI shipped by the **Termux:API** app. Two separate
installs are required and people routinely do only the first:

  * the Termux:API *app*, from the same source as Termux itself, and
  * the `termux-api` *package*, via `pkg install termux-api`.

With the package but not the app, the command exists and hangs. With the app but
not the package, the command does not exist. `availability()` distinguishes those
cases and says which one is missing, because "notifications do not work" with no
further detail is the least useful possible message.

THREE RULES

1. **A notification failure costs a notification, never a scan.** Same rule as
   the webhook, for the same reason: an alert channel that can break the scanner
   is worse than no alert channel. Every path returns a report rather than
   raising, and the outcome is surfaced on `/api/health`.

2. **Escalation only.** The alert engine already deduplicates and applies a
   cooldown. What it does not do is stop a symbol sitting at WATCH from
   notifying every cycle for an hour. Here a symbol notifies again only when its
   level goes *up*. A phone that buzzes for the same thing repeatedly gets its
   notifications turned off, and then the CRITICAL one is missed too — the
   anti-spam rule exists to protect the loud alert, not to be polite.

3. **A DEMO notification says DEMO in its title.** A notification is read on a
   lock screen with no surrounding page, which makes it the single most likely
   place for generated data to be mistaken for real. The warning goes in the
   title, not the body, because the body is what gets truncated.

WHAT HAS AND HAS NOT BEEN VERIFIED

The gate logic, the argument construction, the timeout, and every failure path
are covered by tests that run a stand-in binary and inspect its argv. What cannot
be verified from this environment is that Termux's real `termux-notification`
accepts these flags — this is not an Android device, and no test here has ever
spoken to one. The flags are the long-standing documented ones, and
`cryptopulse notify --test` sends a real notification so the round trip can be
confirmed on the phone in one command.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from cryptopulse.core.logging import get_logger

log = get_logger("alerts.notify")

__all__ = [
    "Availability",
    "NotificationReport",
    "NotificationProvider",
    "TermuxNotifier",
    "UnavailableNotifier",
    "NotificationGate",
    "build_notifier",
    "notify_alerts",
]

# Ordered weakest to strongest. A symbol re-notifies only on a move up this list.
LEVEL_ORDER: tuple[str, ...] = ("INFO", "WATCH", "HIGH", "CRITICAL_SETUP")

_LEVEL_EMOJI = {"CRITICAL_SETUP": "🔴", "HIGH": "🟠", "WATCH": "🟡", "INFO": "🔵"}
# Android priorities. Only the top two make a sound; WATCH deliberately does not.
_LEVEL_PRIORITY = {"CRITICAL_SETUP": "max", "HIGH": "high", "WATCH": "low", "INFO": "min"}

# Beyond this the notification is a wall of text on a lock screen.
MAX_ALERTS_PER_NOTIFICATION = 4
# The CLI can hang when the Termux:API app is missing; this is what turns that
# into a reported failure instead of a stalled scan cycle.
COMMAND_TIMEOUT_SECONDS = 8.0

_SYNTHETIC_TITLE = "DÉMO — chiffres générés"


@dataclass(slots=True)
class Availability:
    """Whether notifications can be sent, and if not, exactly what is missing."""

    available: bool
    reason: str
    fix: str | None = None

    def to_dict(self) -> dict:
        return {"available": self.available, "reason": self.reason, "fix": self.fix}


@dataclass(slots=True)
class NotificationReport:
    attempted: bool = False
    sent: int = 0
    suppressed: int = 0
    ok: bool = True
    error: str | None = None
    channel: str = "none"
    # Why each suppressed symbol was held back, so silence is explainable.
    suppressed_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "sent": self.sent,
            "suppressed": self.suppressed,
            "ok": self.ok,
            "error": self.error,
            "channel": self.channel,
            "suppressed_reasons": self.suppressed_reasons[:6],
        }


class NotificationProvider(ABC):
    """A way of putting an alert in front of the user on this device."""

    name: str = "abstract"

    @abstractmethod
    def availability(self) -> Availability:
        """Can this channel deliver right now, and if not, what would fix it?"""

    @abstractmethod
    async def send(self, alerts: list, *, synthetic: bool) -> NotificationReport:
        """Deliver. Returns a report; never raises."""


class UnavailableNotifier(NotificationProvider):
    """The honest no-op: states why nothing will be delivered.

    Deliberately not a silent null object. A channel that is off because the
    user is on a laptop, and one that is off because they installed half of
    Termux:API, look identical from the outside unless something says so.
    """

    name = "unavailable"

    def __init__(self, reason: str, fix: str | None = None) -> None:
        self._availability = Availability(available=False, reason=reason, fix=fix)

    def availability(self) -> Availability:
        return self._availability

    async def send(self, alerts: list, *, synthetic: bool) -> NotificationReport:
        return NotificationReport(
            attempted=False, ok=True, channel=self.name,
            error=self._availability.reason,
        )


class TermuxNotifier(NotificationProvider):
    """Android notifications through the Termux:API CLI."""

    name = "termux"

    def __init__(self, *, binary: str = "termux-notification", timeout: float = COMMAND_TIMEOUT_SECONDS) -> None:
        self.binary = binary
        self.timeout = timeout

    def availability(self) -> Availability:
        if shutil.which(self.binary) is None:
            return Availability(
                available=False,
                reason=f"{self.binary} not found — the termux-api package is not installed",
                fix="pkg install termux-api",
            )
        return Availability(
            available=True,
            reason="termux-notification is on PATH",
            # Stated even in the success case: the package being present says
            # nothing about the companion app, and that is the failure people
            # actually hit. The command hangs rather than erroring in that case,
            # which the timeout below turns into a reported failure.
            fix="If nothing appears on screen, install the Termux:API app from the same source as Termux.",
        )

    async def send(self, alerts: list, *, synthetic: bool) -> NotificationReport:
        if not alerts:
            return NotificationReport(attempted=False, channel=self.name)

        availability = self.availability()
        if not availability.available:
            return NotificationReport(
                attempted=False, ok=False, channel=self.name, error=availability.reason
            )

        batch = alerts[:MAX_ALERTS_PER_NOTIFICATION]
        top = max(batch, key=lambda a: _rank(_level_of(a)))
        args = self._args(batch, top, synthetic=synthetic)

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                # Its own process group, so a hung command can be killed
                # *including anything it spawned*. Signalling only the direct
                # child leaves grandchildren alive holding the stderr pipe, and
                # the cleanup below then blocks until they exit on their own —
                # which for a command that hangs is never.
                start_new_session=True,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except TimeoutError:
            # The signature failure of "termux-api package installed, Termux:API
            # app missing". Named explicitly because the generic message would
            # send someone looking at their network.
            #
            # The hung child must be killed and reaped here. Timing out only
            # abandons the wait: without this the process survives, and since
            # this runs once per scan cycle it would accumulate one stuck
            # process per minute for as long as the scanner is up.
            await _terminate(proc)
            log.warning("notification_timeout", channel=self.name)
            return NotificationReport(
                attempted=True, ok=False, channel=self.name,
                error="the command hung — this usually means the Termux:API app is not installed",
            )
        except Exception as exc:
            await _terminate(proc)
            log.warning("notification_failed", channel=self.name, error=type(exc).__name__)
            return NotificationReport(
                attempted=True, ok=False, channel=self.name,
                error=f"{type(exc).__name__} while running {self.binary}",
            )

        if proc.returncode != 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip()[:160]
            log.warning("notification_rejected", channel=self.name, code=proc.returncode)
            return NotificationReport(
                attempted=True, ok=False, channel=self.name,
                error=f"{self.binary} exited {proc.returncode}" + (f": {detail}" if detail else ""),
            )

        log.info("notification_sent", channel=self.name, alerts=len(batch))
        return NotificationReport(attempted=True, sent=len(batch), ok=True, channel=self.name)

    # ------------------------------------------------------------ arguments #

    def _args(self, alerts: list, top, *, synthetic: bool) -> list[str]:
        level = _level_of(top)
        title = self._title(alerts, top, synthetic=synthetic)

        args = [
            self.binary,
            "--title", title,
            "--content", self._content(alerts),
            # A stable id so a new batch replaces the previous notification
            # rather than stacking. Ten unread cards is how a user learns to
            # swipe the whole group away without reading any of them.
            "--id", "cryptopulse-alerts",
            "--group", "cryptopulse",
            "--priority", _LEVEL_PRIORITY.get(level, "low"),
        ]
        # Only the top two levels are allowed to make noise. A WATCH that
        # vibrates at 3am is how the CRITICAL one ends up muted.
        if level in ("HIGH", "CRITICAL_SETUP"):
            args.append("--sound")
        if level == "CRITICAL_SETUP":
            args += ["--vibrate", "300,200,300"]
        return args

    def _title(self, alerts: list, top, *, synthetic: bool) -> str:
        # The warning goes in the title because the body is what Android
        # truncates on a lock screen.
        prefix = f"{_SYNTHETIC_TITLE} · " if synthetic else ""
        emoji = _LEVEL_EMOJI.get(_level_of(top), "•")
        symbol = getattr(top, "symbol", "?")
        if len(alerts) == 1:
            return f"{prefix}{emoji} {symbol}"
        return f"{prefix}{emoji} {symbol} +{len(alerts) - 1}"

    def _content(self, alerts: list) -> str:
        lines = []
        for a in alerts:
            score = getattr(a, "final_score", None)
            # "84/100", never "84%". Nothing here is calibrated, and a lock
            # screen is the last place a caveat would fit.
            score_text = f" {score:.0f}/100" if isinstance(score, int | float) else ""
            emoji = _LEVEL_EMOJI.get(_level_of(a), "•")
            lines.append(
                f"{emoji} {getattr(a, 'symbol', '?')}{score_text} · {getattr(a, 'headline', '')}"
            )
        return "\n".join(lines)[:900]


class NotificationGate:
    """Escalation-only anti-spam, on top of the alert engine's own cooldown.

    The engine already stops the identical alert repeating. This stops a symbol
    that is legitimately re-alerting at the same level from buzzing the phone
    every cycle: it passes on the first notification, and afterwards only when
    the level rises.

    A drop back down does not reset the memory to nothing — it lowers the bar to
    the new level, so a symbol that falls from HIGH to WATCH and climbs back to
    HIGH will notify again. Requiring a strict all-time high would silence a
    symbol oscillating around a threshold, which is exactly when it matters.
    """

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def filter(self, alerts: list) -> tuple[list, list[str]]:
        """Returns (alerts worth a notification, reasons the rest were held)."""
        passed: list = []
        held: list[str] = []
        for a in alerts:
            symbol = getattr(a, "symbol", "?")
            level = _level_of(a)
            previous = self._seen.get(symbol)
            if previous is None or _rank(level) > _rank(previous):
                passed.append(a)
                self._seen[symbol] = level
            else:
                held.append(f"{symbol}: already notified at {previous}, still {level}")
                # Track the current level so a later climb is detected against
                # where it is now, not against its all-time peak.
                self._seen[symbol] = level
        return passed, held

    def forget(self, symbol: str) -> None:
        self._seen.pop(symbol.upper(), None)

    def reset(self) -> None:
        self._seen.clear()


def build_notifier(*, enabled: bool = True, binary: str = "termux-notification") -> NotificationProvider:
    """Pick a channel, or return one that explains why there isn't one."""
    if not enabled:
        return UnavailableNotifier(
            reason="Android notifications are switched off (CP_ALERT_ANDROID_NOTIFICATIONS=false)",
        )
    if shutil.which(binary) is not None:
        return TermuxNotifier(binary=binary)
    return UnavailableNotifier(
        reason=f"{binary} not found — this device is not running Termux with termux-api",
        fix="On Android: pkg install termux-api, and install the Termux:API app.",
    )


async def notify_alerts(
    provider: NotificationProvider, gate: NotificationGate, alerts: list, *, synthetic: bool
) -> NotificationReport:
    """Gate, then deliver. Never raises — the scan owns the cycle, not this."""
    if not alerts:
        return NotificationReport(attempted=False, channel=provider.name)

    passed, held = gate.filter(alerts)
    if not passed:
        return NotificationReport(
            attempted=False, suppressed=len(held), ok=True,
            channel=provider.name, suppressed_reasons=held,
        )

    try:
        report = await provider.send(passed, synthetic=synthetic)
    except Exception as exc:  # a provider that raises is a bug, not a scan failure
        log.error("notification_provider_raised", channel=provider.name, error=type(exc).__name__)
        return NotificationReport(
            attempted=True, ok=False, channel=provider.name,
            error=f"{type(exc).__name__} raised by the notification provider",
        )

    report.suppressed = len(held)
    report.suppressed_reasons = held
    return report


async def _terminate(proc) -> None:
    """Kill and reap a child that is not going to finish, and everything it spawned.

    The whole group is signalled, not just the direct child. Found by running
    the timeout test: killing only the child left its `sleep` grandchild holding
    the stderr pipe, and `wait()` then blocked for the full sleep — turning a
    0.4 second timeout into a 30 second stall inside the scan cycle.

    Reaping is bounded too. A process that ignores SIGKILL cannot exist, but a
    pipe that never closes can, and waiting forever to clean up a notification
    would be a worse bug than the one being cleaned up.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Already gone, or not ours to signal. Fall back to the direct child.
        try:
            proc.kill()
        except ProcessLookupError:
            return
    except Exception as exc:
        log.warning("notification_cleanup_failed", error=type(exc).__name__)
        return

    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except Exception as exc:
        # Includes TimeoutError. Cleanup is best effort by design: failing to
        # reap a notification process must not itself become an error path.
        log.warning("notification_cleanup_incomplete", error=type(exc).__name__)


def _level_of(alert) -> str:
    level = getattr(alert, "level", None)
    return getattr(level, "value", level) or "INFO"


def _rank(level: str) -> int:
    try:
        return LEVEL_ORDER.index(level)
    except ValueError:
        return 0
