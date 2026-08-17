"""Startup: the screen must not wait for the network.

The failure this phase fixed was not a slow computation. It was an *ordering*
mistake — the launcher ran a blocking feed check before starting the server, so
the user waited on a network round trip in order to see data that was already
sitting in SQLite and did not depend on it.

These tests protect the ordering rather than any particular millisecond count.
Timings vary by machine and would make a flaky assertion; "the stored scan is
servable before the feed has been verified" is a property, and it is the one
that actually matters.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cryptopulse.api.app import create_app
from cryptopulse.api.service import ScannerService, set_service
from cryptopulse.api.startup import PHASES, StartupTracker
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.database.session import reset_engine
from cryptopulse.providers.verify import FeedState, verify_feed


@pytest.fixture()
def settings(tmp_path) -> CryptoPulseSettings:
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    s.database.url = f"sqlite:///{tmp_path}/startup.db"
    s.scanner.min_quote_volume_24h = 100_000.0
    return s


@pytest.fixture()
def client(settings):
    reset_engine()
    set_service(ScannerService(settings))
    with TestClient(create_app(start_loop=False)) as c:
        yield c
    set_service(None)
    reset_engine()


# --------------------------------------------------------------------------- #
# The tracker
# --------------------------------------------------------------------------- #


def test_a_phase_that_has_not_happened_is_absent_not_zero():
    """Zero would read as "instant" when it means "not yet", and on a startup
    timeline that is the difference between working and broken."""
    tracker = StartupTracker()
    tracker.mark("server_ready_ms")
    d = tracker.to_dict()

    assert d["server_ready_ms"] is not None
    assert d["first_scan_ms"] is None
    assert d["full_scan_ms"] is None


def test_every_declared_phase_appears_in_the_payload():
    """Declared rather than discovered, so a phase that stopped being recorded
    shows up as a gap instead of silently vanishing."""
    d = StartupTracker().to_dict()
    for phase in PHASES:
        assert phase in d, phase


def test_a_phase_is_recorded_once():
    """`ui_ready_ms` is set by a page load. Every later reload would otherwise
    overwrite the launch timing with a number describing a refresh."""
    tracker = StartupTracker()
    first = tracker.mark("ui_ready_ms")
    second = tracker.mark("ui_ready_ms")

    assert first is not None
    assert second is None, "a second mark must not overwrite the first"


def test_python_import_time_is_reported_rather_than_hidden(monkeypatch):
    """On a phone the interpreter and imports are the larger half of a launch.
    A timeline that started after them would claim the app was ready long
    before anything reached the screen."""
    import time

    monkeypatch.setenv("CP_PROCESS_START", str(time.time() - 2.0))
    tracker = StartupTracker()

    assert tracker.preamble_ms >= 1900
    assert tracker.mark("server_ready_ms") >= 1900
    assert tracker.to_dict()["python_preamble_ms"] >= 1900


# --------------------------------------------------------------------------- #
# Feed verification
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_synthetic_feed_is_skipped_rather_than_passed(settings):
    """Verifying generated candles against themselves would always pass and
    would mean nothing — which is worse than not checking, because it would
    print LIVE VERIFIED over invented data."""
    from cryptopulse.providers.registry import build_market_provider

    provider = build_market_provider(settings, cached=False)
    try:
        result = await verify_feed(provider, settings)
    finally:
        await provider.close()

    assert result.state is FeedState.SKIPPED_SYNTHETIC
    assert result.verified is False
    assert result.emoji == "🟠"


def test_the_four_states_have_distinct_presentations():
    from cryptopulse.providers.verify import PRESENTATION

    assert set(PRESENTATION) == set(FeedState)
    emojis = [p[0] for p in PRESENTATION.values()]
    assert len(set(emojis)) == len(FeedState), "two states sharing an icon are indistinguishable"
    # The three the user named, in the words they named them.
    assert PRESENTATION[FeedState.PENDING][1] == "VÉRIFICATION BINANCE EN COURS"
    assert PRESENTATION[FeedState.VERIFIED][1] == "BINANCE LIVE VERIFIED"
    assert PRESENTATION[FeedState.FAILED][1] == "FLUX LIVE NON VÉRIFIÉ"


def test_pending_is_not_verified(client):
    """Before the check has run, nothing may claim to be live. PENDING is an
    honest third state, not an optimistic default."""
    service = client.app  # noqa: F841 — clarity only
    from cryptopulse.api.service import get_service

    svc = get_service()
    svc.feed.state = FeedState.PENDING
    assert svc.live_verified is False


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #


def test_startup_endpoint_reports_the_timeline_and_the_feed(client):
    body = client.get("/api/startup").json()

    assert "startup" in body and "feed_verification" in body
    assert body["startup"]["server_ready_ms"] is not None
    assert "live_verified" in body


def test_the_feed_check_can_be_rerun_on_demand(client):
    body = client.post("/api/feed/verify").json()
    assert body["state"] in {s.value for s in FeedState}
    assert body["label_fr"]


def test_health_carries_the_startup_timeline(client):
    body = client.get("/api/health").json()
    assert "startup" in body
    assert "feed_verification" in body


# --------------------------------------------------------------------------- #
# The ordering property — the reason this phase exists
# --------------------------------------------------------------------------- #


def test_the_stored_scan_is_servable_without_any_feed_verification(client, settings):
    """The whole point. A launch must be able to fill the screen from SQLite
    while the network check has not run — or has failed."""
    client.post("/api/scan/run")  # populate the journal

    # A fresh process that has scanned nothing and verified nothing.
    reset_engine()
    set_service(ScannerService(settings))
    with TestClient(create_app(start_loop=False)) as fresh:
        from cryptopulse.api.service import get_service

        assert get_service().live_verified is False, "nothing has been verified yet"

        body = fresh.get("/api/scan?limit=5").json()
        assert body["results"], "the stored scan must be servable regardless"
        assert body["meta"]["source"] == "journal"
        assert body["meta"]["live"] is False
        assert body["meta"]["age_seconds"] is not None, "restored rows carry their real age"


def test_decisions_state_whether_they_are_verified_live(client):
    """Rule 7: while the feed is unverified, no new decision may be presented as
    live. The previous data stays visible — with its age — but the claim does
    not get made."""
    client.post("/api/scan/run")
    body = client.get("/api/decisions").json()

    assert "live_verified" in body
    assert "feed_verification" in body
    for row in body["entries"]:
        assert "live_verified" in row


def test_an_unverified_feed_journals_no_new_buy_signal(client):
    """A BUY recorded against an unverified feed would enter the statistics as
    though it had been a real recommendation."""
    from cryptopulse.api.service import get_service

    svc = get_service()
    svc.feed.state = FeedState.FAILED
    client.post("/api/scan/run")

    signals = client.get("/api/trade-signals").json()["signals"]
    assert [s for s in signals if s["action"] == "BUY"] == []


def test_the_first_cycle_defers_the_network_bound_followups(client):
    """Outcome resolution and horizon tracking are network-bound and nothing on
    screen depends on them. On the first cycle they would compete with the scan
    the user is actually waiting for."""
    from cryptopulse.api.service import get_service

    svc = get_service()
    client.post("/api/scan/run")

    assert svc.scan_count == 1
    assert svc.last_resolution is None, "resolution must not run on the first cycle"


def test_the_free_prescan_is_never_deferred(client):
    """Found by a failing test when the deferral was written too broadly. The
    pre-scan reads the ticker dictionary the scan already holds — it costs
    nothing, and skipping it on the first cycle silently loses the reading every
    later delta is measured against."""
    from cryptopulse.api.service import get_service

    svc = get_service()
    client.post("/api/scan/run")

    assert svc.scan_count == 1, "this is the cycle the deferral applies to"
    assert svc.hunter_memory._previous, (
        "the first cycle must still record a snapshot — it is free, and it is "
        "what every later acceleration is measured against"
    )
    assert client.get("/api/hunt").json()["prescan"]["requests_used"] == 0


def test_a_scan_that_produced_nothing_does_not_blank_the_screen(client, settings):
    """Found by running the app against an unreachable Binance. The scan
    completed with zero successes, `last_report` stopped being None, and the
    screen went from a populated journal to blank — the exact empty screen this
    phase exists to prevent. A scan that produced nothing has not replaced the
    last one that produced something."""
    client.post("/api/scan/run")  # a real scan, into the journal

    from cryptopulse.api.service import get_service

    svc = get_service()
    # Simulate the failed-feed case: a completed report carrying no results.
    from cryptopulse.scanner.base import ScanReport

    svc.last_report = ScanReport(
        started_at_ms=0, finished_at_ms=0, scanned=0, succeeded=0, failed=0,
        results=[], errors={}, provider_health=[], universe_size=0,
        synthetic_data=True, notes=[],
    )

    body = client.get("/api/scan?limit=5").json()
    assert body["results"], "the journal must still fill the screen"
    assert body["meta"]["source"] == "journal"
    assert "aucun résultat exploitable" in body["meta"]["reason"]
