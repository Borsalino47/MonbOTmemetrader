"""Scanner pipeline, temporal memory and alert deduplication."""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.alerts.engine import AlertEngine, AlertLevel
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import AssetFeatures, TimeframeFeatures
from cryptopulse.providers.fixture import FixtureProvider
from cryptopulse.scanner.cex import CexScanner
from cryptopulse.scanner.memory import ScoreMemory, ScorePoint
from cryptopulse.scoring.engine import ScoreEngine
from cryptopulse.scoring.states import SetupState
from tests.conftest import FIXED_NOW_MS, make_series

pytestmark = pytest.mark.asyncio


def _settings() -> CryptoPulseSettings:
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    s.scanner.candles_per_timeframe = 300
    s.scanner.min_quote_volume_24h = 100_000.0
    return s


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #


async def test_memory_records_and_returns_history():
    m = ScoreMemory()
    for i, score in enumerate([51.0, 58.0, 67.0, 76.0, 84.0]):
        m.record("SOLUSDT", ScorePoint(FIXED_NOW_MS + i * 300_000, score, score, 100.0, "WATCH"))
    history = m.history("SOLUSDT")
    assert [p.final_score for p in history] == [51.0, 58.0, 67.0, 76.0, 84.0]


async def test_memory_detects_a_rising_score():
    m = ScoreMemory(acceleration_window_seconds=1200)
    for i, score in enumerate([51.0, 58.0, 67.0, 76.0, 84.0]):
        m.record("SOLUSDT", ScorePoint(FIXED_NOW_MS + i * 300_000, score, score, 100.0, "WATCH"))
    now = FIXED_NOW_MS + 4 * 300_000
    delta, reference = m.acceleration("SOLUSDT", now)
    assert delta is not None and delta > 0
    assert reference == 51.0, "the 20-minute-old reading is the correct baseline"


async def test_memory_reports_no_acceleration_for_a_flat_score():
    m = ScoreMemory(acceleration_window_seconds=1200)
    for i in range(5):
        m.record("FLATUSDT", ScorePoint(FIXED_NOW_MS + i * 300_000, 80.0, 80.0, 100.0, "WATCH"))
    delta, _ = m.acceleration("FLATUSDT", FIXED_NOW_MS + 4 * 300_000)
    assert delta == pytest.approx(0.0)


async def test_memory_returns_none_before_it_has_a_baseline():
    """A first observation must not manufacture acceleration out of nothing."""
    m = ScoreMemory()
    m.record("NEWUSDT", ScorePoint(FIXED_NOW_MS, 90.0, 90.0, 1.0, "ARMED"))
    assert m.acceleration("NEWUSDT", FIXED_NOW_MS) == (None, None)


async def test_memory_overwrites_a_repeat_of_the_same_candle():
    m = ScoreMemory()
    m.record("X", ScorePoint(FIXED_NOW_MS, 50.0, 50.0, 1.0, "WATCH"))
    m.record("X", ScorePoint(FIXED_NOW_MS, 55.0, 55.0, 1.0, "WATCH"))
    history = m.history("X")
    assert len(history) == 1 and history[0].final_score == 55.0


async def test_memory_respects_its_bound():
    m = ScoreMemory(maxlen=10)
    for i in range(50):
        m.record("X", ScorePoint(FIXED_NOW_MS + i * 1000, float(i), float(i), 1.0, "WATCH"))
    assert len(m.history("X")) == 10


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #


async def test_scan_produces_ranked_results():
    scanner = CexScanner(_settings(), clock=FrozenClock(FIXED_NOW_MS))
    report = await scanner.scan()
    assert report.succeeded > 10
    assert report.synthetic_data is True
    assert any("SYNTHETIC" in n for n in report.notes)
    scores = [r.final_score for r in report.results]
    # The ranking key is not the raw score, but the list must still be ordered by
    # it. It depends on the configured rank mode, so it is read off the instance.
    keys = [scanner._rank_key(r) for r in report.results]
    assert keys == sorted(keys, reverse=True)
    assert all(0 <= s <= 100 for s in scores)
    await scanner.close()


async def test_scan_survives_a_symbol_that_fails():
    """One broken asset must not abort the whole pass."""
    settings = _settings()
    provider = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS))
    original = provider.get_ohlcv

    async def flaky(symbol, timeframe, limit):
        # Fail only the deep history fetch, so the symbol still clears the cheap
        # ticker pre-filter and actually reaches the scoring phase before breaking.
        if symbol == "SOLUSDT" and limit >= 100:
            raise RuntimeError("simulated provider failure")
        return await original(symbol, timeframe, limit)

    provider.get_ohlcv = flaky
    scanner = CexScanner(settings, provider=provider, clock=FrozenClock(FIXED_NOW_MS))
    report = await scanner.scan()

    assert "SOLUSDT" in report.errors
    assert report.succeeded > 5, "the rest of the universe should still have scored"
    assert report.failed >= 1
    await scanner.close()


async def test_scan_aborts_cleanly_when_the_source_is_down():
    from cryptopulse.providers.base import ProviderHealth

    provider = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS))

    async def dead_health():
        return ProviderHealth(name="dead", available=False, detail="connection refused")

    provider.health = dead_health
    scanner = CexScanner(_settings(), provider=provider, clock=FrozenClock(FIXED_NOW_MS))
    report = await scanner.scan()

    assert report.succeeded == 0
    assert report.results == []
    assert any("SOURCE_UNAVAILABLE" in e for e in report.errors.values())
    await scanner.close()


