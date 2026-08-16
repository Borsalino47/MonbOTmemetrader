"""Scoring behaviour: weights, explainability, maturity, states."""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import AssetFeatures, Bias, TimeframeFeatures
from cryptopulse.scoring.acceleration import compute_acceleration
from cryptopulse.scoring.components import score_orderflow, score_volume
from cryptopulse.scoring.engine import ScoreEngine
from cryptopulse.scoring.pump_maturity import compute_pump_maturity
from cryptopulse.scoring.states import SetupState
from tests.conftest import FIXED_NOW_MS, make_series


def _asset(closes, volumes=None, *, symbol="TESTUSDT", quote_volume=50_000_000.0, tfs=None) -> AssetFeatures:
    tfs = tfs or (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)
    per_tf = {
        tf: TimeframeFeatures.build(make_series(closes, timeframe=tf, volumes=volumes), min_bars=60) for tf in tfs
    }
    return AssetFeatures(
        symbol=symbol,
        primary_timeframe=Timeframe.M5,
        timeframes=per_tf,
        quote_volume_24h=quote_volume,
    )


def _flat(n=300, price=100.0):
    rng = np.random.default_rng(11)
    return price * np.exp(np.cumsum(rng.normal(0, 0.002, n)))


def _rising(n=300, price=100.0):
    rng = np.random.default_rng(12)
    return price * np.exp(np.cumsum(rng.normal(0.0012, 0.003, n)))


# --------------------------------------------------------------------------- #
# Engine invariants
# --------------------------------------------------------------------------- #


def test_weights_sum_to_100_or_engine_refuses_to_start():
    settings = CryptoPulseSettings()
    ScoreEngine(settings)  # must not raise
    settings.scoring.w_volume = 25.0  # total now 105
    with pytest.raises(ValueError, match="must sum to 100"):
        ScoreEngine(settings)


def test_final_equals_raw_minus_penalty_and_never_negative():
    engine = ScoreEngine(CryptoPulseSettings())
    r = engine.score(_asset(_rising()), FIXED_NOW_MS)
    assert r.final_score == pytest.approx(max(0.0, r.raw_score - r.risk_penalty))
    assert r.final_score >= 0.0
    assert 0.0 <= r.raw_score <= 100.0


def test_component_points_never_exceed_their_maximum():
    engine = ScoreEngine(CryptoPulseSettings())
    for closes in (_flat(), _rising()):
        r = engine.score(_asset(closes), FIXED_NOW_MS)
        for c in r.components:
            assert 0.0 <= c.points <= c.max_points, f"{c.name} out of range"


def test_raw_score_equals_sum_of_components():
    engine = ScoreEngine(CryptoPulseSettings())
    r = engine.score(_asset(_rising()), FIXED_NOW_MS)
    assert r.raw_score == pytest.approx(sum(c.points for c in r.components))


def test_every_scoring_component_is_explained():
    """Every component must justify its points, either positively or as a caveat."""
    engine = ScoreEngine(CryptoPulseSettings())
    for closes in (_rising(), _flat()):
        r = engine.score(_asset(closes), FIXED_NOW_MS)
        for c in r.components:
            assert c.reasons or c.caveats, f"component {c.name} produced no explanation"


def test_caveats_are_reported_as_risks_not_as_reasons_to_buy():
    """A stretched RSI explains the score but must never appear under "why"."""
    engine = ScoreEngine(CryptoPulseSettings())
    base = _flat(280)
    vertical = np.concatenate([base, base[-1] * np.cumprod(np.full(20, 1.03))])
    r = engine.score(_asset(vertical), FIXED_NOW_MS)

    caveats = [c for comp in r.components for c in comp.caveats]
    assert caveats, "an extended, stretched asset should raise at least one caveat"
    why = r.why()
    risks = r.risks()
    for c in caveats:
        assert c not in why, f"caveat leaked into the positive case: {c!r}"
        assert c in risks, f"caveat missing from risks: {c!r}"


