"""ROBINHOOD_TRADE_DECISION_V1 — the one engine here whose output gets acted on.

Every other engine in this project produces a number to interpret. This one
produces an instruction, which changes what a wrong answer costs: a mistaken
score wastes attention, a mistaken 🟢 wastes money on a token that may not be
sellable tomorrow.

So the tests are mostly about what must *not* happen:

  * no buy while security says stop, whatever the scores are (spec §18-19);
  * no buy on an absence of data, which is not a low reading (spec §51, §53);
  * no buy from a single spectacular number — convergence, not a maximum;
  * no order-placing surface anywhere near it (spec §39-40).

The decisions and colours are asserted to be the *same objects* as the Binance
engine's, because two screens that disagree about what 🟢 means is the failure
this shared vocabulary exists to prevent.
"""

from __future__ import annotations

import pytest

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.hunter.robinhood_detail import (
    Agreement,
    CrossCheck,
    FieldComparison,
)
from cryptopulse.hunter.robinhood_discovery import TokenCandidate
from cryptopulse.providers.geckoterminal import PoolSnapshot, TokenRef
from cryptopulse.risk.robinhood_safety import (
    RugRisk,
    SafetyFinding,
    SafetyReport,
    Severity,
)
from cryptopulse.scoring.robinhood_early import (
    DataConfidence,
    EarlyScore,
    PumpMaturity,
)
from cryptopulse.scoring.robinhood_explosion import RobinhoodExplosion
from cryptopulse.trading.decision import PRESENTATION, SignalStrength, TradeAction
from cryptopulse.trading.robinhood_decision import (
    RH_TRADE_ENGINE_VERSION,
    RobinhoodTradeDecisionEngine,
)

SETTINGS = RobinhoodSettings()
ADDR = "0x" + "ab" * 20
NOW = 1_700_000_000_000


# --------------------------------------------------------------------------- #
# Builders — a token that clears everything, then one spoiled input at a time
# --------------------------------------------------------------------------- #


def safety(score=95.0, rug=RugRisk.LOW, findings=None) -> SafetyReport:
    return SafetyReport(
        address=ADDR, score=score, rug_risk=rug, analysed=True,
        coverage=0.9, findings=findings or [],
    )


def honeypot() -> SafetyReport:
    return SafetyReport(
        address=ADDR, score=0.0, rug_risk=RugRisk.CRITICAL, analysed=True, coverage=0.9,
        findings=[
            SafetyFinding("HONEYPOT", Severity.CRITICAL, "HONEYPOT — revente impossible",
                          blocking=True),
        ],
    )


def token(
    *,
    early=88.0,
    explosion=90.0,
    confidence=85.0,
    safety_report=None,
    maturity=15.0,
    maturity_known=True,
    liquidity=60_000.0,
) -> TokenCandidate:
    """A candidate that clears every floor, unless an argument says otherwise."""
    c = TokenCandidate(address=ADDR, symbol="XYZ", name="Xyz")
    pool = PoolSnapshot(
        pool_address="0xpool", name="XYZ / WETH", dex="uniswap-v4",
        base=TokenRef(address=ADDR, symbol="XYZ"),
        quote=TokenRef(address="0x" + "ee" * 20, symbol="WETH"),
        created_at_ms=None, base_price_usd=0.00042, liquidity_usd=liquidity,
    )
    c.pools = [pool]
    c.price_usd = 0.00042
    c.liquidity_usd = liquidity
    c.pool_age_seconds = 1800.0
    c.safety = safety() if safety_report is None else safety_report
    c.early = None if early is None else EarlyScore(score=early)
    c.explosion = (
        None if explosion is None
        else RobinhoodExplosion(score=explosion, reasons=["poussée d'achats sur 5 minutes"])
    )
    c.confidence = (
        None if confidence is None
        else DataConfidence(score=confidence, present=9, total=10)
    )
    c.maturity = (
        None if maturity is None
        else PumpMaturity(score=maturity, known=maturity_known)
    )
    return c


