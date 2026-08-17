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
    assert "donnée de marché" in body["synthetic_warning"]
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
    assert "reste vide" in stats["win_rate_note"]
    assert stats["total_signals"] >= 0


def test_alerts_endpoint_shape(client):
    client.post("/api/scan/run")
    assert isinstance(client.get("/api/alerts").json()["alerts"], list)


def test_config_endpoint_publishes_weights_and_the_disclaimer(client):
    body = client.get("/api/config").json()
    assert sum(body["weights"].values()) == pytest.approx(100.0)
    assert "ne doit pas être lu comme une probabilité" in body["disclaimer"]


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


# --------------------------------------------------------------------------- #
# Outcome tracking endpoints
# --------------------------------------------------------------------------- #


def test_performance_endpoint_before_anything_is_resolved(client):
    client.post("/api/scan/run")
    body = client.get("/api/performance").json()

    assert body["counts"]["settled"] == 0
    assert body["performance"]["overall"]["n"] == 0
    assert body["performance"]["overall"]["win_rate"] is None, "no verdicts must not read as a 0% win rate"
    assert "config" in body["label"] and "definition" in body["label"]
    assert "round_trip_cost_pct" in body["costs"]


def test_resolve_endpoint_is_a_noop_when_nothing_is_ready(client):
    client.post("/api/scan/run")
    body = client.post("/api/outcomes/resolve").json()
    # Signals were just created, so none can have a verdict yet.
    assert body["resolution"]["resolved"] == 0
    assert body["counts"]["settled"] == 0


def test_pending_endpoint_reports_the_readiness_cutoff(client):
    client.post("/api/scan/run")
    body = client.get("/api/outcomes/pending").json()
    assert "ready_before_ms" in body and body["horizon_bars"] > 0
    assert isinstance(body["signals"], list)


def test_health_exposes_the_outcome_tracker_configuration(client):
    tracker = client.get("/api/health").json()["outcome_tracker"]
    assert tracker["label_config"]
    assert "next bar's open" in tracker["definition"]
    assert tracker["horizon_bars"] > 0


def test_signal_stats_never_invents_a_win_rate(client):
    client.post("/api/scan/run")
    stats = client.get("/api/signals").json()["stats"]
    assert stats["win_rate"] is None
    assert stats["settled"] == 0
    assert "reste vide" in stats["win_rate_note"] or "Aucune issue" in stats["win_rate_note"]
    assert stats["sufficient_sample"] is False


# --------------------------------------------------------------------------- #
# Multi-horizon verification
# --------------------------------------------------------------------------- #


def test_health_exposes_the_horizon_tracker_configuration(client):
    h = client.get("/api/health").json()["horizon_tracker"]
    assert h["horizons"] == ["15m", "1h", "4h", "24h"]
    assert "après le coût aller-retour modélisé" in h["success_criterion"]
    assert "jamais la clôture du signal lui-même" in h["entry_rule"]


def test_health_states_live_or_demo_without_the_client_guessing(client):
    body = client.get("/api/health").json()
    assert body["data_mode"] == "DEMO"
    assert "Aucun chiffre ici ne provient d'un marché" in body["data_mode_detail"]


def test_horizons_endpoint_reports_every_window_before_any_has_closed(client):
    client.post("/api/scan/run")
    perf = client.get("/api/horizons").json()["performance"]
    assert perf["horizons"] == ["15m", "1h", "4h", "24h"]
    assert all(b["n"] == 0 for b in perf["overall"])
    assert all(b["success_rate"] is None for b in perf["overall"]), (
        "an unmeasured window must not read as a 0% success rate"
    )


def test_horizon_tracking_endpoint_runs_and_reports_its_windows(client):
    client.post("/api/scan/run")
    run = client.post("/api/horizons/track").json()["run"]
    assert run["horizons"] == ["15m", "1h", "4h", "24h"]
    # Signals were created moments ago, so no window can have closed.
    assert run["resolved_horizons"] == 0


def test_per_signal_horizons_say_pending_rather_than_returning_zeros(client):
    client.post("/api/scan/run")
    body = client.get("/api/signals/1/horizons").json()
    assert body["horizons"] == []
    assert "plutôt que tranchée au prix actuel" in body["message"]


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #


def test_every_scanned_row_carries_a_verdict_with_a_caveat(client):
    client.post("/api/scan/run")
    rows = client.get("/api/scan").json()["results"]
    assert rows
    for r in rows:
        v = r["verdict"]
        assert v["level"] in {"STRONG", "WATCH", "RISKY", "AVOID"}
        assert v["emoji"] and v["headline"] and v["headline_fr"]
        assert "not a probability" in v["caveat"], "a badge without its caveat reads as a prediction"