def test_engine_version_and_fingerprint_travel_with_the_score():
    settings = CryptoPulseSettings()
    engine = ScoreEngine(settings)
    r = engine.score(_asset(_rising()), FIXED_NOW_MS)
    assert r.engine_version == "SCORE_ENGINE_V1"
    assert len(r.weights_fingerprint) == 12

    settings2 = CryptoPulseSettings()
    settings2.scoring.w_volume = 25.0
    settings2.scoring.w_momentum = 10.0  # keep the total at 100
    assert ScoreEngine(settings2).weights_fingerprint != engine.weights_fingerprint


def test_score_is_never_presented_as_a_probability():
    engine = ScoreEngine(CryptoPulseSettings())
    payload = engine.score(_asset(_rising()), FIXED_NOW_MS).to_dict()
    assert payload["opportunity_label"].endswith("/100")

    # No percent-suffixed score anywhere, and no field that claims to be one.
    # The word "probability" is allowed only where the payload *denies* being
    # one — the verdict's caveat says so deliberately, and stripping that
    # sentence out would remove the very disclaimer this test exists to protect.
    blob = str(payload).lower()
    for phrase in ("probability of", "chance of", "% probability", "win probability"):
        assert phrase not in blob, f"payload implies a probability via {phrase!r}"
    for mention in ("probability", "probabilité"):
        for occurrence in _contexts(blob, mention):
            assert "not a probability" in occurrence or "ni un conseil ni une probabilité" in occurrence, (
                f"the word {mention!r} appears outside a disclaimer: {occurrence!r}"
            )


def _contexts(blob: str, word: str, width: int = 60) -> list[str]:
    """Every occurrence of `word` with the text around it, for inspection."""
    out, start = [], 0
    while (i := blob.find(word, start)) != -1:
        out.append(blob[max(0, i - width) : i + len(word) + width])
        start = i + len(word)
    return out


def test_every_row_carries_a_four_level_verdict():
    engine = ScoreEngine(CryptoPulseSettings())
    verdict = engine.score(_asset(_rising()), FIXED_NOW_MS).to_dict()["verdict"]
    assert verdict["level"] in {"STRONG", "WATCH", "RISKY", "AVOID"}
    assert verdict["emoji"] and verdict["label"] and verdict["label_fr"]
    # A badge with no caveat is how a ranking gets read as a prediction.
    assert "not a probability" in verdict["caveat"]


# --------------------------------------------------------------------------- #
# Unknown data must not earn points
# --------------------------------------------------------------------------- #


def test_missing_order_book_scores_zero_and_is_flagged_unavailable():
    af = _asset(_rising())
    assert af.order_book_imbalance is None
    cs = score_orderflow(af, CryptoPulseSettings().scoring)
    assert cs.points == 0.0
    assert cs.available is False
    assert "DATA_UNAVAILABLE" in " ".join(cs.reasons)


def test_bullish_order_book_scores_higher_than_bearish():
    cfg = CryptoPulseSettings().scoring
    bull = _asset(_rising())
    bull.order_book_imbalance = 0.40
    bear = _asset(_rising())
    bear.order_book_imbalance = -0.30
    assert score_orderflow(bull, cfg).points > score_orderflow(bear, cfg).points


def test_rising_volume_outscores_flat_high_volume():
    """The premise of the product: acceleration beats a high plateau."""
    cfg = CryptoPulseSettings().scoring
    n = 300
    plateau = np.concatenate([np.full(n - 60, 100.0), np.full(60, 250.0)])
    ramping = np.concatenate([np.full(n - 5, 100.0), np.array([120.0, 150.0, 190.0, 240.0, 300.0])])

    closes = _flat(n)
    flat_high = score_volume(_asset(closes, plateau), cfg)
    accelerating = score_volume(_asset(closes, ramping), cfg)
    assert accelerating.points > flat_high.points, (
        f"accelerating volume scored {accelerating.points:.2f} vs plateau {flat_high.points:.2f}"
    )


# --------------------------------------------------------------------------- #
# Pump maturity
# --------------------------------------------------------------------------- #