def agreeing() -> CrossCheck:
    return CrossCheck(
        comparisons=[
            FieldComparison("price_usd", "Prix", 0.00042, 0.000421, Agreement.AGREE, 0.2),
            FieldComparison("liquidity_usd", "Liquidité", 60_000.0, 60_500.0, Agreement.AGREE, 0.8),
        ],
        sources_seen=["geckoterminal", "dexscreener"],
    )


def disagreeing() -> CrossCheck:
    return CrossCheck(
        comparisons=[
            FieldComparison("price_usd", "Prix", 0.00042, 0.00061, Agreement.DISAGREE, 31.1),
        ],
        sources_seen=["geckoterminal", "dexscreener"],
    )


@pytest.fixture
def engine() -> RobinhoodTradeDecisionEngine:
    return RobinhoodTradeDecisionEngine(SETTINGS)


# --------------------------------------------------------------------------- #
# Identity and versioning
# --------------------------------------------------------------------------- #


def test_the_engine_is_versioned_and_fingerprinted(engine):
    d = engine.decide_entry(token(), now_ms=NOW)
    assert d.engine_version == RH_TRADE_ENGINE_VERSION
    assert len(d.weights_fingerprint) == 12


def test_moving_a_floor_moves_the_fingerprint():
    """A decision taken last week must never be reread under this week's rules
    (invariant 13). The fingerprint is what makes that impossible."""
    a = RobinhoodTradeDecisionEngine(RobinhoodSettings())
    b = RobinhoodTradeDecisionEngine(RobinhoodSettings(buy_min_safety=90.0))
    assert a.weights_fingerprint != b.weights_fingerprint


def test_the_fingerprint_is_not_the_binance_one():
    """Two markets, two sets of floors. Sharing a fingerprint would mean
    changing a Binance threshold silently reinterprets a Robinhood decision."""
    from cryptopulse.trading.decision import TradeDecisionEngine

    assert (
        RobinhoodTradeDecisionEngine(SETTINGS).weights_fingerprint
        != TradeDecisionEngine().weights_fingerprint
    )


def test_a_bad_rug_risk_setting_fails_loudly_at_construction():
    with pytest.raises(ValueError, match="risque de rug"):
        RobinhoodTradeDecisionEngine(RobinhoodSettings(buy_max_rug_risk="TRÈS ÉLEVÉ"))


# --------------------------------------------------------------------------- #
# The shared vocabulary (spec §21)
# --------------------------------------------------------------------------- #


def test_the_six_decisions_and_colours_are_the_binance_ones(engine):
    """Not merely equal — the same table. A user who learns 🟢 on one screen
    must not have to relearn it on the other."""
    d = engine.decide_entry(token(), now_ms=NOW)
    assert d.emoji == PRESENTATION[TradeAction.BUY][0] == "🟢"
    assert d.label_fr == "ACHETER"
    assert d.tone == "buy"


def test_buy_and_hold_never_share_a_colour():
    """Sharing one would mean a glance no longer separates "there is something
    to do" from "there is nothing to do"."""
    buy, hold = PRESENTATION[TradeAction.BUY], PRESENTATION[TradeAction.HOLD]
    assert buy[0] != hold[0] and buy[2] != hold[2]


def test_every_rendering_carries_icon_text_and_colour_together(engine):
    """Colour alone fails for anyone who cannot distinguish these hues, and for
    everyone on a phone in sunlight."""
    payload = engine.decide_entry(token(), now_ms=NOW).to_dict()
    assert payload["emoji"] and payload["label_fr"] and payload["tone"]


# --------------------------------------------------------------------------- #
# Security outranks every score (spec §18-19)
# --------------------------------------------------------------------------- #


def test_a_honeypot_is_never_a_buy_however_perfect_the_scores(engine):
    """Spec §18, word for word: même si Early Score = 100, Explosion = 100,
    Momentum = 100. LA SÉCURITÉ PRIME TOUJOURS."""
    d = engine.decide_entry(
        token(early=100.0, explosion=100.0, confidence=100.0, safety_report=honeypot()),
        now_ms=NOW,
    )
    assert d.action is TradeAction.AVOID
    assert d.emoji == "⚫"
    assert any("HONEYPOT" in b for b in d.blocking)


