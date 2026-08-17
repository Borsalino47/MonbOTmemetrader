"""Staleness and data-confidence behaviour.

The rule being enforced: old data is never presented as live. Confidence must
fall as data ages, and the reason must appear in the payload.
"""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.types import DataQuality, Provenance, Timeframe
from cryptopulse.features.pipeline import AssetFeatures, TimeframeFeatures
from cryptopulse.scoring.confidence import compute_confidence
from cryptopulse.scoring.engine import ScoreEngine
from tests.conftest import FIXED_NOW_MS, code, make_series

SETTINGS = CryptoPulseSettings()


def _asset(n=300, end_time_ms=FIXED_NOW_MS, tfs=None) -> AssetFeatures:
    rng = np.random.default_rng(31)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.004, n)))
    tfs = tfs or (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)
    per_tf = {
        tf: TimeframeFeatures.build(make_series(closes, timeframe=tf, end_time_ms=end_time_ms), min_bars=60)
        for tf in tfs
    }
    return AssetFeatures(
        symbol="TESTUSDT", primary_timeframe=Timeframe.M5, timeframes=per_tf, quote_volume_24h=50_000_000.0
    )


def test_provenance_age_tracks_the_clock():
    p = Provenance(source="test", as_of_ms=FIXED_NOW_MS - 90_000, fetched_at_ms=FIXED_NOW_MS)
    assert p.age_seconds(FIXED_NOW_MS) == pytest.approx(90.0)
    assert p.to_dict(FIXED_NOW_MS)["age_seconds"] == pytest.approx(90.0)


def test_age_is_never_negative_for_a_future_timestamp():
    p = Provenance(source="test", as_of_ms=FIXED_NOW_MS + 5000, fetched_at_ms=FIXED_NOW_MS)
    assert p.age_seconds(FIXED_NOW_MS) == 0.0


def test_confidence_falls_as_data_ages():
    af = _asset()
    fresh = compute_confidence(af, SETTINGS.scanner, FIXED_NOW_MS)
    stale = compute_confidence(af, SETTINGS.scanner, FIXED_NOW_MS + 45 * 60 * 1000)
    assert stale.score < fresh.score - 15
    assert any(code(i) == "CONF_STALE" for i in stale.issues)


def test_fresh_data_reports_no_stale_issue():
    c = compute_confidence(_asset(), SETTINGS.scanner, FIXED_NOW_MS)
    assert not any(code(i) == "CONF_STALE" for i in c.issues)


def test_missing_order_book_is_reported_as_an_issue():
    c = compute_confidence(_asset(), SETTINGS.scanner, FIXED_NOW_MS)
    assert any(code(i) == "CONF_NO_BOOK" for i in c.issues)
    assert c.components["order_book"] == 0.0


def test_order_book_presence_raises_confidence():
    af = _asset()
    without = compute_confidence(af, SETTINGS.scanner, FIXED_NOW_MS)
    af.order_book_imbalance = 0.12
    with_book = compute_confidence(af, SETTINGS.scanner, FIXED_NOW_MS)
    assert with_book.score > without.score


def test_shallow_history_lowers_confidence_and_says_so():
    shallow = compute_confidence(_asset(n=70), SETTINGS.scanner, FIXED_NOW_MS)
    deep = compute_confidence(_asset(n=300), SETTINGS.scanner, FIXED_NOW_MS)
    assert shallow.score < deep.score
    assert shallow.components["history_depth"] < deep.components["history_depth"]


def test_missing_timeframes_lower_coverage():
    partial = compute_confidence(_asset(tfs=(Timeframe.M5, Timeframe.M15)), SETTINGS.scanner, FIXED_NOW_MS)
    full = compute_confidence(_asset(), SETTINGS.scanner, FIXED_NOW_MS)
    assert partial.components["timeframe_coverage"] < full.components["timeframe_coverage"]
    assert any(code(i) == "CONF_MISSING_TF" for i in partial.issues)


def test_confidence_is_bounded():
    for offset in (0, 60_000, 3_600_000, 86_400_000):
        c = compute_confidence(_asset(), SETTINGS.scanner, FIXED_NOW_MS + offset)
        assert 0.0 <= c.score <= 100.0


def test_partial_quality_series_is_penalised():
    af = _asset()
    primary = af.timeframes[Timeframe.M5]
    primary.provenance = Provenance(
        source="test", as_of_ms=FIXED_NOW_MS, fetched_at_ms=FIXED_NOW_MS, quality=DataQuality.PARTIAL
    )
    c = compute_confidence(af, SETTINGS.scanner, FIXED_NOW_MS)
    assert c.components["continuity"] < 1.0
    assert any(code(i) == "CONF_PARTIAL_TF" for i in c.issues)


def test_stale_data_does_not_silently_produce_a_premium_signal():
    """A very old feed must not surface as an actionable premium setup."""
    engine = ScoreEngine(SETTINGS)
    af = _asset()
    result = engine.score(af, FIXED_NOW_MS + 6 * 60 * 60 * 1000)  # six hours later
    assert result.confidence.score < 60
    assert not result.is_premium
    assert any(code(r) == "CONF_STALE" for r in result.risks())


def test_frozen_clock_is_deterministic():
    clock = FrozenClock(FIXED_NOW_MS)
    assert clock.now_ms() == FIXED_NOW_MS == clock.now_ms()
    clock.advance(30)
    assert clock.now_ms() == FIXED_NOW_MS + 30_000