# --------------------------------------------------------------------------- #
# Alert delivery and retention
# --------------------------------------------------------------------------- #


def test_health_says_alerts_are_dashboard_only_when_no_webhook_is_set(client):
    d = client.get("/api/health").json()["alert_delivery"]
    assert d["configured"] is False
    assert "CP_ALERT_WEBHOOK_URL" in d["note"]
    assert d["last"]["attempted"] is False


def test_health_never_exposes_the_webhook_url(client):
    """A webhook URL is a bearer token. /api/health is often the first thing
    pasted into a bug report."""
    from cryptopulse.api.service import get_service

    secret = "https://discord.com/api/webhooks/999/TOP-SECRET-TOKEN"
    get_service().delivery.webhook_url = secret
    body = client.get("/api/health").text
    assert "TOP-SECRET-TOKEN" not in body
    assert "999" not in body.split("alert_delivery")[-1]


def test_health_reports_the_retention_policy(client):
    r = client.get("/api/health").json()["retention"]
    assert r["retention_days"] > 0
    assert "doit encore une issue" in r["note"]


def test_prune_endpoint_reports_what_it_removed_and_what_it_held_back(client):
    client.post("/api/scan/run")
    body = client.post("/api/maintenance/prune").json()
    assert body["removed"]["signals"] == 0, "nothing is old enough to prune"
    assert "held_back_unsettled" in body


def test_health_reports_the_candle_cache(client):
    """The saving must be visible, and a cache that silently stopped working
    must look different from one that is switched off."""
    c = client.get("/api/health").json()["candle_cache"]
    assert c["enabled"] is True
    assert c["hit_rate"] is None, "nothing asked yet must not read as a 0% hit rate"

    client.post("/api/scan/run")
    c = client.get("/api/health").json()["candle_cache"]
    assert c["requests"] > 0
    assert c["stored_series"] > 0


# --------------------------------------------------------------------------- #
# Token Hunter
# --------------------------------------------------------------------------- #


def test_hunt_says_it_needs_a_scan_before_one_has_run(client):
    """The hunter reads the ticker snapshot a scan produces; it does not fetch."""
    resp = client.get("/api/hunt")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "NO_SCAN_YET"


def test_hunt_ranks_the_venue_without_spending_a_request(client):
    client.post("/api/scan/run")
    body = client.get("/api/hunt").json()
    p = body["prescan"]

    assert p["requests_used"] == 0, "a wide search must cost nothing extra"
    assert p["universe_size"] > 0
    assert p["candidates"], "the venue produced no candidates at all"
    assert body["data_mode"] == "DEMO"
    assert "n'est pas un score" in body["disclaimer"]


def test_hunt_candidates_never_claim_an_acceleration_they_cannot_measure(client):
    """One scan means one reading, so every delta must be null rather than 0."""
    client.post("/api/scan/run")
    p = client.get("/api/hunt").json()["prescan"]

    assert p["has_previous_reading"] is False
    for c in p["candidates"]:
        assert c["volume_excess_vs_yesterday"] is None
        assert c["seconds_since_previous"] is None
        assert c["reasons"] or c["caveats"], f"{c['symbol']} ranked with no explanation"


def test_hunt_serves_the_cycle_report_so_deltas_survive_being_read(client):
    """Recomputing on read would return the same ranking against a memory the
    cycle had just advanced, so every delta would come back null exactly when
    the user asked for it."""
    import dataclasses

    from cryptopulse.api.service import get_service

    service = get_service()
    client.post("/api/scan/run")

    # Age the recorded snapshots so the next cycle has a real interval to measure.
    service.hunter_memory._previous = {
        k: dataclasses.replace(s, at_ms=s.at_ms - 90_000, quote_volume_24h=s.quote_volume_24h * 0.94)
        for k, s in service.hunter_memory._previous.items()
    }
    client.post("/api/scan/run")

    first = client.get("/api/hunt").json()["prescan"]
    assert first["has_previous_reading"] is True

    # Reading repeatedly must not degrade the answer.
    for _ in range(3):
        again = client.get("/api/hunt").json()["prescan"]
        assert again["has_previous_reading"] is True
        assert again["candidates"][0]["volume_excess_vs_yesterday"] == \
            first["candidates"][0]["volume_excess_vs_yesterday"]


def test_deep_scan_needs_a_prescan_first(client):
    resp = client.post("/api/hunt/deep")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "NO_PRESCAN_YET"


