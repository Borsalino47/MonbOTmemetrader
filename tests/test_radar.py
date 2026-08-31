"""The autonomous radar: configuration, alerting on the ×10 horizon, and delivery.

This is the file that would catch "the radar ran all night and nobody was
told" — the failure that makes every other module pointless.
"""

from __future__ import annotations

import argparse
import json

import pytest
from fastapi.testclient import TestClient

from cryptopulse.alerts.engine import AlertEngine, AlertKind
from cryptopulse.api.app import create_app
from cryptopulse.api.service import ScannerService, set_service
from cryptopulse.cli import cmd_radar
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.database.session import reset_engine
from cryptopulse.providers.fixture import FixtureProvider
from cryptopulse.scanner.cex import CexScanner
from cryptopulse.scoring.moonshot import MoonshotStage
from tests.conftest import FIXED_NOW_MS


def _settings(tmp_path, **overrides) -> CryptoPulseSettings:
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    s.scanner.universe = "robinhood"
    s.database.url = f"sqlite:///{tmp_path}/radar.db"
    s.alerts.channels = ["jsonl"]
    s.alerts.jsonl_path = str(tmp_path / "alerts.jsonl")
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        setattr(getattr(s, section), field, value)
    return s


# --------------------------------------------------------------------------- #
# Configuration that used to take the process down
# --------------------------------------------------------------------------- #


def test_comma_separated_env_lists_load(monkeypatch):
    """.env.example documents comma-separated lists; they must actually parse.

    pydantic-settings JSON-decodes complex fields before validators run, so this
    raised at import until `enable_decoding=False` was set. The whole process
    died on a documented configuration.
    """
    monkeypatch.setenv("CP_SCAN_ALWAYS_INCLUDE", "BTCUSDT,ETHUSDT")
    monkeypatch.setenv("CP_SCAN_TIMEFRAMES", "5m,1h")
    monkeypatch.setenv("CP_ALERT_CHANNELS", "console,jsonl")
    monkeypatch.setenv("CP_CORS_ORIGINS", "http://a,http://b")
    monkeypatch.setenv("CP_SCAN_ROBINHOOD_EXTRA", "fartcoin,spx")

    s = CryptoPulseSettings()
    assert s.scanner.always_include == ["BTCUSDT", "ETHUSDT"]
    assert [tf.value for tf in s.scanner.timeframes] == ["5m", "1h"]
    assert s.alerts.channels == ["console", "jsonl"]
    assert s.cors_origins == ["http://a", "http://b"]
    assert s.scanner.robinhood_extra == ["FARTCOIN", "SPX"]


def test_the_moonshot_timeframe_is_added_to_whatever_is_configured(tmp_path):
    settings = _settings(tmp_path)
    scanner = CexScanner(settings, provider=FixtureProvider(), clock=FrozenClock(FIXED_NOW_MS))
    assert settings.moonshot.timeframe in scanner.timeframes
    # Added, not substituted: the intraday setup logic still needs its own.
    for tf in settings.scanner.timeframes:
        assert tf in scanner.timeframes


def test_disabling_the_moonshot_layer_removes_its_fetch_cost(tmp_path):
    settings = _settings(tmp_path, moonshot__enabled=False)
    scanner = CexScanner(settings, provider=FixtureProvider(), clock=FrozenClock(FIXED_NOW_MS))
    assert scanner.timeframes == list(settings.scanner.timeframes)


def test_the_benchmark_falls_back_to_the_providers_own_reference_symbol(tmp_path):
    """Hardcoding BTCUSDT is simply wrong on a venue that calls bitcoin XBT."""
    settings = _settings(tmp_path)
    scanner = CexScanner(settings, provider=FixtureProvider(), clock=FrozenClock(FIXED_NOW_MS))
    assert scanner.benchmark_symbol == "BTCUSDT"

    settings.scanner.benchmark_symbol = "XBTUSDT"
    assert scanner.benchmark_symbol == "XBTUSDT"


# --------------------------------------------------------------------------- #
# Cross-asset enrichment reaches the scoring layer
# --------------------------------------------------------------------------- #


async def test_a_scan_attaches_relative_strength_and_a_cross_sectional_volume_rank(tmp_path):
    settings = _settings(tmp_path)
    clock = FrozenClock(FIXED_NOW_MS)
    scanner = CexScanner(settings, provider=FixtureProvider(clock=clock), clock=clock)
    report = await scanner.scan()

    enriched = [r.features for r in report.results if r.features is not None]
    assert any(f.rs_vs_benchmark_pct is not None for f in enriched)
    assert any(f.rvol_percentile_universe is not None for f in enriched)
    for f in enriched:
        if f.rvol_percentile_universe is not None:
            assert 0.0 <= f.rvol_percentile_universe <= 1.0
        if f.rs_vs_benchmark_pct is not None:
            assert f.benchmark_symbol and "BTCUSDT" in f.benchmark_symbol
    await scanner.close()


