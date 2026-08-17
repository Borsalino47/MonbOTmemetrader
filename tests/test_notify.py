"""Android notifications.

WHAT THESE TESTS CAN AND CANNOT PROVE

They run a stand-in `termux-notification` that records its argv, so the argument
construction, the timeout, the non-zero exit path and the gate logic are all
exercised for real — not mocked at the boundary, but actually executed as a
subprocess.

What they cannot prove is that Termux's own binary accepts these flags. This is
not an Android device and nothing here has ever spoken to one. That gap is why
`cryptopulse notify` exists: it sends a real notification so the round trip can
be confirmed on the phone in one command, and it prints the availability
diagnosis first so a failure names which of the two Termux:API installs is
missing.
"""

from __future__ import annotations

import asyncio
import os
import stat

import pytest

from cryptopulse.alerts.engine import Alert, AlertLevel
from cryptopulse.alerts.notify import (
    NotificationGate,
    TermuxNotifier,
    UnavailableNotifier,
    build_notifier,
    notify_alerts,
)


def _alert(symbol: str, level: AlertLevel, score: float = 80.0) -> Alert:
    return Alert(
        symbol=symbol,
        level=level,
        headline=f"{symbol} moved",
        timestamp_ms=1_700_000_000_000,
        final_score=score,
        pump_maturity=20.0,
        data_confidence=90.0,
        safety=100.0,
        liquidity="GOOD",
        state="BREAKOUT",
        price=1.0,
        score_acceleration=None,
    )


def _stub(tmp_path, script: str, name: str = "termux-notification") -> str:
    """A real executable on disk, so the subprocess path is genuinely exercised."""
    path = tmp_path / name
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _recorder(tmp_path) -> tuple[str, object]:
    """A stub that writes its arguments out, one per line."""
    out = tmp_path / "argv.txt"
    binary = _stub(
        tmp_path,
        "#!/bin/sh\n"
        f'for a in "$@"; do printf "%s\\n" "$a"; done > "{out}"\n'
        "exit 0\n",
    )
    return binary, out


# --------------------------------------------------------------------------- #
# Availability — the failure people actually hit
# --------------------------------------------------------------------------- #


def test_a_missing_binary_is_reported_rather_than_failing_at_send_time():
    notifier = build_notifier(binary="definitely-not-a-real-command")
    availability = notifier.availability()

    assert availability.available is False
    assert "introuvable" in availability.reason
    assert availability.fix and "termux-api" in availability.fix


def test_switching_notifications_off_says_so_rather_than_looking_broken():
    """'Off by choice' and 'off because half of Termux:API is missing' look
    identical from the outside unless something distinguishes them."""
    notifier = build_notifier(enabled=False)
    assert notifier.availability().available is False
    assert "désactivées" in notifier.availability().reason


def test_an_available_channel_still_warns_about_the_companion_app(tmp_path):
    """The package being on PATH says nothing about the Termux:API app, and
    that combination is the one that hangs rather than erroring."""
    binary, _ = _recorder(tmp_path)
    availability = TermuxNotifier(binary=binary).availability()

    assert availability.available is True
    assert availability.fix and "Termux:API" in availability.fix


@pytest.mark.asyncio
async def test_an_unavailable_channel_never_pretends_to_have_sent():
    report = await UnavailableNotifier("no channel here").send(
        [_alert("BTCUSDT", AlertLevel.HIGH)], synthetic=False
    )
    assert report.attempted is False
    assert report.sent == 0
    assert report.error == "no channel here"


# --------------------------------------------------------------------------- #
# What actually reaches the command line
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_notification_carries_the_symbol_the_score_and_no_percent_sign(tmp_path):
    binary, out = _recorder(tmp_path)
    report = await TermuxNotifier(binary=binary).send(
        [_alert("SOLUSDT", AlertLevel.HIGH, score=84.0)], synthetic=False
    )

    assert report.ok and report.sent == 1
    argv = out.read_text().splitlines()
    joined = "\n".join(argv)

    assert "SOLUSDT" in joined
    # A ranking, on a lock screen where a caveat would never fit.
    assert "84/100" in joined
    assert "%" not in joined


@pytest.mark.asyncio
async def test_a_demo_notification_says_demo_in_its_title_not_its_body(tmp_path):
    """The body is what Android truncates on a lock screen, which makes it the
    wrong place for the one warning that must survive."""
    binary, out = _recorder(tmp_path)
    await TermuxNotifier(binary=binary).send(
        [_alert("BTCUSDT", AlertLevel.CRITICAL_SETUP)], synthetic=True
    )

    argv = out.read_text().splitlines()
    title = argv[argv.index("--title") + 1]
    assert "DÉMO" in title


