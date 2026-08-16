"""TRADE_DECISION_V1 — the engine that turns three scores into one word.

The property these tests exist to protect is convergence. Every other engine in
this project produces a number that someone reads and interprets; this one
produces an instruction. The failure mode is not "the number is a bit off" — it
is "🟢 ACHETER appeared because one reading spiked", which is precisely how a
scanner becomes a way of losing money quickly.

So the tests are mostly refusals: what must NOT produce a buy.
"""

from __future__ import annotations

import pytest

from cryptopulse.config.settings import TradeSettings
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
from cryptopulse.trading.decision import (
    PRESENTATION,
    TRADE_ENGINE_VERSION,
    SignalStrength,
    TradeAction,
    TradeDecisionEngine,
)

NOW_MS = 1_700_000_000_000


def _structure(*, resistance=105.0, support=95.0, broke_out=True) -> StructureReport:
    return StructureReport(
        trend=StructureTrend.UPTREND,
        swings=[],
        resistances=[],
        supports=[],
        nearest_resistance=Level(
            price=resistance, touches=3, kind=SwingKind.HIGH, last_touch_index=10, strength=0.8
        ) if resistance else None,
        nearest_support=Level(
            price=support, touches=3, kind=SwingKind.LOW, last_touch_index=8, strength=0.7
        ) if support else None,
        distance_to_resistance_atr=0.3,
        distance_to_support_atr=1.0,
        broke_out=broke_out,
        fake_breakout=False,
        in_retest=False,
        range_position=0.6,
        atr_value=1.0,
        notes=[],
    )


def _result(
    *,
    opportunity=85.0,
    explosion=88.0,
    confidence=95.0,
    safety=95.0,
    maturity=20.0,
    liquidity=LiquidityStatus.GOOD,
    liquidity_veto=False,
    safety_veto=False,
    state=SetupState.BREAKOUT,
    explosion_vetoed=False,
    with_explosion=True,
    with_features=True,
) -> ScoreResult:
    features = None
    if with_features:
        tf = TimeframeFeatures(
            timeframe=Timeframe.M5, bars=200, close=100.0,
            open_time_ms=NOW_MS - 300_000, close_time_ms=NOW_MS,
            structure=_structure(), bias=Bias.BULLISH,
        )
        features = AssetFeatures(
            symbol="TESTUSDT", primary_timeframe=Timeframe.M5, timeframes={Timeframe.M5: tf}
        )

    result = ScoreResult(
        symbol="TESTUSDT",
        price=100.0,
        timestamp_ms=NOW_MS,
        raw_score=opportunity,
        risk_penalty=0.0,
        final_score=opportunity,
        components=[],
        penalties=PenaltyReport(total=0.0),
        maturity=PumpMaturity(score=maturity, reasons=[]),
        acceleration=AccelerationScores(momentum_acceleration=10.0, early_move=10.0, reasons=[]),
        confidence=DataConfidence(score=confidence, issues=[], max_age_seconds=30.0),
        liquidity=LiquidityAssessment(
            status=liquidity, fraction=0.8, veto=liquidity_veto, reasons=[]
        ),
        safety=SafetyAssessment(score=safety, hard_veto=safety_veto, reasons=["flagged"] if safety_veto else []),
        state=StateDecision(state=state, rationale="test", trigger="t", invalidation="i"),
        features=features,
    )
    if with_explosion:
        result.explosion = ExplosionResult(
            symbol="TESTUSDT", score=explosion, components=[],
            engine_version="EXPLOSION_ENGINE_V1", weights_fingerprint="abc",
            timestamp_ms=NOW_MS,
            vetoed=explosion_vetoed,
            veto_reason="liquidity gate: a fast move here could not be exited" if explosion_vetoed else None,
        )
    return result


@pytest.fixture()
def engine() -> TradeDecisionEngine:
    return TradeDecisionEngine()


# --------------------------------------------------------------------------- #
# The one that must work
# --------------------------------------------------------------------------- #


def test_a_setup_clearing_every_floor_is_a_buy(engine):
    decision = engine.decide_entry(_result(), discovery_score=80.0)

    assert decision.action is TradeAction.BUY
    assert decision.emoji == "🟢"
    assert decision.label_fr == "ACHETER"
    assert decision.strength is not None
    assert decision.is_actionable is True
    assert all(c["passed"] for c in decision.checks)