async def test_ranking_by_moonshot_orders_by_the_daily_reading(tmp_path):
    settings = _settings(tmp_path, scanner__rank_mode="moonshot")
    clock = FrozenClock(FIXED_NOW_MS)
    scanner = CexScanner(settings, provider=FixtureProvider(clock=clock), clock=clock)
    report = await scanner.scan()

    readable = [r for r in report.results if r.moonshot and r.moonshot.stage is not MoonshotStage.UNKNOWN]
    scores = [r.moonshot.score for r in readable]
    assert scores == sorted(scores, reverse=True)
    assert any("ranked by moonshot" in n for n in report.notes)
    await scanner.close()


# --------------------------------------------------------------------------- #
# Moonshot alerts
# --------------------------------------------------------------------------- #


def _result_with_moonshot(score: float, stage: MoonshotStage, symbol: str = "PEPEUSDT"):
    """A minimal scored result; only the fields the alert gates read."""
    from types import SimpleNamespace

    from cryptopulse.risk.liquidity import LiquidityStatus
    from cryptopulse.scoring.moonshot import MoonshotAssessment
    from cryptopulse.scoring.states import SetupState

    return SimpleNamespace(
        symbol=symbol,
        price=1.23,
        final_score=45.0,
        maturity=SimpleNamespace(score=20.0),
        confidence=SimpleNamespace(score=80.0),
        safety=SimpleNamespace(score=90.0, hard_veto=False),
        liquidity=SimpleNamespace(status=LiquidityStatus.GOOD, veto=False),
        state=SimpleNamespace(state=SetupState.OBSERVE, invalidation="x", trigger=None),
        score_acceleration=1.0,
        features=None,
        moonshot=MoonshotAssessment(
            score=score,
            ignition=score,
            headroom=50.0,
            capacity=None,
            stage=stage,
            timeframe="1d",
            multiple_to_window_high=9.1,
            reasons=["120 1d bars inside one range"],
            caveats=["that high is old"],
            unknowns=["market cap unknown"],
        ),
    )


def test_a_strong_candidate_fires_a_moonshot_alert_of_its_own_kind():
    engine = AlertEngine(CryptoPulseSettings().alerts, CryptoPulseSettings().scoring)
    alerts = engine.evaluate_moonshots([_result_with_moonshot(85.0, MoonshotStage.IGNITION)], FIXED_NOW_MS)

    assert len(alerts) == 1
    a = alerts[0]
    assert a.kind is AlertKind.MOONSHOT
    assert a.moonshot_stage == "IGNITION"
    assert "MOONSHOT" in a.headline
    # Both what argues against it and what was never measured reach the reader.
    assert "that high is old" in a.risks
    assert "market cap unknown" in a.risks


def test_an_exhausted_or_neutral_asset_never_fires_a_moonshot_alert():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    for stage in (MoonshotStage.EXHAUSTION, MoonshotStage.EXPANSION, MoonshotStage.NEUTRAL, MoonshotStage.DORMANT):
        assert engine.evaluate_moonshots([_result_with_moonshot(95.0, stage)], FIXED_NOW_MS) == []


def test_a_vetoed_asset_never_fires_however_good_the_reading():
    """A market cap small enough for a ×10 is worthless if you cannot get out."""
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    result = _result_with_moonshot(95.0, MoonshotStage.IGNITION)
    result.liquidity.veto = True
    assert engine.evaluate_moonshots([result], FIXED_NOW_MS) == []


def test_the_same_base_does_not_re_alert_on_every_scan():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    result = _result_with_moonshot(85.0, MoonshotStage.IGNITION)

    assert len(engine.evaluate_moonshots([result], FIXED_NOW_MS)) == 1
    one_hour_later = FIXED_NOW_MS + 3_600_000
    assert engine.evaluate_moonshots([result], one_hour_later) == []
    # A daily base is not news again an hour later, but it is after the cooldown.
    later = FIXED_NOW_MS + (settings.alerts.moonshot_cooldown_seconds + 60) * 1000
    assert len(engine.evaluate_moonshots([result], later)) == 1


def test_a_stage_change_is_news_and_re_alerts_inside_the_cooldown():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    accumulating = _result_with_moonshot(85.0, MoonshotStage.ACCUMULATION)
    assert len(engine.evaluate_moonshots([accumulating], FIXED_NOW_MS)) == 1

    igniting = _result_with_moonshot(85.0, MoonshotStage.IGNITION)
    assert len(engine.evaluate_moonshots([igniting], FIXED_NOW_MS + 60_000)) == 1


