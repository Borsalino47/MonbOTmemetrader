"""Position health, exit risk, and the four decisions about something held.

The failure that matters here is the opposite of the one in the entry engine.
There, the danger is buying on noise. Here, it is **selling on noise** — and the
worst version of that is selling because a data request failed. A health score
that treats a missing reading as a bad one would do exactly that, so several of
these tests exist only to prove it does not.

The second protected property is that health is not PnL. A position up 12% on a
setup that has completely broken is unhealthy, and saying so is the whole point:
that is the moment the gain is about to be given back.
"""

from __future__ import annotations

import pytest

from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import AssetFeatures, Bias, TimeframeFeatures
from cryptopulse.features.structure import Level, StructureReport, StructureTrend, SwingKind
from cryptopulse.risk.liquidity import LiquidityAssessment, LiquidityStatus
from cryptopulse.risk.penalties import PenaltyReport
from cryptopulse.risk.safety import SafetyAssessment
from cryptopulse.scoring.acceleration import AccelerationScores
from cryptopulse.scoring.confidence import DataConfidence
from cryptopulse.scoring.engine import ScoreResult
from cryptopulse.scoring.explosion import ExplosionResult
from cryptopulse.scoring.pump_maturity import PumpMaturity
from cryptopulse.scoring.states import SetupState, StateDecision
from cryptopulse.trading.decision import TradeAction, TradeDecisionEngine
from cryptopulse.trading.exit_risk import Severity, assess_exit_risk
from cryptopulse.trading.health import MIN_COVERAGE, WEIGHTS, compute_health

NOW_MS = 1_700_000_000_000


class _Position:
    """The entry snapshot, exactly the fields the engines read from it."""

    def __init__(self, *, entry_price=100.0, invalidation_price=95.0,
                 entry_opportunity=85.0, entry_explosion=88.0, trigger_price=105.0):
        self.entry_price = entry_price
        self.invalidation_price = invalidation_price
        self.trigger_price = trigger_price
        self.entry_opportunity = entry_opportunity
        self.entry_explosion = entry_explosion


def _structure(*, trend=StructureTrend.UPTREND, support=95.0, resistance=115.0,
               fake_breakout=False, range_position=0.6) -> StructureReport:
    return StructureReport(
        trend=trend, swings=[], resistances=[], supports=[],
        nearest_resistance=Level(price=resistance, touches=3, kind=SwingKind.HIGH,
                                 last_touch_index=10, strength=0.8) if resistance else None,
        nearest_support=Level(price=support, touches=3, kind=SwingKind.LOW,
                              last_touch_index=8, strength=0.7) if support else None,
        distance_to_resistance_atr=1.0, distance_to_support_atr=1.0,
        broke_out=True, fake_breakout=fake_breakout, in_retest=False,
        range_position=range_position, atr_value=1.0, notes=[],
    )


def _result(
    *, price=108.0, opportunity=82.0, explosion=80.0, maturity=25.0,
    liquidity=LiquidityStatus.GOOD, liquidity_veto=False, safety_veto=False,
    state=SetupState.CONTINUATION, structure=None, bias=Bias.BULLISH,
    rvol=1.6, roc3=0.8, macd=1.2, macd_prev=1.0, rsi=62.0, climax=False,
    vol_accel=15.0, imbalance=0.2, spread=6.0, with_features=True,
    with_explosion=True,
) -> ScoreResult:
    features = None
    if with_features:
        tf = TimeframeFeatures(
            timeframe=Timeframe.M5, bars=200, close=price,
            open_time_ms=NOW_MS - 300_000, close_time_ms=NOW_MS,
            structure=structure if structure is not None else _structure(),
            bias=bias, rvol=rvol, roc3=roc3, macd_hist=macd, macd_hist_prev=macd_prev,
            rsi14=rsi, volume_climax=climax, volume_acceleration_pct=vol_accel,
        )
        features = AssetFeatures(
            symbol="TESTUSDT", primary_timeframe=Timeframe.M5, timeframes={Timeframe.M5: tf},
            order_book_imbalance=imbalance, spread_bps=spread,
        )

    result = ScoreResult(
        symbol="TESTUSDT", price=price, timestamp_ms=NOW_MS,
        raw_score=opportunity, risk_penalty=0.0, final_score=opportunity,
        components=[], penalties=PenaltyReport(total=0.0),
        maturity=PumpMaturity(score=maturity, reasons=[]),
        acceleration=AccelerationScores(momentum_acceleration=10.0, early_move=10.0, reasons=[]),
        confidence=DataConfidence(score=95.0, issues=[], max_age_seconds=20.0),
        liquidity=LiquidityAssessment(status=liquidity, fraction=0.8, veto=liquidity_veto, reasons=[]),
        safety=SafetyAssessment(score=95.0, hard_veto=safety_veto, reasons=["flagged"] if safety_veto else []),
        state=StateDecision(state=state, rationale="test", trigger="t", invalidation="i"),
        features=features,
    )
    if with_explosion:
        result.explosion = ExplosionResult(
            symbol="TESTUSDT", score=explosion, components=[],
            engine_version="EXPLOSION_ENGINE_V1", weights_fingerprint="abc", timestamp_ms=NOW_MS,
        )
    return result