@pytest.mark.asyncio
async def test_only_the_loud_levels_are_allowed_to_make_a_sound(tmp_path):
    """A WATCH that vibrates at 3am is how the CRITICAL one ends up muted."""
    binary, out = _recorder(tmp_path)
    notifier = TermuxNotifier(binary=binary)

    await notifier.send([_alert("AAAUSDT", AlertLevel.WATCH)], synthetic=False)
    assert "--sound" not in out.read_text().splitlines()

    await notifier.send([_alert("BBBUSDT", AlertLevel.CRITICAL_SETUP)], synthetic=False)
    argv = out.read_text().splitlines()
    assert "--sound" in argv
    assert "--vibrate" in argv
    assert argv[argv.index("--priority") + 1] == "max"


@pytest.mark.asyncio
async def test_a_batch_replaces_the_previous_notification_rather_than_stacking(tmp_path):
    """Ten unread cards is how a user learns to swipe the whole group away
    without reading any of them."""
    binary, out = _recorder(tmp_path)
    await TermuxNotifier(binary=binary).send(
        [_alert("AUSDT", AlertLevel.HIGH), _alert("BUSDT", AlertLevel.WATCH)], synthetic=False
    )

    argv = out.read_text().splitlines()
    assert argv[argv.index("--id") + 1] == "cryptopulse-alerts"
    # The title reflects the loudest alert in the batch, not the first.
    assert "AUSDT" in argv[argv.index("--title") + 1]


# --------------------------------------------------------------------------- #
# Failure costs a notification, never a scan
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_command_that_hangs_is_named_as_the_missing_app(tmp_path):
    """The signature failure of 'package installed, companion app missing'. A
    generic timeout message would send someone looking at their network."""
    binary = _stub(tmp_path, "#!/bin/sh\nsleep 30\n")
    report = await TermuxNotifier(binary=binary, timeout=0.4).send(
        [_alert("BTCUSDT", AlertLevel.HIGH)], synthetic=False
    )

    assert report.ok is False
    assert report.sent == 0
    assert "Termux:API n'est pas installée" in report.error


@pytest.mark.asyncio
async def test_a_hung_command_is_killed_rather_than_left_running(tmp_path):
    """Timing out only abandons the wait. Without an explicit kill the process
    survives, and since this runs once per scan cycle it would accumulate one
    stuck process per minute for as long as the scanner is up."""
    binary = _stub(tmp_path, "#!/bin/sh\nsleep 30\n")
    notifier = TermuxNotifier(binary=binary, timeout=0.4)

    before = _sleep_count()
    await notifier.send([_alert("BTCUSDT", AlertLevel.HIGH)], synthetic=False)
    await asyncio.sleep(0.2)
    assert _sleep_count() <= before, "the timed-out child is still running"


def _sleep_count() -> int:
    """How many `sleep 30` children this process currently owns."""
    import subprocess

    out = subprocess.run(
        ["ps", "-o", "stat=,args=", "--ppid", str(os.getpid())],
        capture_output=True, text=True, check=False,
    ).stdout
    # A reaped child leaves no line at all; an unreaped one shows as Z (zombie).
    return sum(1 for line in out.splitlines() if "sleep 30" in line)


@pytest.mark.asyncio
async def test_a_nonzero_exit_is_reported_with_its_code(tmp_path):
    binary = _stub(tmp_path, "#!/bin/sh\necho 'bad flag' >&2\nexit 3\n")
    report = await TermuxNotifier(binary=binary).send(
        [_alert("BTCUSDT", AlertLevel.HIGH)], synthetic=False
    )

    assert report.ok is False
    assert "code 3" in report.error
    assert "bad flag" in report.error


@pytest.mark.asyncio
async def test_a_provider_that_raises_is_contained(tmp_path):
    """A bug in a notification channel must not cost the scan that produced the
    alert. Same rule as the webhook, for the same reason."""

    class Exploding(UnavailableNotifier):
        name = "exploding"

        async def send(self, alerts, *, synthetic):
            raise RuntimeError("boom")

    report = await notify_alerts(
        Exploding("test"), NotificationGate(), [_alert("BTCUSDT", AlertLevel.HIGH)],
        synthetic=False,
    )
    assert report.ok is False
    assert "RuntimeError" in report.error


# --------------------------------------------------------------------------- #
# The gate — escalation only
# --------------------------------------------------------------------------- #


def test_the_same_symbol_at_the_same_level_does_not_buzz_twice():
    gate = NotificationGate()
    first, _ = gate.filter([_alert("BTCUSDT", AlertLevel.HIGH)])
    second, held = gate.filter([_alert("BTCUSDT", AlertLevel.HIGH)])

    assert len(first) == 1
    assert second == []
    assert "déjà notifié en HIGH" in held[0]