async def test_illiquid_asset_is_vetoed_not_ranked_at_the_top():
    scanner = CexScanner(_settings(), clock=FrozenClock(FIXED_NOW_MS))
    report = await scanner.scan()
    micro = next((r for r in report.results if r.symbol == "MICROUSDT"), None)
    if micro is not None:  # it may be filtered out by the volume pre-filter
        assert micro.liquidity.veto or micro.safety.hard_veto
        assert not micro.is_premium
    await scanner.close()


async def test_scan_records_score_history_across_passes():
    memory = ScoreMemory()
    clock = FrozenClock(FIXED_NOW_MS)
    scanner = CexScanner(_settings(), memory=memory, clock=clock)
    await scanner.scan()
    clock.advance(600)
    await scanner.scan()
    tracked = [s for s in memory.symbols() if len(memory.history(s)) >= 1]
    assert tracked
    await scanner.close()


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #


def _result(score=85.0, *, maturity=30.0, confidence=90.0, state=SetupState.ARMED, symbol="TESTUSDT", accel=10.0):
    """Build a real ScoreResult and adjust the fields under test."""
    settings = CryptoPulseSettings()
    engine = ScoreEngine(settings)
    rng = np.random.default_rng(51)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0006, 0.003, 300)))
    per_tf = {
        tf: TimeframeFeatures.build(make_series(closes, timeframe=tf), min_bars=60)
        for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1)
    }
    af = AssetFeatures(
        symbol=symbol, primary_timeframe=Timeframe.M5, timeframes=per_tf, quote_volume_24h=200_000_000.0
    )
    r = engine.score(af, FIXED_NOW_MS)
    r.final_score = score
    r.maturity.score = maturity
    r.confidence.score = confidence
    r.state.state = state
    r.score_acceleration = accel
    r.safety.hard_veto = False
    r.liquidity.veto = False
    return r


async def test_alert_fires_when_every_gate_passes():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    alerts = engine.evaluate([_result()], FIXED_NOW_MS)
    assert len(alerts) == 1
    assert alerts[0].level is AlertLevel.CRITICAL_SETUP


async def test_alert_suppressed_by_a_liquidity_veto():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    r = _result()
    r.liquidity.veto = True
    assert engine.evaluate([r], FIXED_NOW_MS) == []


async def test_alert_suppressed_by_a_safety_veto():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    r = _result()
    r.safety.hard_veto = True
    assert engine.evaluate([r], FIXED_NOW_MS) == []


async def test_alert_suppressed_when_the_pump_is_already_mature():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    assert engine.evaluate([_result(maturity=95.0)], FIXED_NOW_MS) == []


async def test_alert_suppressed_on_low_data_confidence():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    assert engine.evaluate([_result(confidence=20.0)], FIXED_NOW_MS) == []


async def test_identical_alert_does_not_repeat_inside_the_cooldown():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    assert len(engine.evaluate([_result()], FIXED_NOW_MS)) == 1
    assert engine.evaluate([_result()], FIXED_NOW_MS + 60_000) == [], "same setup, same scan minute later"


async def test_alert_repeats_after_the_cooldown_expires():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    engine.evaluate([_result()], FIXED_NOW_MS)
    later = FIXED_NOW_MS + (settings.alerts.cooldown_seconds + 60) * 1000
    assert len(engine.evaluate([_result()], later)) == 1


async def test_state_change_bypasses_the_cooldown():
    """ARMED becoming BREAKOUT is genuine news, not a repeat."""
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    engine.evaluate([_result(state=SetupState.ARMED)], FIXED_NOW_MS)
    fired = engine.evaluate([_result(state=SetupState.BREAKOUT)], FIXED_NOW_MS + 60_000)
    assert len(fired) == 1 and fired[0].state == "BREAKOUT"


async def test_alert_levels_follow_the_score():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    cases = [(90.0, AlertLevel.CRITICAL_SETUP), (75.0, AlertLevel.HIGH), (63.0, AlertLevel.WATCH), (52.0, AlertLevel.INFO)]
    for score, expected in cases:
        fired = engine.evaluate([_result(score=score, symbol=f"S{int(score)}USDT")], FIXED_NOW_MS)
        assert fired and fired[0].level is expected, f"score {score} produced {fired}"


async def test_low_score_produces_no_alert():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    assert engine.evaluate([_result(score=20.0)], FIXED_NOW_MS) == []


async def test_alert_text_never_claims_a_probability():
    settings = CryptoPulseSettings()
    engine = AlertEngine(settings.alerts, settings.scoring)
    text = engine.evaluate([_result()], FIXED_NOW_MS)[0].format_text()
    assert "Opportunity Score: 85/100" in text
    assert "%" not in text.split("Opportunity Score")[1].split("\n")[0]
    assert "probability" not in text.lower()


async def test_alerts_are_capped_per_scan():
    settings = CryptoPulseSettings()
    settings.alerts.max_alerts_per_scan = 3
    engine = AlertEngine(settings.alerts, settings.scoring)
    results = [_result(symbol=f"SYM{i}USDT") for i in range(10)]
    assert len(engine.evaluate(results, FIXED_NOW_MS)) == 3