def test_a_buy_carries_its_entry_and_its_invalidation_as_prices(engine):
    """A recommendation with no invalidation is not a recommendation — there
    would be no way to know afterwards whether it was wrong."""
    decision = engine.decide_entry(_result(), discovery_score=80.0)
    assert decision.trigger_price == 105.0
    assert decision.invalidation_price is not None


def test_levels_that_cannot_be_measured_are_absent_rather_than_guessed(engine):
    """A made-up entry price is the most dangerous number this product could
    display, so it is never produced."""
    decision = engine.decide_entry(_result(with_features=False), discovery_score=80.0)
    assert decision.trigger_price is None
    assert decision.invalidation_price is None


# --------------------------------------------------------------------------- #
# Convergence — the reason this engine exists
# --------------------------------------------------------------------------- #


def test_one_spectacular_score_never_produces_a_buy(engine):
    """The whole point. A scanner that buys on a single spiking reading is a
    fast way to lose money, and it looks convincing while doing it."""
    decision = engine.decide_entry(
        _result(opportunity=100.0, explosion=5.0, confidence=20.0, safety=30.0),
        discovery_score=99.0,
    )
    assert decision.action is not TradeAction.BUY


@pytest.mark.parametrize(
    ("kwargs", "expected_floor"),
    [
        ({"opportunity": 60.0}, "opportunité"),
        ({"explosion": 40.0}, "explosion 15m"),
        ({"confidence": 50.0}, "confiance des données"),
        ({"safety": 40.0}, "sécurité"),
        ({"maturity": 85.0}, "maturité du mouvement"),
        ({"state": SetupState.ARMED}, "setup déclenché"),
        ({"liquidity": LiquidityStatus.POOR}, "liquidité"),
    ],
)
def test_every_single_floor_can_block_a_buy_on_its_own(engine, kwargs, expected_floor):
    decision = engine.decide_entry(_result(**kwargs), discovery_score=80.0)

    assert decision.action is not TradeAction.BUY
    failed = [c["name"] for c in decision.checks if not c["passed"]]
    assert failed == [expected_floor], f"expected only {expected_floor} to fail, got {failed}"
    assert decision.blocking, "a refusal always says why"


def test_an_armed_setup_is_a_watch_not_a_buy(engine):
    """Coiled under a level is not the same as through it. The thing that would
    make this a buy has not happened yet."""
    decision = engine.decide_entry(_result(state=SetupState.ARMED), discovery_score=80.0)
    assert decision.action is TradeAction.WATCH
    assert decision.emoji == "🟡"


def test_a_discovery_score_below_its_floor_blocks_the_buy(engine):
    decision = engine.decide_entry(_result(), discovery_score=30.0)
    assert decision.action is not TradeAction.BUY


# --------------------------------------------------------------------------- #
# Hard vetoes — outright, never a deduction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs",
    [
        {"liquidity": LiquidityStatus.DANGEROUS},
        {"liquidity_veto": True},
        {"safety_veto": True},
        {"state": SetupState.INVALIDATED},
        {"explosion_vetoed": True},
    ],
)
def test_a_hard_veto_forces_do_not_buy_whatever_the_scores_say(engine, kwargs):
    """Not a reduced score. A reduced score would still let a dangerous token
    outrank a safe one in a sorted list, which is how a veto stops meaning
    anything."""
    decision = engine.decide_entry(
        _result(opportunity=99.0, explosion=99.0, confidence=100.0, safety=100.0,
                maturity=1.0, **kwargs),
        discovery_score=99.0,
    )
    assert decision.action is TradeAction.AVOID
    assert decision.emoji == "⚫"
    assert decision.blocking, "a veto is never silent"


def test_a_veto_always_says_which_one(engine):
    decision = engine.decide_entry(_result(liquidity=LiquidityStatus.DANGEROUS))
    assert any("liquidité dangereuse" in b for b in decision.blocking)


# --------------------------------------------------------------------------- #
# Missing inputs
# --------------------------------------------------------------------------- #


def test_a_missing_explosion_score_blocks_rather_than_scoring_zero(engine):
    """An input that was never computed is not a low reading, and must not be
    treated as one."""
    decision = engine.decide_entry(_result(with_explosion=False), discovery_score=80.0)

    assert decision.action is TradeAction.AVOID
    assert any("indisponible" in b for b in decision.blocking)