def test_pump_maturity_higher_after_a_vertical_move():
    calm = _asset(_flat())
    pumped_closes = np.concatenate([_flat(280), _flat(280)[-1] * np.cumprod(np.full(20, 1.02))])
    pumped = _asset(pumped_closes)
    assert compute_pump_maturity(pumped).score > compute_pump_maturity(calm).score + 20


def test_pump_maturity_is_bounded_and_reports_coverage():
    m = compute_pump_maturity(_asset(_rising()))
    assert 0.0 <= m.score <= 100.0
    assert 0.0 < m.coverage <= 1.0


def test_extended_move_is_penalised_not_rewarded():
    """A coin that already went vertical must not simply score higher."""
    engine = ScoreEngine(CryptoPulseSettings())
    base = _flat(280)
    vertical = np.concatenate([base, base[-1] * np.cumprod(np.full(20, 1.03))])
    r = engine.score(_asset(vertical), FIXED_NOW_MS)
    assert r.maturity.score > 60
    assert r.risk_penalty > 0
    assert any(p.name == "pump_maturity" for p in r.penalties.items)


def test_acceleration_discounts_a_mature_move():
    af = _asset(_rising())
    from cryptopulse.scoring.pump_maturity import PumpMaturity

    young = compute_acceleration(af, PumpMaturity(score=10.0))
    late = compute_acceleration(af, PumpMaturity(score=90.0))
    assert young.early_move > late.early_move
    assert young.momentum_acceleration == pytest.approx(late.momentum_acceleration)


# --------------------------------------------------------------------------- #
# States
# --------------------------------------------------------------------------- #


def test_downtrend_yields_a_non_actionable_state():
    engine = ScoreEngine(CryptoPulseSettings())
    rng = np.random.default_rng(13)
    falling = 100 * np.exp(np.cumsum(rng.normal(-0.0018, 0.003, 300)))
    r = engine.score(_asset(falling), FIXED_NOW_MS)
    assert r.state.state in (SetupState.IGNORE, SetupState.INVALIDATED, SetupState.OBSERVE)
    assert not r.is_premium


def test_hard_veto_blocks_promotion_regardless_of_score():
    engine = ScoreEngine(CryptoPulseSettings())
    # 90k quote volume is below the DANGEROUS floor.
    r = engine.score(_asset(_rising(), quote_volume=90_000.0), FIXED_NOW_MS)
    assert r.liquidity.veto or r.safety.hard_veto
    assert r.state.state in (SetupState.IGNORE, SetupState.OBSERVE)
    assert not r.is_premium


def test_state_carries_a_trigger_and_an_invalidation_when_actionable():
    engine = ScoreEngine(CryptoPulseSettings())
    for closes in (_rising(), _flat()):
        r = engine.score(_asset(closes), FIXED_NOW_MS)
        if r.state.state in (SetupState.ARMED, SetupState.WATCH, SetupState.BREAKOUT):
            assert r.state.trigger, "an actionable state must state its trigger"
            assert r.state.invalidation, "an actionable state must state what invalidates it"


# --------------------------------------------------------------------------- #
# Multi-timeframe
# --------------------------------------------------------------------------- #


def test_mtf_alignment_beats_conflict():
    engine = ScoreEngine(CryptoPulseSettings())
    aligned = _asset(_rising())
    conflicted = _asset(_rising())
    # Force the higher timeframes bearish.
    for tf in (Timeframe.H1, Timeframe.H4):
        conflicted.timeframes[tf].bias = Bias.BEARISH

    a = engine.score(aligned, FIXED_NOW_MS)
    c = engine.score(conflicted, FIXED_NOW_MS)
    mtf_a = next(x for x in a.components if x.name == "mtf")
    mtf_c = next(x for x in c.components if x.name == "mtf")
    assert mtf_a.points > mtf_c.points
    assert c.risk_penalty > a.risk_penalty


def test_single_timeframe_marks_mtf_unavailable():
    engine = ScoreEngine(CryptoPulseSettings())
    r = engine.score(_asset(_rising(), tfs=(Timeframe.M5,)), FIXED_NOW_MS)
    mtf = next(c for c in r.components if c.name == "mtf")
    assert mtf.available is False and mtf.points == 0.0