@pytest.mark.parametrize("level", [RugRisk.HIGH, RugRisk.CRITICAL, RugRisk.UNKNOWN])
def test_rug_risk_high_and_above_forbids_a_purchase(engine, level):
    d = engine.decide_entry(
        token(early=100.0, explosion=100.0, safety_report=safety(score=100.0, rug=level)),
        now_ms=NOW,
    )
    assert d.action is TradeAction.AVOID


def test_a_veto_always_carries_its_own_reason(engine):
    """Found by running the UI on the safety engine: a token vetoed for the
    level alone had an empty `blocking` list, and the screen fell back to
    "sécurité non analysée" on a token that had been fully analysed
    (invariant 73). The decision layer must not reintroduce that hole."""
    d = engine.decide_entry(
        token(safety_report=safety(score=100.0, rug=RugRisk.HIGH)), now_ms=NOW
    )
    assert d.action is TradeAction.AVOID
    assert d.blocking, "un veto sans raison est indiscernable d'un bug"


def test_an_unanalysed_token_is_never_a_buy(engine):
    """Spec §53. "We did not look" and "it is clean" are different sentences."""
    # `token(safety_report=None)` means "use the clean default", so spoil it
    # explicitly rather than through the builder.
    c = token()
    c.safety = None
    d = engine.decide_entry(c, now_ms=NOW)
    assert d.action is TradeAction.AVOID
    assert any("non analysée" in b for b in d.blocking)


def test_a_veto_is_not_a_deduction(engine):
    """A merely reduced answer would still let a honeypot outrank a sound token
    in a sorted list, which is exactly how a veto stops meaning anything."""
    vetoed = engine.decide_entry(token(safety_report=honeypot()), now_ms=NOW)
    clean = engine.decide_entry(token(), now_ms=NOW)
    assert vetoed.action is TradeAction.AVOID and clean.action is TradeAction.BUY
    assert vetoed.strength is None


def test_a_pool_too_thin_to_exit_is_a_veto_not_a_failed_floor(engine):
    d = engine.decide_entry(token(liquidity=800.0), now_ms=NOW)
    assert d.action is TradeAction.AVOID
    assert any("sortie" in b for b in d.blocking)


def test_a_vetoed_explosion_blocks_the_purchase(engine):
    c = token()
    c.explosion = RobinhoodExplosion(
        score=0.0, vetoed=True, veto_reason="liquidité insuffisante pour un mouvement de 15 min"
    )
    d = engine.decide_entry(c, now_ms=NOW)
    assert d.action is TradeAction.AVOID
    assert any("15 min" in b for b in d.blocking)


# --------------------------------------------------------------------------- #
# Convergence, not a maximum
# --------------------------------------------------------------------------- #


def test_one_spectacular_score_never_produces_a_buy(engine):
    """The whole point of the engine: 🟢 asserts something about the
    *combination*, not about whichever number happened to spike."""
    d = engine.decide_entry(
        token(early=100.0, explosion=10.0, confidence=20.0, maturity=95.0),
        now_ms=NOW,
    )
    assert d.action is not TradeAction.BUY


@pytest.mark.parametrize(
    "spoil",
    [
        {"early": 40.0},
        {"explosion": 30.0},
        {"confidence": 20.0},
        {"maturity": 90.0},
        {"liquidity": 5_000.0},
    ],
)
def test_any_single_failed_floor_removes_the_buy(engine, spoil):
    assert engine.decide_entry(token(), now_ms=NOW).action is TradeAction.BUY
    assert engine.decide_entry(token(**spoil), now_ms=NOW).action is not TradeAction.BUY


def test_medium_rug_risk_is_tolerable_to_look_at_but_not_to_buy(engine):
    d = engine.decide_entry(
        token(safety_report=safety(score=90.0, rug=RugRisk.MEDIUM)), now_ms=NOW
    )
    assert d.action is not TradeAction.BUY
    assert any("risque de rug" in c["name"] for c in d.checks)


