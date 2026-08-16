"""EXPLOSION_15M_SCORE.

The property that matters most here is not any single weight — those are an
unvalidated hypothesis and the tests say so — but that this engine stays a
*separate* engine answering a *separate* question over a *stated* window. A
score that quietly started reading 4h indicators, or that got averaged into the
opportunity score, would still produce plausible numbers and would silently
destroy the one validation loop in this project that closes.
"""

from __future__ import annotations

import pytest

from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import AssetFeatures, Bias, TimeframeFeatures
from cryptopulse.features.structure import StructureReport, StructureTrend
from cryptopulse.scoring.explosion import (
    EXPLOSION_ENGINE_VERSION,
    EXPLOSION_HORIZON_MINUTES,
    EXPLOSION_WEIGHTS,
    ExplosionEngine,
)

NOW_MS = 1_700_000_000_000


def _structure(*, distance_atr: float | None, broke_out: bool = False, fake: bool = False):
    return StructureReport(
        trend=StructureTrend.UPTREND,
        swings=[],
        resistances=[],
        supports=[],
        nearest_resistance=None,
        nearest_support=None,
        distance_to_resistance_atr=distance_atr,
        distance_to_support_atr=None,
        broke_out=broke_out,
        fake_breakout=fake,
        in_retest=False,
        range_position=0.5,
        atr_value=1.0,
        notes=[],
    )


def _tf(
    tf: Timeframe,
    *,
    rvol=None,
    rvol_slope=None,
    roc1=None,
    roc3=None,
    compression=None,
    rsi=None,
    structure=None,
) -> TimeframeFeatures:
    return TimeframeFeatures(
        timeframe=tf,
        bars=200,
        close=100.0,
        open_time_ms=NOW_MS - 300_000,
        close_time_ms=NOW_MS,
        rvol=rvol,
        rvol_slope=rvol_slope,
        roc1=roc1,
        roc3=roc3,
        compression=compression,
        rsi14=rsi,
        structure=structure,
        bias=Bias.BULLISH,
    )


def _asset(timeframes: dict, *, imbalance=None, spread=None) -> AssetFeatures:
    return AssetFeatures(
        symbol="TESTUSDT",
        primary_timeframe=Timeframe.M5,
        timeframes=timeframes,
        order_book_imbalance=imbalance,
        spread_bps=spread,
    )


def _loud() -> AssetFeatures:
    """Everything this engine looks for, all at once."""
    return _asset(
        {
            Timeframe.M5: _tf(
                Timeframe.M5, rvol=3.2, rvol_slope=0.4, roc1=0.9, roc3=1.6, rsi=62.0,
                structure=_structure(distance_atr=0.3),
            ),
            Timeframe.M15: _tf(
                Timeframe.M15, rvol=2.4, roc1=1.4, compression=0.12,
                structure=_structure(distance_atr=0.4),
            ),
        },
        imbalance=0.42,
        spread=3.0,
    )


def _quiet() -> AssetFeatures:
    return _asset(
        {
            Timeframe.M5: _tf(
                Timeframe.M5, rvol=0.6, rvol_slope=-0.2, roc1=-0.05, roc3=-0.1, rsi=45.0,
                structure=_structure(distance_atr=6.0),
            ),
            Timeframe.M15: _tf(
                Timeframe.M15, rvol=0.7, roc1=-0.1, compression=0.92,
                structure=_structure(distance_atr=6.0),
            ),
        },
        imbalance=-0.3,
        spread=40.0,
    )


@pytest.fixture()
def engine() -> ExplosionEngine:
    return ExplosionEngine()


# --------------------------------------------------------------------------- #
# The engine's identity
# --------------------------------------------------------------------------- #


def test_weights_sum_to_one_hundred():
    """The score is presented on a 0-100 scale and must stay on it."""
    assert sum(EXPLOSION_WEIGHTS.values()) == pytest.approx(100.0)


def test_engine_refuses_to_construct_with_weights_that_do_not_sum_to_100(monkeypatch):
    monkeypatch.setitem(EXPLOSION_WEIGHTS, "volume_burst", 40.0)
    with pytest.raises(ValueError, match="must sum to 100"):
        ExplosionEngine()


