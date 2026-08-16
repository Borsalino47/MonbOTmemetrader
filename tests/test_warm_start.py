"""Warm start: the screen is never empty, and never lies about what it shows.

Before this, `/api/scan` read an in-memory variable that is empty until the first
scan of the *process* finishes, so a restart meant a blank dashboard for the
length of a full scan — hundreds of requests over a mobile connection — while the
journal on disk held the previous scan all along.

Serving that journal is easy. Serving it *honestly* is what these tests are for:
a restored scan must carry its real age and its own provenance, and a snapshot
written by the synthetic provider must stay marked DEMO even when the process is
now configured against a real feed. Showing old generated rows without the DEMO
banner would be the worst regression this project could ship.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cryptopulse.api.app import create_app
from cryptopulse.api.service import ScannerService, set_service
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.database import repo
from cryptopulse.database.session import reset_engine


@pytest.fixture()
def settings(tmp_path) -> CryptoPulseSettings:
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    s.database.url = f"sqlite:///{tmp_path}/warm.db"
    s.scanner.min_quote_volume_24h = 100_000.0
    return s


def _client(settings) -> TestClient:
    reset_engine()
    set_service(ScannerService(settings))
    return TestClient(create_app(start_loop=False))


def _populate(settings) -> int:
    """One real scan, persisted — the state a restart inherits."""
    with _client(settings) as c:
        c.post("/api/scan/run")
        return c.get("/api/signals").json()["stats"]["total_signals"]


# --------------------------------------------------------------------------- #
# The empty screen
# --------------------------------------------------------------------------- #


def test_a_restart_shows_the_last_scan_instead_of_an_empty_screen(settings):
    assert _populate(settings) > 0

    with _client(settings) as c:  # fresh process, empty memory, full journal
        resp = c.get("/api/scan")
        assert resp.status_code == 200, "a populated journal must never answer 503"
        body = resp.json()
        assert body["results"], "the restored scan must actually carry rows"
        assert body["meta"]["source"] == "journal"


def test_an_empty_journal_still_says_so_rather_than_inventing_a_scan(settings):
    """Nothing to restore is a real state and keeps its honest 503."""
    with _client(settings) as c:
        resp = c.get("/api/scan")
        assert resp.status_code == 503
        assert resp.json()["reason"] == "NO_SCAN_YET"


def test_a_live_scan_supersedes_the_journal(settings):
    _populate(settings)
    with _client(settings) as c:
        assert c.get("/api/scan").json()["meta"]["source"] == "journal"
        c.post("/api/scan/run")
        meta = c.get("/api/scan").json()["meta"]
        assert meta["source"] == "live"
        assert meta["live"] is True and meta["stale"] is False


# --------------------------------------------------------------------------- #
# Honesty about what is on screen
# --------------------------------------------------------------------------- #


def test_restored_rows_are_marked_stale_and_carry_their_real_age(settings):
    _populate(settings)
    with _client(settings) as c:
        meta = c.get("/api/scan").json()["meta"]
        assert meta["live"] is False
        assert meta["stale"] is True
        assert meta["age_seconds"] >= 0
        assert "journal" in meta["message"]


def test_a_synthetic_snapshot_keeps_its_demo_banner_after_the_provider_changes(settings):
    """The regression that would matter most.

    The journal was written by the synthetic provider. The process is now
    configured against a different one. What is *on screen* is still generated
    data, so it must still say DEMO — the banner follows the rows, not the config.
    """
    _populate(settings)

    reset_engine()
    live_ish = settings.model_copy(deep=True)
    live_ish.providers.market_data = "binance"  # never contacted: no scan is run
    set_service(ScannerService(live_ish))
    with TestClient(create_app(start_loop=False)) as c:
        meta = c.get("/api/scan").json()["meta"]
        assert meta["synthetic_data"] is True
        assert meta["data_mode"] == "DEMO"

        health = c.get("/api/health").json()
        assert health["displaying"] == "journal"
        assert health["journal_snapshot"]["data_mode"] == "DEMO"
        assert health["data_mode"] == "DEMO", "the banner must follow the rows on screen"
    set_service(None)
    reset_engine()


def test_health_reports_which_source_is_being_displayed(settings):
    _populate(settings)
    with _client(settings) as c:
        assert c.get("/api/health").json()["displaying"] == "journal"
        c.post("/api/scan/run")
        assert c.get("/api/health").json()["displaying"] == "live"


def test_unjournalled_columns_are_unknown_rather_than_zero(settings):
    """Order-book fields were never stored. A restored row must not claim a
    spread of zero, which would read as a perfectly tight market."""
    _populate(settings)
    with _client(settings) as c:
        row = c.get("/api/scan").json()["results"][0]
        assert row["from_journal"] is True
        metrics = row["metrics"]
        assert "spread_bps" not in metrics or metrics.get("spread_bps") is None
        assert metrics.get("atr_pct") is None
        assert row["data_confidence"]["max_age_seconds"] is None


# --------------------------------------------------------------------------- #
# The restored rows behave like scan rows
# --------------------------------------------------------------------------- #


def test_a_restored_row_carries_the_same_verdict_the_live_row_had(settings):
    """The snapshot computes its verdict with `build_verdict`, the same function
    the live path uses, so the badge cannot drift between the two sources."""
    with _client(settings) as c:
        c.post("/api/scan/run")
        live = {r["symbol"]: r["verdict"]["level"] for r in c.get("/api/scan").json()["results"]}

    with _client(settings) as c:
        restored = c.get("/api/scan").json()["results"]
        assert restored
        for row in restored:
            assert row["verdict"]["level"] == live[row["symbol"]]
            assert "not a probability" in row["verdict"]["caveat"]


def test_filters_apply_to_restored_rows_too(settings):
    _populate(settings)
    with _client(settings) as c:
        everything = c.get("/api/scan").json()["results"]
        floor = max(r["final_score"] for r in everything)
        filtered = c.get("/api/scan", params={"min_score": floor}).json()["results"]
        assert 0 < len(filtered) < len(everything) or len(everything) == 1
        assert all(r["final_score"] >= floor for r in filtered)


def test_restored_rows_are_ranked_best_first(settings):
    _populate(settings)
    with _client(settings) as c:
        scores = [r["final_score"] for r in c.get("/api/scan").json()["results"]]
        assert scores == sorted(scores, reverse=True)


def test_the_snapshot_takes_one_scan_not_a_mixture_of_two(settings):
    """Two scans at different candle times must not be merged into one screen —
    that would show prices from two different moments side by side."""
    with _client(settings) as c:
        c.post("/api/scan/run")

    snap = repo.last_scan_snapshot()
    assert snap is not None
    stamps = {r["timestamp_ms"] for r in snap["rows"]}
    assert len(stamps) == 1, "every restored row must belong to the same scan"
    reset_engine()