def test_every_check_is_recorded_whether_or_not_it_passed(engine):
    """The answer has to be arguable rather than merely stated."""
    d = engine.decide_entry(token(early=40.0), now_ms=NOW)
    names = {c["name"] for c in d.checks}
    assert {"score de précocité", "explosion 15 min", "confiance des données",
            "sécurité", "maturité du mouvement", "liquidité", "risque de rug"} <= names
    assert any(c["passed"] for c in d.checks) and any(not c["passed"] for c in d.checks)


# --------------------------------------------------------------------------- #
# A missing input is not a low reading (spec §51)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", ["early", "explosion", "confidence"])
def test_an_absent_score_blocks_rather_than_scoring_zero(engine, missing):
    d = engine.decide_entry(token(**{missing: None}), now_ms=NOW)
    assert d.action is TradeAction.AVOID
    assert any(c["unavailable"] for c in d.checks)


def test_an_unknown_maturity_is_not_read_as_early(engine):
    """Maturity is 50 with `known=False` when there was nothing to measure
    (invariant 80). Fifty is under the ceiling of 40? No — but 20 would be, and
    a token with no history would then buy on an absence of data."""
    d = engine.decide_entry(token(maturity=20.0, maturity_known=False), now_ms=NOW)
    assert d.action is TradeAction.AVOID
    maturity = next(c for c in d.checks if c["name"] == "maturité du mouvement")
    assert maturity["unavailable"] and not maturity["passed"]


def test_unknown_liquidity_blocks_and_says_which_question_it_leaves_open(engine):
    c = token()
    c.liquidity_usd = None
    d = engine.decide_entry(c, now_ms=NOW)
    assert d.action is TradeAction.AVOID
    assert any("fermée" in r for r in d.risks)


def test_a_missing_input_never_becomes_a_watch(engine):
    """SURVEILLER says "this is worth a second look". An unknown safety score
    is not a low one, and offering to watch it implies we measured something."""
    c = token(early=100.0, confidence=100.0)
    c.explosion = None
    d = engine.decide_entry(c, now_ms=NOW)
    assert d.action is TradeAction.AVOID


# --------------------------------------------------------------------------- #
# Two sources that disagree (invariant 75)
# --------------------------------------------------------------------------- #


def test_a_disagreement_between_sources_blocks_the_purchase(engine):
    d = engine.decide_entry(token(), now_ms=NOW, cross_check=disagreeing())
    assert d.action is TradeAction.AVOID
    assert any("désaccord" in b or "pas d'accord" in b for b in d.blocking)


def test_without_a_cross_check_a_buy_is_possible_but_capped(engine):
    """Same shape as the discovery score on the Binance side: the missing
    criterion costs the top grade rather than the decision."""
    d = engine.decide_entry(token(early=100.0, explosion=100.0, confidence=100.0), now_ms=NOW)
    assert d.action is TradeAction.BUY
    assert d.strength is not SignalStrength.VERY_STRONG
    assert any("recoupé" in r for r in d.risks)


def test_two_agreeing_sources_unlock_the_top_grade(engine):
    d = engine.decide_entry(
        token(early=100.0, explosion=100.0, confidence=100.0),
        now_ms=NOW, cross_check=agreeing(),
    )
    assert d.action is TradeAction.BUY
    assert d.strength is SignalStrength.VERY_STRONG


def test_strength_measures_the_worst_margin_not_the_best_score(engine):
    """A token scraping past one floor cannot be TRÈS FORTE because another
    score is spectacular."""
    d = engine.decide_entry(
        token(early=100.0, explosion=100.0, confidence=SETTINGS.buy_min_confidence),
        now_ms=NOW, cross_check=agreeing(),
    )
    assert d.strength is SignalStrength.STANDARD


def test_liquidity_margin_never_inflates_the_strength(engine):
    """Dollars and score points are not the same unit. Feeding a 60 000 dollar
    margin into a calculation whose other margins are single-digit points would
    make every deep pool look TRÈS FORTE."""
    d = engine.decide_entry(
        token(early=SETTINGS.buy_min_early, explosion=SETTINGS.buy_min_explosion,
              confidence=SETTINGS.buy_min_confidence, liquidity=5_000_000.0),
        now_ms=NOW, cross_check=agreeing(),
    )
    assert d.strength is SignalStrength.STANDARD