def test_a_missing_discovery_score_allows_a_buy_but_caps_its_strength(engine):
    """Discovery only exists after a deep scan. Requiring it would mean an
    ordinary cycle could never say ACHETER; ignoring it would hide that the
    convergence rests on one criterion fewer. So: allowed, capped, and said."""
    comfortable = dict(opportunity=95.0, explosion=95.0, confidence=99.0, safety=95.0, maturity=15.0)
    with_discovery = engine.decide_entry(_result(**comfortable), discovery_score=95.0)
    without = engine.decide_entry(_result(**comfortable), discovery_score=None)

    assert without.action is TradeAction.BUY
    assert without.strength is not SignalStrength.VERY_STRONG
    assert with_discovery.strength is SignalStrength.VERY_STRONG
    assert any("recherche" in r for r in without.risks)


# --------------------------------------------------------------------------- #
# Strength
# --------------------------------------------------------------------------- #


def test_strength_measures_the_worst_floor_not_the_best_score(engine):
    """Otherwise one spectacular number would buy a VERY_STRONG grade while
    another criterion sat one point above its minimum."""
    barely = engine.decide_entry(
        _result(opportunity=100.0, explosion=100.0, confidence=100.0,
                safety=95.0, maturity=39.5),
        discovery_score=99.0,
    )
    comfortably = engine.decide_entry(
        _result(opportunity=95.0, explosion=95.0, confidence=99.0, safety=95.0, maturity=15.0),
        discovery_score=95.0,
    )
    assert barely.strength is SignalStrength.STANDARD
    assert comfortably.strength is SignalStrength.VERY_STRONG


def test_only_a_buy_carries_a_strength(engine):
    """A strength on an AVOID would read as 'strongly do not buy', which is a
    different claim from the one being made."""
    for decision in (
        engine.decide_entry(_result(state=SetupState.ARMED)),
        engine.decide_entry(_result(safety_veto=True)),
    ):
        assert decision.strength is None


# --------------------------------------------------------------------------- #
# The colour code
# --------------------------------------------------------------------------- #


def test_every_action_has_exactly_one_presentation():
    """One source for the six colours, so two screens cannot disagree."""
    assert set(PRESENTATION) == set(TradeAction)
    emojis = [p[0] for p in PRESENTATION.values()]
    tones = [p[2] for p in PRESENTATION.values()]
    assert len(set(emojis)) == 6, "two decisions sharing an icon are indistinguishable"
    assert len(set(tones)) == 6


def test_green_is_reserved_for_opening_a_position():
    """The most expensive confusion this screen could create is a glance that
    does not separate 'there is something to do' from 'there is nothing to do'."""
    assert PRESENTATION[TradeAction.BUY][0] == "🟢"
    assert PRESENTATION[TradeAction.HOLD][0] == "🔵"
    assert PRESENTATION[TradeAction.HOLD][0] != PRESENTATION[TradeAction.BUY][0]


def test_a_decision_never_travels_as_a_colour_alone():
    """Accessibility, and also legibility in sunlight on a phone."""
    for action, (emoji, fr, tone, en) in PRESENTATION.items():
        assert emoji and fr and tone and en, action


# --------------------------------------------------------------------------- #
# Identity and honesty
# --------------------------------------------------------------------------- #


def test_changing_a_threshold_changes_the_fingerprint():
    """A decision journalled under one set of floors must never be reread as
    though it had been taken under another."""
    a = TradeDecisionEngine(TradeSettings())
    b = TradeDecisionEngine(TradeSettings(buy_min_opportunity=60.0))
    assert a.weights_fingerprint != b.weights_fingerprint


def test_an_unreadable_liquidity_setting_is_refused_at_construction():
    """Silently falling back to a default would apply a floor the user did not
    choose, to every decision, invisibly."""
    with pytest.raises(ValueError, match="not a liquidity status"):
        TradeDecisionEngine(TradeSettings(buy_min_liquidity="VERY_LIQUID"))


def test_the_payload_says_it_is_not_advice_and_places_no_orders(engine):
    d = engine.decide_entry(_result(), discovery_score=80.0).to_dict()
    assert d["engine_version"] == TRADE_ENGINE_VERSION
    assert "conseil" in d["disclaimer"]
    assert "Aucun ordre" in d["disclaimer"]
    # A ranking-derived instruction, never rendered as a likelihood.
    assert "%" not in d["label_fr"]


def test_the_repository_contains_no_order_placing_code():
    """The rule the whole product rests on. Checked here rather than trusted,
    because this is the package where such a thing would most plausibly be
    added by someone trying to be helpful."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "cryptopulse"
    forbidden = ("create_order", "place_order", "new_order", "/api/v3/order", "submit_order")
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, offenders