def test_an_escalation_gets_through():
    gate = NotificationGate()
    gate.filter([_alert("BTCUSDT", AlertLevel.WATCH)])
    passed, _ = gate.filter([_alert("BTCUSDT", AlertLevel.CRITICAL_SETUP)])
    assert len(passed) == 1


def test_a_symbol_that_falls_and_climbs_back_notifies_again():
    """Requiring a strict all-time high would silence a symbol oscillating
    around a threshold — which is exactly when it matters."""
    gate = NotificationGate()
    gate.filter([_alert("BTCUSDT", AlertLevel.HIGH)])
    gate.filter([_alert("BTCUSDT", AlertLevel.WATCH)])       # dropped back
    passed, _ = gate.filter([_alert("BTCUSDT", AlertLevel.HIGH)])
    assert len(passed) == 1


def test_a_de_escalation_is_held_but_explained():
    gate = NotificationGate()
    gate.filter([_alert("BTCUSDT", AlertLevel.CRITICAL_SETUP)])
    passed, held = gate.filter([_alert("BTCUSDT", AlertLevel.WATCH)])

    assert passed == []
    assert held and "BTCUSDT" in held[0]


def test_different_symbols_do_not_gate_each_other():
    gate = NotificationGate()
    passed, _ = gate.filter([
        _alert("AUSDT", AlertLevel.HIGH),
        _alert("BUSDT", AlertLevel.HIGH),
    ])
    assert len(passed) == 2


@pytest.mark.asyncio
async def test_silence_from_the_gate_is_reported_rather_than_looking_like_calm(tmp_path):
    """A suppressed notification and an empty market must not look identical on
    /api/health."""
    binary, _ = _recorder(tmp_path)
    notifier = TermuxNotifier(binary=binary)
    gate = NotificationGate()

    await notify_alerts(notifier, gate, [_alert("BTCUSDT", AlertLevel.HIGH)], synthetic=False)
    report = await notify_alerts(
        notifier, gate, [_alert("BTCUSDT", AlertLevel.HIGH)], synthetic=False
    )

    assert report.sent == 0
    assert report.suppressed == 1
    assert report.suppressed_reasons
    assert report.ok is True, "a deliberate suppression is not a failure"


# --------------------------------------------------------------------------- #
# The service's own level floor
# --------------------------------------------------------------------------- #


def test_the_notification_floor_is_higher_than_the_webhooks(tmp_path):
    """A notification is a physical interruption; the webhook is not. The
    default floor of HIGH means a WATCH never buzzes."""
    from cryptopulse.api.service import ScannerService
    from cryptopulse.config.settings import CryptoPulseSettings

    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.database.url = f"sqlite:///{tmp_path}/notify.db"
    service = ScannerService(settings)

    assert service._loud_enough(_alert("A", AlertLevel.CRITICAL_SETUP)) is True
    assert service._loud_enough(_alert("A", AlertLevel.HIGH)) is True
    assert service._loud_enough(_alert("A", AlertLevel.WATCH)) is False
    assert service._loud_enough(_alert("A", AlertLevel.INFO)) is False


def test_an_unrecognised_floor_lets_everything_through_rather_than_muting_all(tmp_path):
    """A typo in the setting must fail loud, not silently disable every
    notification the user configured this for."""
    from cryptopulse.api.service import ScannerService
    from cryptopulse.config.settings import CryptoPulseSettings

    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.database.url = f"sqlite:///{tmp_path}/notify2.db"
    settings.alerts.android_min_level = "VERY_LOUD"
    service = ScannerService(settings)

    assert service._loud_enough(_alert("A", AlertLevel.INFO)) is True


def test_health_reports_the_channel_and_why_it_is_or_is_not_available(tmp_path):
    from cryptopulse.api.service import ScannerService
    from cryptopulse.config.settings import CryptoPulseSettings

    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.database.url = f"sqlite:///{tmp_path}/notify3.db"
    block = ScannerService(settings).status()["android_notifications"]

    assert "available" in block
    assert block["reason"], "an unavailable channel must always say why"
    assert block["min_level"] == "HIGH"


def test_the_repo_ships_no_hardcoded_notification_credentials():
    """This channel needs no account, no token and no URL — it talks to the
    device it runs on. A future change that introduces one would break the
    project's own rule that no secret lives in code."""
    from cryptopulse.alerts import notify

    source = (os.path.dirname(notify.__file__), "notify.py")
    text = open(os.path.join(*source), encoding="utf-8").read()
    for forbidden in ("api_key", "apikey", "Bearer ", "token="):
        assert forbidden not in text