def test_fingerprint_covers_the_horizon_not_only_the_weights(monkeypatch):
    """The same weights over a different window are a different claim.

    If the horizon were outside the fingerprint, changing 15m to 60m would let
    old rows be reinterpreted as though they had made the new claim.
    """
    before = ExplosionEngine().weights_fingerprint
    monkeypatch.setattr("cryptopulse.scoring.explosion.EXPLOSION_HORIZON_MINUTES", 60)
    after = ExplosionEngine()._fingerprint()
    assert before != after


def test_the_horizon_matches_the_one_the_outcome_tracker_measures():
    """This is the whole reason the score is falsifiable. `outcomes/horizons.py`
    records what the price did 15 minutes after a signal; if these two drifted
    apart the score would be measured against a window it never spoke about."""
    from cryptopulse.outcomes.horizons import HORIZONS

    assert any(h.minutes == EXPLOSION_HORIZON_MINUTES for h in HORIZONS)


# --------------------------------------------------------------------------- #
# Ordering — the only claim these weights can honestly make
# --------------------------------------------------------------------------- #


def test_a_loud_setup_outranks_a_quiet_one(engine):
    loud = engine.score(_loud(), timestamp_ms=NOW_MS)
    quiet = engine.score(_quiet(), timestamp_ms=NOW_MS)
    assert loud.score > quiet.score
    assert loud.label == "IMMINENT"
    assert quiet.label == "CALME"


def test_score_stays_inside_the_scale_it_is_presented_on(engine):
    for af in (_loud(), _quiet()):
        result = engine.score(af, timestamp_ms=NOW_MS, maturity_score=10.0)
        assert 0.0 <= result.score <= 100.0


def test_distance_to_resistance_dominates_the_horizon(engine):
    """A level five ATR away cannot be reached in fifteen minutes however loud
    the volume is. This is the component that makes the window mean something."""
    near = _loud()
    far = _loud()
    far.timeframes[Timeframe.M5].structure = _structure(distance_atr=5.5)
    far.timeframes[Timeframe.M15].structure = _structure(distance_atr=5.5)

    assert engine.score(near, timestamp_ms=NOW_MS).score > engine.score(
        far, timestamp_ms=NOW_MS
    ).score


def test_a_mature_move_scores_lower_than_a_young_one(engine):
    young = engine.score(_loud(), timestamp_ms=NOW_MS, maturity_score=15.0)
    late = engine.score(_loud(), timestamp_ms=NOW_MS, maturity_score=88.0)
    assert young.score > late.score
    assert any("retrace" in r for r in late.risks())


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_a_hard_gate_zeroes_the_score_rather_than_reducing_it(engine):
    """A reduced score would still let an untradeable token outrank a safe one,
    and on this horizon a ten-percent candle on no liquidity is a trap."""
    result = engine.score(
        _loud(), timestamp_ms=NOW_MS, maturity_score=10.0,
        hard_veto=True, veto_reason="liquidity gate: a fast move here could not be exited",
    )
    assert result.score == 0.0
    assert result.vetoed is True
    assert result.label == "BLOQUÉ"
    assert "liquidity gate" in result.risks()[0]


def test_a_veto_is_never_silent(engine):
    """A zero with no reason is indistinguishable from a calm market."""
    result = engine.score(_loud(), timestamp_ms=NOW_MS, hard_veto=True)
    assert result.veto_reason is not None and result.veto_reason.strip()


def test_missing_timeframes_score_zero_and_say_why(engine):
    """No 5m and no 15m means the question cannot be answered at all. It must
    not be answered with a low score that looks like a calm market."""
    af = _asset({Timeframe.H4: _tf(Timeframe.H4, rvol=5.0, roc1=9.0)})
    result = engine.score(af, timestamp_ms=NOW_MS)

    assert result.score == 0.0
    unavailable = [c for c in result.components if not c.available]
    assert len(unavailable) >= 3
    assert all(c.caveats for c in unavailable), "an unavailable input must say so"