def test_deep_scan_reports_what_it_cost_and_which_engine_scored(client):
    client.post("/api/scan/run")
    body = client.post("/api/hunt/deep", params={"max_symbols": 5}).json()

    d = body["deep_scan"]
    assert d["examined"] > 0
    assert d["kline_requests"] == d["newly_fetched"] * 4, "the cost must be stated exactly"
    assert body["engine"]["discovery_engine"] == "DISCOVERY_ENGINE_V1"
    assert body["engine"]["weights_fingerprint"]
    assert sum(body["engine"]["weights"].values()) == 100.0
    assert "pas une probabilité" in body["disclaimer"]

    first = d["results"][0]
    assert first["discovery"]["discovery_label"].endswith("/100")
    assert "%" not in first["discovery"]["discovery_label"]


def test_pump_history_reports_its_resolution_and_never_fakes_a_rate(client):
    body = client.get("/api/pumps/BTCUSDT").json()
    h, sim = body["history"], body["similarity"]

    assert h["timeframe"] == "1h"
    assert h["days_covered"] > 1.0
    assert "definition" in h, "what counts as a pump must be stated"
    if h["episodes"]:
        assert h["resolution_minutes"] == 60
        assert any("résolution" in n for n in h["notes"])

    # Below the sample floor no rate is shown at all, not a greyed-out one.
    if sim["insufficient_sample"]:
        assert sim["reached"] == {}
        assert sim["median_gain_pct"] is None
    assert sim["min_sample"] == 20


# --------------------------------------------------------------------------- #
# Pump history endpoint — the contract the token card renders against.
#
# These assert the payload's *shape and refusals*, not its values: the values
# come from the synthetic provider and mean nothing. What matters is that the
# fields the UI reads are always present, and that the one forward-looking block
# stays silent below the sample floor no matter what the numbers say.
# --------------------------------------------------------------------------- #


def test_pump_history_carries_every_field_the_token_card_renders(client):
    client.post("/api/scan/run")
    body = client.get("/api/pumps/BTCUSDT").json()

    history = body["history"]
    for key in (
        "symbol", "timeframe", "bars_examined", "days_covered", "resolution_minutes",
        "definition", "definition_fr", "episodes_found", "episodes",
    ):
        assert key in history, f"the token card reads history.{key}"

    for key in ("n", "by_size", "insufficient_sample", "min_sample"):
        assert key in body["stats"]

    for key in ("comparable", "examined", "not_comparable", "reached",
                "insufficient_sample", "min_sample"):
        assert key in body["similarity"]

    assert set(body["current_setup"]) == {
        "rvol", "volume_change_pct", "range_position", "atr_pct"
    }
    # The panel is rendered inside a screen whose banner is driven by this.
    assert body["data_mode"] == "DEMO"


def test_pump_episodes_state_their_timing_resolution(client):
    """Timing is known to the bar. An episode that did not say so would let the
    UI render a minute figure the 1h detection cannot support."""
    body = client.get("/api/pumps/BTCUSDT").json()
    for episode in body["history"]["episodes"]:
        assert episode["resolution_minutes"] == 60
        assert episode["minutes_to_peak"] % 60 == 0


def test_similarity_sends_no_rate_at_all_below_the_sample_floor(client):
    """Not a greyed-out rate — no rate. A number on screen gets read however it
    is styled, and on a few weeks of history this is the ordinary outcome."""
    similarity = client.get("/api/pumps/BTCUSDT").json()["similarity"]
    if similarity["insufficient_sample"]:
        assert similarity["reached"] == {}
        assert similarity["median_gain_pct"] is None
        assert similarity["median_minutes_to_peak"] is None
        assert similarity["median_drawdown_after_pct"] is None
    else:
        assert similarity["comparable"] >= similarity["min_sample"]


def test_pump_definition_is_carried_in_both_languages(client):
    """The definition is the whole meaning of the panel it heads, and the owner
    of this product reads French. Same choice as the verdict, same reason."""
    history = client.get("/api/pumps/BTCUSDT").json()["history"]
    assert "at least" in history["definition"]
    assert "au moins" in history["definition_fr"]
    assert history["definition"] != history["definition_fr"]


def test_unknown_symbol_reports_a_failure_rather_than_an_empty_history(client):
    """An empty episode list means 'this token had no accelerations'. A symbol
    that does not exist must never produce that sentence."""
    resp = client.get("/api/pumps/NOSUCHTOKEN")
    assert resp.status_code == 502
    assert "NOSUCHTOKEN" in resp.json()["detail"]
