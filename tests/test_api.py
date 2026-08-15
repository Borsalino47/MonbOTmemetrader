"""API contract tests against a real app instance backed by the fixture provider."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cryptopulse.api.app import create_app
from cryptopulse.api.service import ScannerService, set_service
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.database.session import reset_engine


@pytest.fixture()
def client(tmp_path):
    reset_engine()
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.database.url = f"sqlite:///{tmp_path}/test.db"
    settings.scanner.min_quote_volume_24h = 100_000.0

    service = ScannerService(settings)
    set_service(service)
    # start_loop=False: the test drives scans explicitly rather than racing a timer.
    app = create_app(start_loop=False)
    with TestClient(app) as c:
        yield c
    set_service(None)
    reset_engine()


def test_health_reports_provider_and_paper_mode(client):
    body = client.get("/api/health").json()
    assert body["paper_mode"] is True, "V1 must never default to live execution"
    assert body["provider"] == "SYNTHETIC-FIXTURE"
    assert body["synthetic_data"] is True
    assert "is market data" in body["synthetic_warning"]
    assert body["engine_version"] == "SCORE_ENGINE_V1"


def test_scan_endpoint_says_so_before_any_scan_has_run(client):
    resp = client.get("/api/scan")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "NO_SCAN_YET"


def test_run_scan_then_read_results(client):
    meta = client.post("/api/scan/run").json()
    assert meta["succeeded"] > 5
    assert meta["synthetic_data"] is True

    body = client.get("/api/scan").json()
    assert body["results"]
    row = body["results"][0]
    for key in (
        "symbol", "price", "raw_score", "risk_penalty", "final_score",
        "pump_maturity", "data_confidence", "safety", "liquidity", "setup", "why", "risks",
    ):
        assert key in row, f"missing {key} in scan row"
    assert row["opportunity_label"].endswith("/100")


def test_scan_row_arithmetic_is_consistent(client):
    client.post("/api/scan/run")
    for row in client.get("/api/scan").json()["results"]:
        assert row["final_score"] == pytest.approx(max(0.0, row["raw_score"] - row["risk_penalty"]), abs=0.02)


def test_scan_filters(client):
    client.post("/api/scan/run")
    high = client.get("/api/scan", params={"min_score": 60}).json()
    assert all(r["final_score"] >= 60 for r in high["results"])

    young = client.get("/api/scan", params={"max_pump_maturity": 40}).json()
    assert all(r["pump_maturity"]["score"] <= 40 for r in young["results"])

    armed = client.get("/api/scan", params={"state": "ARMED,BREAKOUT"}).json()
    assert all(r["setup"]["state"] in ("ARMED", "BREAKOUT") for r in armed["results"])


def test_invalid_liquidity_filter_is_rejected(client):
    client.post("/api/scan/run")
    assert client.get("/api/scan", params={"min_liquidity": "SUPERB"}).status_code == 400


def test_top_opportunities_excludes_vetoed_assets(client):
    client.post("/api/scan/run")
    for row in client.get("/api/scan/top", params={"limit": 10}).json()["results"]:
        assert row["safety"]["hard_veto"] is False
        assert row["liquidity"]["veto"] is False
        assert row["data_confidence"]["score"] >= 50


def test_asset_detail_explains_the_score(client):
    client.post("/api/scan/run")
    symbol = client.get("/api/scan").json()["results"][0]["symbol"]
    body = client.get(f"/api/asset/{symbol}").json()

    assert body["symbol"] == symbol
    assert "why_this_asset" in body and "what_can_invalidate_it" in body
    ex = body["explainability"]
    total = sum(c["points"] for c in ex["breakdown"])
    # Each of the 8 component points is rounded to 2dp for display, so the sum can
    # legitimately drift up to 0.04 from the unrounded raw score.
    assert total == pytest.approx(ex["raw_score"], abs=0.05), "breakdown must add up to the raw score"
    assert ex["final_score"] == pytest.approx(max(0.0, ex["raw_score"] - ex["risk_penalty"]), abs=0.02)
    assert "features" in body


def test_unknown_asset_is_a_404(client):
    client.post("/api/scan/run")
    assert client.get("/api/asset/NOTAREALCOIN").status_code == 404


def test_asset_history_endpoint(client):
    client.post("/api/scan/run")
    symbol = client.get("/api/scan").json()["results"][0]["symbol"]
    body = client.get(f"/api/asset/{symbol}/history").json()
    assert body["symbol"] == symbol
    assert isinstance(body["points"], list)


def test_signals_endpoint_refuses_to_invent_a_win_rate(client):
    client.post("/api/scan/run")
    stats = client.get("/api/signals").json()["stats"]
    assert stats["win_rate"] is None, "no outcome has been resolved, so there is no win rate"
    assert "null until" in stats["win_rate_note"]
    assert stats["total_signals"] >= 0


def test_alerts_endpoint_shape(client):
    client.post("/api/scan/run")
    assert isinstance(client.get("/api/alerts").json()["alerts"], list)


def test_config_endpoint_publishes_weights_and_the_disclaimer(client):
    body = client.get("/api/config").json()
    assert sum(body["weights"].values()) == pytest.approx(100.0)
    assert "not" in body["disclaimer"].lower() and "probability" in body["disclaimer"].lower()


def test_no_endpoint_presents_a_score_as_a_probability(client):
    client.post("/api/scan/run")
    for path in ("/api/scan", "/api/scan/top", "/api/health"):
        text = client.get(path).text.lower()
        assert "% probability" not in text
        assert "chance of" not in text


def test_freshness_is_always_reported(client):
    client.post("/api/scan/run")
    health = client.get("/api/health").json()
    last = health["last_scan"]
    assert last is not None
    assert "age_seconds" in last and last["age_seconds"] >= 0
    assert "market_data_age_seconds" in last
    assert health["provider_health"]


def test_scan_persists_to_the_database(client):
    client.post("/api/scan/run")
    body = client.get("/api/signals", params={"limit": 50}).json()
    if body["signals"]:
        row = body["signals"][0]
        assert row["engine_version"] == "SCORE_ENGINE_V1"
        assert row["synthetic"] is True
        assert row["outcome"]["evaluated"] is False