@pytest.fixture()
def engine() -> TradeDecisionEngine:
    return TradeDecisionEngine()


def _decide(engine, position, result, *, price=None, drawdown=None):
    health = compute_health(position, result, current_price=price)
    risk = assess_exit_risk(position, result, current_price=price, drawdown_from_peak_pct=drawdown)
    return engine.decide_position(position, result, health, risk, current_price=price), health, risk


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


def test_weights_sum_to_one_hundred():
    assert sum(WEIGHTS.values()) == pytest.approx(100.0)


def test_an_intact_thesis_scores_healthy():
    health = compute_health(_Position(), _result())
    assert health.score is not None and health.score >= 70.0
    assert health.label_fr == "SAINE"
    assert health.reasons()


def test_a_broken_thesis_scores_unhealthy():
    broken = _result(
        price=94.0, opportunity=40.0, explosion=20.0,
        structure=_structure(trend=StructureTrend.DOWNTREND, support=98.0, range_position=0.1),
        bias=Bias.BEARISH, rvol=0.5, roc3=-2.0, macd=0.2, macd_prev=1.0, rsi=32.0,
        vol_accel=-40.0, imbalance=-0.4, liquidity=LiquidityStatus.POOR,
    )
    health = compute_health(_Position(), broken, current_price=94.0)
    assert health.score is not None and health.score < 30.0
    assert health.label_fr == "ROMPUE"


def test_health_is_not_pnl():
    """A position well in profit on a setup that has broken is unhealthy. That
    is exactly the moment the gain is about to be given back, and a health score
    that tracked PnL would say nothing at all at that moment."""
    winning_but_broken = _result(
        price=130.0, opportunity=35.0, explosion=10.0,
        structure=_structure(trend=StructureTrend.DOWNTREND, support=135.0, range_position=0.1),
        bias=Bias.BEARISH, rvol=0.4, roc3=-3.0, macd=0.1, macd_prev=2.0, rsi=35.0,
        vol_accel=-50.0, imbalance=-0.5, maturity=90.0,
    )
    health = compute_health(_Position(), winning_but_broken, current_price=130.0)

    assert health.score is not None and health.score < 50.0, (
        "a +30% position on a dead setup must not read as healthy"
    )


def test_a_missing_input_leaves_the_scale_alone():
    """Removed from both the total and the denominator. Scoring it as zero would
    make a data gap look like a collapsing position."""
    full = compute_health(_Position(), _result())
    partial = compute_health(_Position(), _result(with_explosion=False))

    assert partial.coverage == 1.0, "explosion is only part of one component"
    assert partial.score is not None
    # The component degrades, but the score is not divided by a missing input.
    assert partial.score < full.score
    assert any("non comparable" in c for c in partial.caveats())


def test_too_little_data_reports_unknown_rather_than_a_low_score():
    """The worst false alarm this engine could produce is telling someone to
    sell because a request failed."""
    health = compute_health(_Position(), _result(with_features=False))

    assert health.score is None
    assert health.label_fr == "INCONNUE"
    assert health.coverage < MIN_COVERAGE


def test_an_entry_without_a_recorded_invalidation_says_so():
    naked = _Position(invalidation_price=None)
    health = compute_health(naked, _result())
    invalidation = next(c for c in health.components if c.name == "invalidation")

    assert invalidation.available is False
    assert any("invalidation" in c for c in invalidation.caveats)


# --------------------------------------------------------------------------- #
# Exit risk
# --------------------------------------------------------------------------- #


def test_a_broken_invalidation_is_critical_and_flagged_separately():
    """It is not one signal among many: it is the term agreed before the
    position existed, and the decision rules treat it as its own fact."""
    report = assess_exit_risk(_Position(), _result(price=90.0), current_price=90.0)

    assert report.invalidation_broken is True
    assert report.worst is Severity.CRITICAL
    assert any(s.name == "invalidation_broken" for s in report.critical)


def test_an_intact_position_produces_no_critical_signal():
    report = assess_exit_risk(_Position(), _result(), current_price=108.0)
    assert report.critical == []


def test_signals_are_counted_by_severity_never_summed():
    """Five cosmetic warnings must not outweigh one broken support. A total
    would let them."""
    noisy = _result(
        price=108.0, roc3=-0.3, macd=0.9, macd_prev=1.0, rsi=38.0, rvol=0.6,
        vol_accel=-40.0, spread=45.0,
    )
    report = assess_exit_risk(_Position(), noisy, current_price=108.0, drawdown_from_peak_pct=-6.0)

    assert len(report.minor) >= 4
    assert report.worst is Severity.MINOR
    assert not hasattr(report, "total"), "an exit *score* would defeat the severity split"