# --------------------------------------------------------------------------- #
# WATCH, and the shape of the payload
# --------------------------------------------------------------------------- #


def test_a_near_miss_with_real_data_is_worth_watching(engine):
    d = engine.decide_entry(token(explosion=40.0), now_ms=NOW)
    assert d.action is TradeAction.WATCH
    assert d.emoji == "🟡"
    assert d.risks, "surveiller sans dire ce qui manque n'apprend rien"


def test_a_token_with_nothing_going_on_is_not_offered_for_watching(engine):
    d = engine.decide_entry(token(early=12.0, explosion=5.0, confidence=15.0), now_ms=NOW)
    assert d.action is TradeAction.AVOID


def test_a_buy_explains_itself(engine):
    d = engine.decide_entry(token(), now_ms=NOW)
    assert d.action is TradeAction.BUY
    assert d.reasons, "un achat sans raison affichée n'est pas explicable"


def test_the_payload_is_serialisable_and_carries_its_disclaimer(engine):
    import json

    payload = engine.decide_entry(token(), now_ms=NOW).to_dict()
    json.dumps(payload)
    assert "probabilité" in payload["disclaimer"]
    assert "aucun ordre" in payload["disclaimer"].lower()


def test_the_strength_is_none_on_everything_that_is_not_a_buy(engine):
    for candidate in (token(explosion=40.0), token(safety_report=honeypot())):
        assert engine.decide_entry(candidate, now_ms=NOW).strength is None


# --------------------------------------------------------------------------- #
# Paper mode (spec §39-40)
# --------------------------------------------------------------------------- #


def test_the_module_contains_no_order_placing_surface():
    """Analyse -> recommend -> alert -> the user acts manually. A test walks the
    source because the absence is the feature."""
    from pathlib import Path

    source = Path(
        "cryptopulse/trading/robinhood_decision.py"
    ).read_text(encoding="utf-8").lower()
    for forbidden in ("place_order", "create_order", "submit_order", "private_key",
                      "seed phrase", "sign_transaction", "eth_sendrawtransaction"):
        assert forbidden not in source, forbidden


def test_the_decision_never_claims_to_be_a_probability(engine):
    d = engine.decide_entry(token(), now_ms=NOW)
    rendered = " ".join([d.label_fr, *d.reasons, *d.risks]).lower()
    assert "%" not in rendered.replace(" %", "")


def test_the_safety_veto_is_stated_once_not_relayed_twice(engine):
    """Found by running the search against a honeypot: the card read

        ✖ veto de sécurité : HONEYPOT — revente impossible
        ✖ veto sécurité : HONEYPOT — revente impossible

    because the explosion engine relays the safety veto as its own gate. The
    same fact in two slightly different wordings reads as a bug rather than as
    emphasis. The relay is dropped; a veto genuinely belonging to the explosion
    engine is still shown.
    """
    c = token(safety_report=honeypot())
    c.explosion = RobinhoodExplosion(
        score=0.0, vetoed=True, veto_reason="veto sécurité : HONEYPOT — revente impossible"
    )
    blocking = engine.decide_entry(c, now_ms=NOW).blocking
    assert len(blocking) == 1, blocking
    assert "HONEYPOT" in blocking[0]


def test_an_explosion_veto_of_its_own_is_still_reported(engine):
    """A pool too thin for a fifteen-minute move is the explosion engine's own
    finding, not a relay, and dropping it would hide a real reason."""
    c = token()
    c.explosion = RobinhoodExplosion(
        score=0.0, vetoed=True,
        veto_reason="liquidité de 900 $ — un mouvement de 10 % ici n'est pas une opportunité",
    )
    blocking = engine.decide_entry(c, now_ms=NOW).blocking
    assert any("liquidité" in b for b in blocking)


def test_no_blocking_reason_is_ever_repeated(engine):
    """Two engines describing the same fact must not both reach the screen."""
    for candidate in (token(safety_report=honeypot()), token(liquidity=500.0)):
        blocking = engine.decide_entry(candidate, now_ms=NOW).blocking
        assert len(blocking) == len(set(blocking)), blocking
