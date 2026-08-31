"""Running unattended for weeks: liveness, memory across restarts, housekeeping.

The failure this file is about is not a wrong score. It is a radar that is up,
serving a dashboard, and no longer scanning — because that is the failure you
do not notice until you needed it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cryptopulse.alerts.engine import AlertKind
from cryptopulse.api.app import create_app
from cryptopulse.api.service import ScannerService, set_service
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import SYSTEM_CLOCK
from cryptopulse.database import repo
from cryptopulse.database.session import reset_engine
from cryptopulse.scanner.memory import ScoreMemory, ScorePoint


def _settings(tmp_path) -> CryptoPulseSettings:
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    s.scanner.universe = "robinhood"
    s.database.url = f"sqlite:///{tmp_path}/ops.db"
    s.alerts.channels = ["jsonl"]
    s.alerts.jsonl_path = str(tmp_path / "alerts.jsonl")
    return s


@pytest.fixture()
def service(tmp_path):
    reset_engine()
    svc = ScannerService(_settings(tmp_path))
    yield svc
    reset_engine()


# --------------------------------------------------------------------------- #
# Liveness
# --------------------------------------------------------------------------- #


def test_a_fresh_service_is_starting_not_healthy(service):
    health = service.health_status()
    assert health["status"] == "STARTING"
    assert "no scan has completed" in health["reasons"][0]


def test_a_process_that_never_completed_a_scan_is_down(service):
    """Up and serving is not the same as working."""
    service.started_at_ms -= 10 * 60_000
    assert service.health_status()["status"] == "DOWN"


async def test_a_successful_scan_makes_it_healthy(service):
    await service.run_once()
    health = service.health_status()
    assert health["status"] in ("OK", "DEGRADED")
    assert health["since_success_seconds"] is not None
    await service.stop()


async def test_a_stale_success_is_down_even_with_the_process_alive(service):
    await service.run_once()
    # Time passes with no further successful pass.
    service.last_success_ms -= (service.watchdog_deadline_seconds() + 60) * 1000
    health = service.health_status()
    assert health["status"] == "DOWN"
    assert "no successful scan" in health["reasons"][0]
    await service.stop()


def test_the_watchdog_deadline_follows_the_scan_interval(service):
    service.settings.scanner.scan_interval_seconds = 60
    service.settings.alerts.watchdog_after_seconds = 0
    assert service.watchdog_deadline_seconds() == 300

    service.settings.alerts.watchdog_after_seconds = 900
    assert service.watchdog_deadline_seconds() == 900


# --------------------------------------------------------------------------- #
# The watchdog says it once, and says it again when it recovers
# --------------------------------------------------------------------------- #


async def test_the_watchdog_fires_once_per_outage(service):
    await service.run_once()
    service.last_success_ms -= (service.watchdog_deadline_seconds() + 60) * 1000

    first = await service.check_watchdog()
    assert first is not None
    assert first.kind is AlertKind.SYSTEM
    assert "NOT SCANNING" in first.headline
    # Repeating every cycle is how a watchdog gets muted.
    assert await service.check_watchdog() is None
    await service.stop()


async def test_the_watchdog_reports_recovery(service):
    await service.run_once()
    service.last_success_ms -= (service.watchdog_deadline_seconds() + 60) * 1000
    await service.check_watchdog()

    service.last_success_ms = SYSTEM_CLOCK.now_ms()
    recovery = await service.check_watchdog()
    assert recovery is not None and "RECOVERED" in recovery.headline
    assert await service.check_watchdog() is None
    await service.stop()


async def test_a_system_alert_reaches_the_configured_channels(service, tmp_path):
    import json

    await service.run_once()
    service.last_success_ms -= (service.watchdog_deadline_seconds() + 60) * 1000
    await service.check_watchdog()

    lines = (tmp_path / "alerts.jsonl").read_text().strip().split("\n")
    kinds = [json.loads(line)["kind"] for line in lines]
    assert "SYSTEM" in kinds
    await service.stop()


def test_the_watchdog_can_be_turned_off(service):
    service.settings.alerts.watchdog_enabled = False
    service.started_at_ms -= 10 * 60_000
    assert service.health_status()["status"] == "DOWN"  # health still tells the truth


# --------------------------------------------------------------------------- #
# Score memory across a restart
# --------------------------------------------------------------------------- #


def test_score_memory_rehydrates_from_persisted_points():
    memory = ScoreMemory()
    points = [
        {"symbol": "BTCUSDT", "timestamp_ms": 1_000, "final_score": 40.0, "raw_score": 45.0,
         "price": 1.0, "state": "OBSERVE"},
        {"symbol": "BTCUSDT", "timestamp_ms": 2_000, "final_score": 60.0, "raw_score": 65.0,
         "price": 1.1, "state": "ARMED"},
    ]
    assert memory.rehydrate(points) == 2
    history = memory.history("BTCUSDT")
    assert [p.final_score for p in history] == [40.0, 60.0]
    assert isinstance(history[0], ScorePoint)


def test_a_malformed_row_does_not_stop_a_restart():
    memory = ScoreMemory()
    loaded = memory.rehydrate([
        {"symbol": "BTCUSDT", "timestamp_ms": 1, "final_score": 1.0, "raw_score": 1.0,
         "price": 1.0, "state": "OBSERVE"},
        {"symbol": "BROKEN"},  # missing everything else
        {"symbol": "ETHUSDT", "timestamp_ms": None, "final_score": "x", "raw_score": 1.0,
         "price": 1.0, "state": "OBSERVE"},
    ])
    assert loaded == 1


async def test_a_restarted_service_recovers_its_score_history(tmp_path):
    """Without this, a restart blinds the ranker to rising scores for a while."""
    reset_engine()
    settings = _settings(tmp_path)

    first = ScannerService(settings)
    await first.run_once()
    symbols_before = {s for s in first.memory._data if first.memory.history(s)}
    await first.stop()
    assert symbols_before

    reset_engine()
    second = ScannerService(settings)
    second.ensure_db()  # rehydration happens here, before any scan
    recovered = {s for s in second.memory._data if second.memory.history(s)}
    assert recovered & symbols_before, "no score history survived the restart"
    reset_engine()


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #


async def test_maintenance_purges_old_score_points_but_never_signals(service):
    await service.run_once()
    signals_before = repo.signal_stats()["total_signals"]
    assert signals_before > 0

    # Everything is "old" now.
    service.settings.database.retention_days = 0
    purged = await service.run_maintenance(force=True)

    assert purged > 0, "score points should have been purged"
    assert repo.signal_stats()["total_signals"] == signals_before, (
        "signals are the evidence; a ×10 label can take 180 days to settle, so they "
        "must never be deleted on a retention timer"
    )
    await service.stop()


async def test_maintenance_does_not_run_on_every_scan(service):
    await service.run_once()
    service.settings.database.retention_days = 0
    assert await service.run_maintenance(force=True) >= 0
    # A second call inside the window is a no-op rather than another full delete.
    assert await service.run_maintenance() == 0
    await service.stop()


# --------------------------------------------------------------------------- #
# The endpoint an orchestrator reads
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_path):
    reset_engine()
    svc = ScannerService(_settings(tmp_path))
    set_service(svc)
    app = create_app(start_loop=False)
    with TestClient(app) as c:
        yield c, svc
    set_service(None)
    reset_engine()


def test_healthz_answers_with_a_status_code_not_a_payload_to_parse(client):
    c, svc = client
    assert c.get("/healthz").status_code == 200  # STARTING

    c.post("/api/scan/run")
    assert c.get("/healthz").status_code == 200

    svc.last_success_ms -= (svc.watchdog_deadline_seconds() + 60) * 1000
    resp = c.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "DOWN"


def test_the_dashboard_health_payload_carries_the_same_verdict(client):
    c, _ = client
    c.post("/api/scan/run")
    body = c.get("/api/health").json()
    assert body["health"]["status"] in ("OK", "DEGRADED")
    assert body["candle_cache"] is not None  # the saving is visible, not internal