def test_an_absent_order_book_scores_zero_and_is_not_treated_as_neutral(engine):
    """Most symbols never get a book fetched. Treating that as 'balanced' would
    hand free points to every unscanned token."""
    af = _loud()
    af.order_book_imbalance = None
    af.spread_bps = None

    book = next(c for c in engine.score(af, timestamp_ms=NOW_MS).components
                if c.name == "book_pressure")
    assert book.points == 0.0
    assert book.available is False
    assert any("order book" in c for c in book.caveats)


def test_no_component_awards_points_without_saying_why(engine):
    """The same rule the opportunity engine is held to. A number with no
    sentence behind it cannot be argued with."""
    for af in (_loud(), _quiet()):
        for maturity in (None, 10.0, 90.0):
            result = engine.score(af, timestamp_ms=NOW_MS, maturity_score=maturity)
            for c in result.components:
                if c.points > 0:
                    assert c.reasons, f"{c.name} scored {c.points} with no reason"


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def test_the_payload_states_its_window_and_denies_being_a_probability(engine):
    d = engine.score(_loud(), timestamp_ms=NOW_MS, maturity_score=20.0).to_dict()
    assert d["horizon_minutes"] == EXPLOSION_HORIZON_MINUTES
    assert d["engine_version"] == EXPLOSION_ENGINE_VERSION
    assert "not a probability" in d["disclaimer"]
    # A ranking, rendered as a ranking.
    assert "%" not in d["explosion_label"]


def test_label_bands_are_ordered(engine):
    assert engine.score(_loud(), timestamp_ms=NOW_MS, maturity_score=5.0).label == "IMMINENT"
    assert engine.score(_quiet(), timestamp_ms=NOW_MS, maturity_score=95.0).label == "CALME"


def test_this_engine_never_reads_a_timeframe_longer_than_15m(engine):
    """The guard against this slowly becoming a second opportunity score.

    A 4h reading cannot say anything about the next fifteen minutes, so adding
    one here would produce a number that still looked reasonable while no longer
    describing the window it claims.
    """
    short_only = _loud()
    with_slow = _loud()
    with_slow.timeframes[Timeframe.H4] = _tf(
        Timeframe.H4, rvol=9.0, roc1=12.0, compression=0.01, rsi=55.0,
        structure=_structure(distance_atr=0.05),
    )
    with_slow.timeframes[Timeframe.D1] = _tf(Timeframe.D1, rvol=9.0, roc1=30.0)

    a = engine.score(short_only, timestamp_ms=NOW_MS, maturity_score=20.0)
    b = engine.score(with_slow, timestamp_ms=NOW_MS, maturity_score=20.0)
    assert a.score == b.score, "a slow timeframe changed a fifteen-minute claim"


# --------------------------------------------------------------------------- #
# The journal — where this score has to survive to be testable later
# --------------------------------------------------------------------------- #


def test_a_scan_journals_the_explosion_score_with_its_own_version(tmp_path):
    """The score is only worth computing if it reaches disk beside the outcome
    it will eventually be judged against. Its own version and fingerprint travel
    with it so rows scored under old weights are never reinterpreted."""
    from cryptopulse.api.service import ScannerService, set_service
    from cryptopulse.config.settings import CryptoPulseSettings
    from cryptopulse.database.session import reset_engine

    reset_engine()
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.database.url = f"sqlite:///{tmp_path}/exp.db"
    settings.scanner.min_quote_volume_24h = 100_000.0

    from fastapi.testclient import TestClient

    from cryptopulse.api.app import create_app

    set_service(ScannerService(settings))
    try:
        with TestClient(create_app(start_loop=False)) as c:
            c.post("/api/scan/run")
            signals = c.get("/api/signals?limit=100").json()["signals"]
    finally:
        set_service(None)
        reset_engine()

    assert signals, "the fixture scan produced no persistable signals"
    scored = [s for s in signals if s.get("explosion_score") is not None]
    assert scored, "no signal carried an explosion score to the journal"
    for s in scored:
        assert s["explosion_engine_version"] == EXPLOSION_ENGINE_VERSION
        assert s["explosion_weights_fingerprint"]
        assert 0.0 <= s["explosion_score"] <= 100.0