def test_messages_are_ordered_most_serious_first():
    """The order someone reading in a hurry actually needs."""
    report = assess_exit_risk(
        _Position(), _result(price=90.0, rsi=35.0, rvol=0.5), current_price=90.0
    )
    assert "invalidation franchie" in report.messages()[0]


def test_unavailable_inputs_are_listed_rather_than_read_as_calm():
    """An absent signal and an absent measurement look identical unless one of
    them says so."""
    report = assess_exit_risk(_Position(), _result(with_features=False), current_price=100.0)
    assert report.unavailable


# --------------------------------------------------------------------------- #
# The four decisions about a held position
# --------------------------------------------------------------------------- #


def test_a_healthy_position_is_hold_and_hold_is_blue(engine):
    decision, _, _ = _decide(engine, _Position(), _result(), price=108.0)

    assert decision.action is TradeAction.HOLD
    assert decision.emoji == "🔵"
    assert decision.label_fr == "CONSERVER"
    assert decision.is_actionable is False, "holding is not an instruction to act"


def test_buy_is_never_offered_for_something_already_held(engine):
    """Recommending a purchase of something already owned is a different
    instruction — adding to a position — that this product does not model."""
    for kwargs in ({}, {"opportunity": 99.0, "explosion": 99.0}, {"price": 90.0}):
        decision, _, _ = _decide(engine, _Position(), _result(**kwargs), price=kwargs.get("price"))
        assert decision.action is not TradeAction.BUY


def test_a_broken_invalidation_sells_even_with_everything_else_positive(engine):
    """The one rule that needs no argument. It was agreed before the position
    existed and before any of this was emotional."""
    still_pretty = _result(price=90.0, opportunity=88.0, explosion=90.0, rvol=3.0, roc3=2.0)
    decision, _, _ = _decide(engine, _Position(), still_pretty, price=90.0)

    assert decision.action is TradeAction.SELL
    assert decision.emoji == "🔴"
    assert any("SETUP INVALIDÉ" in b for b in decision.blocking)


def test_first_signs_of_trouble_produce_watch(engine):
    slowing = _result(price=108.0, roc3=-0.2, macd=0.9, macd_prev=1.0, rvol=0.65)
    decision, health, _ = _decide(engine, _Position(), slowing, price=108.0)

    assert decision.action is TradeAction.WATCH
    assert decision.emoji == "🟡"
    assert health.score is not None


def test_several_major_signals_produce_reduce(engine):
    # Exactly two major signals — structure turned and momentum reversed — with
    # the support intact and the opportunity drop under the collapse threshold.
    # Two is the boundary the rule is written on; four would be a SELL.
    deteriorating = _result(
        price=112.0, opportunity=70.0,
        structure=_structure(trend=StructureTrend.DOWNTREND, support=95.0),
        rvol=0.9, roc3=-1.4, macd=0.5, macd_prev=1.4,
    )
    decision, _, risk = _decide(engine, _Position(), deteriorating, price=112.0, drawdown=-6.0)

    assert len(risk.major) == 2, [s.name for s in risk.major]
    assert decision.action is TradeAction.REDUCE
    assert decision.emoji == "🟠"


def test_a_collapsing_position_sells_without_needing_the_invalidation(engine):
    collapsing = _result(
        price=96.0, opportunity=30.0, explosion=15.0, maturity=90.0,
        structure=_structure(trend=StructureTrend.DOWNTREND, support=105.0, range_position=0.05),
        bias=Bias.BEARISH, rvol=0.4, roc3=-3.5, macd=0.1, macd_prev=2.0, rsi=30.0,
        vol_accel=-60.0, imbalance=-0.5, climax=True,
    )
    decision, _, _ = _decide(engine, _Position(), collapsing, price=96.0, drawdown=-14.0)

    assert decision.action is TradeAction.SELL
    assert decision.blocking


def test_unknown_health_watches_rather_than_selling(engine):
    """Selling because a request failed would be the worst false alarm this
    engine could produce."""
    decision, health, _ = _decide(engine, _Position(), _result(with_features=False), price=100.0)

    assert health.score is None
    assert decision.action is TradeAction.WATCH
    assert any("non calculable" in r for r in decision.reasons)


def test_a_liquidity_collapse_sells_immediately(engine):
    trapped = _result(price=110.0, opportunity=80.0, liquidity=LiquidityStatus.DANGEROUS)
    decision, _, _ = _decide(engine, _Position(), trapped, price=110.0)

    assert decision.action is TradeAction.SELL
    assert any("liquidité" in b for b in decision.blocking)


def test_every_position_decision_carries_the_invalidation_it_is_judged_against(engine):
    decision, _, _ = _decide(engine, _Position(), _result(), price=108.0)
    assert decision.invalidation_price == 95.0


def test_a_sell_always_says_why(engine):
    """A red card with no reason is unusable: there is no way to disagree with
    it, and no way to learn from it afterwards."""
    decision, _, _ = _decide(engine, _Position(), _result(price=90.0), price=90.0)
    assert decision.blocking or decision.risks