# --------------------------------------------------------------------------- #
# Delivery happens as part of a scan, not only in the CLI
# --------------------------------------------------------------------------- #


async def test_running_a_scan_delivers_its_alerts(tmp_path):
    reset_engine()
    settings = _settings(tmp_path, scanner__min_quote_volume_24h=100_000.0)
    service = ScannerService(settings)
    await service.run_once()

    path = tmp_path / "alerts.jsonl"
    if service.last_alerts:
        lines = path.read_text().strip().split("\n")
        assert len(lines) == len(service.last_alerts)
        assert json.loads(lines[0])["symbol"] == service.last_alerts[0].symbol
        assert [d.channel for d in service.notifiers.last_results] == ["jsonl"]
    else:
        assert not path.exists()  # nothing fired, nothing written
    await service.stop()
    reset_engine()


async def test_the_radar_command_runs_a_full_cycle_and_shuts_down_cleanly(tmp_path, monkeypatch, capsys):
    reset_engine()
    monkeypatch.setenv("CP_DB_URL", f"sqlite:///{tmp_path}/cli.db")
    monkeypatch.setenv("CP_ALERT_CHANNELS", "jsonl")
    monkeypatch.setenv("CP_ALERT_JSONL_PATH", str(tmp_path / "cli-alerts.jsonl"))
    from cryptopulse.config.settings import reset_settings_cache

    reset_settings_cache()

    args = argparse.Namespace(
        provider="fixture", universe="robinhood", rank="moonshot", valuation=None,
        interval=None, once=True,
    )
    assert await cmd_radar(args) == 0

    out = capsys.readouterr().out
    assert "CRYPTO PULSE AI — RADAR" in out
    assert "SYNTHETIC — NOT MARKET DATA" in out  # the fixture must announce itself
    assert "alert channel   jsonl      ready" in out
    assert "cycle 1" in out
    assert "1 cycle(s) completed" in out

    reset_settings_cache()
    reset_engine()


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_path):
    reset_engine()
    settings = _settings(tmp_path, scanner__min_quote_volume_24h=100_000.0)
    service = ScannerService(settings)
    set_service(service)
    app = create_app(start_loop=False)
    with TestClient(app) as c:
        yield c
    set_service(None)
    reset_engine()


def test_the_moonshot_endpoint_ranks_and_declares_what_it_is(client):
    assert client.get("/api/moonshot").status_code == 503  # no scan yet

    client.post("/api/scan/run")
    body = client.get("/api/moonshot").json()

    assert body["meta"]["engine_version"] == "MOONSHOT_ENGINE_V1"
    assert body["meta"]["timeframe"] == "1d"
    assert "not to be read as a likelihood" in body["meta"]["disclaimer"]

    scores = [r["moonshot"]["score"] for r in body["results"]]
    assert scores == sorted(scores, reverse=True)
    row = body["results"][0]
    for key in ("symbol", "moonshot", "pump_maturity", "liquidity", "setup_state"):
        assert key in row
    assert row["moonshot"]["stage"] != "UNKNOWN"


def test_the_moonshot_endpoint_filters_by_stage_and_score(client):
    client.post("/api/scan/run")
    filtered = client.get("/api/moonshot", params={"stage": "ACCUMULATION", "min_score": 0}).json()
    assert all(r["moonshot"]["stage"] == "ACCUMULATION" for r in filtered["results"])

    high = client.get("/api/moonshot", params={"min_score": 99.9}).json()
    assert high["results"] == []


def test_the_universe_endpoint_shows_what_is_scanned_and_what_is_missing(client):
    client.post("/api/scan/run")
    body = client.get("/api/universe").json()

    assert body["mode"] == "robinhood"
    resolution = body["resolution"]
    assert resolution["count"] > 5
    assert "BTCUSDT" in resolution["symbols"]
    # Listed assets this venue does not carry are named, not hidden.
    assert resolution["missing"]
    assert any("NOT from Robinhood" in n for n in resolution["notes"])


def test_health_reports_where_alerts_go_and_how_the_x10_layer_is_configured(client):
    body = client.get("/api/health").json()
    assert body["moonshot"]["engine_version"] == "MOONSHOT_ENGINE_V1"
    assert body["moonshot"]["target_multiple"] == 10.0
    assert body["moonshot"]["valuation_source"] == "none"
    assert body["universe"]["mode"] == "robinhood"
    assert [c["channel"] for c in body["alert_delivery"]["channels"]] == ["jsonl"]


def test_config_declares_the_moonshot_weights_are_a_hypothesis(client):
    body = client.get("/api/config").json()
    assert body["moonshot"]["weights"]["ignition"] > 0
    assert "must not be read as a probability" in body["moonshot"]["disclaimer"]
    assert "never been fitted" in body["moonshot"]["disclaimer"]
