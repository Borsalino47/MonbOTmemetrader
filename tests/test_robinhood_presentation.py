"""Reading a Robinhood card in five seconds without being misled by it.

THE TOKEN THAT PROMPTED THIS FILE

DRCO, seen on a real Android against a live Robinhood Chain:

    🟡 SURVEILLER
    Précocité        66/100      (minimum 75)
    Explosion 15 min 92/100      ← the biggest number on the card
    Maturité         35/100
    Confiance        50/100      (minimum 70)
    Sécurité         75/100      (minimum 85)
    Liquidité        2 471 $     (minimum 25 000 $)
    ~2 minutes of history

The engine was right and the screen was wrong: 92 was what the eye landed on,
and a raw score visually outranking four guard rails is how a guard rail stops
working.

So these tests assert what the *card* must say, and — just as important — that
none of it changed the decision. A presentation layer that moved a threshold
would be the worst possible outcome of a readability pass.
"""

from __future__ import annotations

import pytest

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.hunter.robinhood_discovery import TokenCandidate
from cryptopulse.providers.geckoterminal import PoolSnapshot, TokenRef
from cryptopulse.risk.robinhood_safety import (
    RugRisk,
    SafetyFinding,
    SafetyReport,
    Severity,
)
from cryptopulse.scoring.robinhood_early import DataConfidence, EarlyScore, PumpMaturity
from cryptopulse.scoring.robinhood_explosion import RobinhoodExplosion
from cryptopulse.scoring.robinhood_presentation import (
    RELIABILITY_BANDS,
    build_presentation,
    describe_history,
    describe_liquidity,
    describe_reliability,
    describe_safety,
    describe_score,
    why_not_buy,
)
from cryptopulse.trading.decision import TradeAction
from cryptopulse.trading.robinhood_decision import RobinhoodTradeDecisionEngine

SETTINGS = RobinhoodSettings()
ADDR = "0x" + "dc" * 20
NOW = 1_700_000_000_000


def drco() -> TokenCandidate:
    """The real token, with the numbers exactly as they came off the phone."""
    c = TokenCandidate(address=ADDR, symbol="DRCO", name="Drco")
    pool = PoolSnapshot(
        pool_address="0xpool", name="DRCO / WETH", dex="uniswap-v4",
        base=TokenRef(address=ADDR, symbol="DRCO"),
        quote=TokenRef(address="0x" + "ee" * 20, symbol="WETH"),
        created_at_ms=None, base_price_usd=0.00031, liquidity_usd=2_471.0,
    )
    c.pools = [pool]
    c.price_usd = 0.00031
    c.liquidity_usd = 2_471.0
    c.pool_age_seconds = 112.0                       # ~2 minutes
    c.early = EarlyScore(score=66.0)
    c.explosion = RobinhoodExplosion(score=92.0, reasons=["volume 12,0× le rythme horaire"])
    c.maturity = PumpMaturity(score=35.0, known=True)
    c.confidence = DataConfidence(
        score=50.0, present=4, total=8,
        missing=[
            "ratio achats/ventes 1 h — fenêtre non écoulée (2 min d'historique)",
            "volume 1 h — fenêtre non écoulée (2 min d'historique)",
            "acheteurs uniques 1 h — fenêtre non écoulée (2 min d'historique)",
            "accélération du volume (5 min) — fenêtre non écoulée (2 min d'historique)",
        ],
    )
    c.safety = SafetyReport(
        address=ADDR, score=75.0, rug_risk=RugRisk.LOW, analysed=True, coverage=0.68,
        findings=[
            SafetyFinding("BUY_TAX_UNKNOWN", Severity.UNKNOWN, "Taxe à l'achat inconnue"),
            SafetyFinding("SELL_TAX_UNKNOWN", Severity.UNKNOWN, "Taxe à la vente inconnue"),
            SafetyFinding("LP_LOCK_UNKNOWN", Severity.UNKNOWN, "Verrouillage de liquidité inconnu"),
            SafetyFinding("HOLDERS_UNKNOWN", Severity.UNKNOWN, "Répartition des détenteurs inconnue"),
        ],
    )
    c.decision = RobinhoodTradeDecisionEngine(SETTINGS).decide_entry(c, now_ms=NOW)
    c.presentation = build_presentation(c, SETTINGS)
    return c


# --------------------------------------------------------------------------- #
# The scenario itself (spec §23)
# --------------------------------------------------------------------------- #


def test_drco_stays_a_watch_and_never_becomes_a_buy():
    """The engine's answer must not move because the card got clearer."""
    assert drco().decision.action is TradeAction.WATCH


def test_the_presentation_layer_changes_no_decision():
    """A readability pass that moved a threshold would be the worst possible
    outcome of a readability pass (spec §18, §22)."""
    c = drco()
    engine = RobinhoodTradeDecisionEngine(SETTINGS)
    before = engine.decide_entry(c, now_ms=NOW)
    c.presentation = build_presentation(c, SETTINGS)
    after = engine.decide_entry(c, now_ms=NOW)
    assert before.action is after.action
    assert before.weights_fingerprint == after.weights_fingerprint
    assert [ch["passed"] for ch in before.checks] == [ch["passed"] for ch in after.checks]


def test_drco_names_all_four_reasons_it_is_not_a_buy():
    """Spec §7 and §23: liquidity, safety, data confidence and earliness."""
    reasons = drco().presentation["why_not_buy"]
    joined = " ".join(r["detail_fr"] for r in reasons).lower()
    assert "liquidité" in joined
    assert "sécurité" in joined
    assert "confiance" in joined
    assert "précocité" in joined


def test_drco_shows_the_liquidity_gap_in_dollars_not_in_points():
    """"2471/100" would be absurd. Depth is dollars and the minimum is dollars."""
    liquidity = next(
        r for r in drco().presentation["why_not_buy"] if "iquidit" in r["label_fr"]
    )
    assert "$" in liquidity["value_fr"] and "$" in liquidity["minimum_fr"]
    assert "/100" not in liquidity["value_fr"]


def test_drco_explosion_is_never_rendered_as_a_strong_signal():
    """92 on two minutes of history is a strong *number*, not a strong signal.
    Green is reserved for a decision (spec §11-12)."""
    explosion = drco().presentation["scores"]["explosion"]
    assert explosion["display"] == "92/100"
    assert explosion["tone"] == "caution"
    assert explosion["tone"] != "strong"
    assert explosion["warning"] and "fiabilité faible" in explosion["warning"]


def test_drco_warns_that_the_token_is_extremely_recent():
    history = drco().presentation["history"]
    assert history["very_recent"]
    assert "EXTRÊMEMENT RÉCENT" in history["warning"]
    # U+202F, the narrow no-break space French typography requires.
    assert history["available_fr"] == "2 min"


def test_drco_says_a_thin_pool_is_not_an_opportunity():
    """Spec §20 — the distinction the whole card turns on."""
    caveats = " ".join(drco().presentation["caveats"])
    assert "facile à provoquer" in caveats
    assert "pas la même chose qu'une opportunité" in caveats


def test_drco_never_presents_safety_as_verified():
    """Spec §9-10. Four checks came back empty; "RUG RISK FAIBLE" alone would
    read as reassurance it did not earn."""
    safety = drco().presentation["safety"]
    assert safety["partial"]
    assert safety["coverage_pct"] == 68
    assert safety["unknown_count"] == 4
    assert "détecté" in safety["headline_fr"]
    assert "contrôles effectués" in safety["headline_fr"]


def test_drco_lists_which_security_checks_were_never_run():
    unknown = drco().presentation["safety"]["unknown_checks"]
    joined = " ".join(unknown).lower()
    for expected in ("taxe à l'achat", "taxe à la vente", "verrouillage", "détenteurs"):
        assert expected in joined


def test_drco_counts_its_missing_data():
    missing = drco().presentation["missing_data"]
    assert missing["count"] == 4
    assert len(missing["fields"]) == 4
    assert "jamais comptée comme une donnée favorable" in missing["note"]


# --------------------------------------------------------------------------- #
# A score is never a probability (spec §1)
# --------------------------------------------------------------------------- #


def test_no_score_is_ever_shown_as_a_percentage():
    for name, block in drco().presentation["scores"].items():
        assert block["not_a_probability"], name
        assert "/100" in block["display"], name
        assert "%" not in block["display"], name
        assert "pas une probabilité" in block["note"], name


def test_the_whole_payload_never_says_chance_or_probability_of_success():
    import json

    text = json.dumps(drco().presentation, ensure_ascii=False).lower()
    for forbidden in ("% de chance", "chance d'explosion", "probabilité de hausse",
                      "probabilité de succès"):
        assert forbidden not in text, forbidden


# --------------------------------------------------------------------------- #
# Reliability bands (spec §2-3)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "score,label",
    [(0, "TRÈS FAIBLE"), (39, "TRÈS FAIBLE"), (40, "FAIBLE"), (59, "FAIBLE"),
     (60, "MOYENNE"), (74, "MOYENNE"), (75, "BONNE"), (89, "BONNE"),
     (90, "TRÈS BONNE"), (100, "TRÈS BONNE")],
)
def test_the_reliability_bands_match_the_specification(score, label):
    assert describe_reliability(float(score))["label_fr"] == label


def test_the_bands_are_about_data_never_about_the_token():
    note = describe_reliability(80.0)["note"]
    assert "données" in note and "potentiel" in note


def test_an_uncomputable_reliability_is_unknown_never_zero():
    """A missing reading is not a bad one."""
    r = describe_reliability(None)
    assert r["score"] is None
    assert r["label_fr"] == "INCONNUE"
    assert not r["trustworthy"]


def test_the_bands_cover_every_score_without_a_gap():
    for score in range(0, 101):
        assert describe_reliability(float(score))["label_fr"]
    assert RELIABILITY_BANDS[-1][0] == 0.0


def test_a_high_score_on_good_data_may_read_as_strong():
    """The tone is not permanently pessimistic — it follows the evidence."""
    assert describe_score("Explosion", 92.0, 95.0)["tone"] == "strong"
    assert describe_score("Explosion", 92.0, 50.0)["tone"] == "caution"


def test_a_score_with_no_value_has_no_tone_of_confidence():
    block = describe_score("Explosion", None, 95.0)
    assert block["tone"] == "unknown"
    assert block["display"] == "—"


# --------------------------------------------------------------------------- #
# UNKNOWN is never SAFE (spec §10)
# --------------------------------------------------------------------------- #


def test_an_unanalysed_token_says_so_rather_than_looking_clean():
    safety = describe_safety(None)
    assert not safety["analysed"]
    assert safety["risk_level_fr"] == "INCONNU"
    assert "n'est pas absence de danger" in safety["note"]


def test_full_coverage_earns_the_plain_headline():
    report = SafetyReport(
        address=ADDR, score=98.0, rug_risk=RugRisk.LOW, analysed=True, coverage=0.95
    )
    safety = describe_safety(report)
    assert not safety["partial"]
    assert safety["headline_fr"] == "Aucun risque critique détecté"


def test_a_blocking_finding_outranks_the_coverage_in_the_headline():
    report = SafetyReport(
        address=ADDR, score=0.0, rug_risk=RugRisk.CRITICAL, analysed=True, coverage=0.95,
        findings=[SafetyFinding("HONEYPOT", Severity.CRITICAL,
                                "HONEYPOT — revente impossible", blocking=True)],
    )
    safety = describe_safety(report)
    assert safety["emoji"] == "🔴"
    assert "HONEYPOT" in safety["headline_fr"]


def test_the_risk_level_and_the_coverage_are_two_separate_facts():
    """A LOW level on a 40 % reading must be able to look different from a LOW
    level on a 95 % one."""
    thin = describe_safety(SafetyReport(
        address=ADDR, score=75.0, rug_risk=RugRisk.LOW, analysed=True, coverage=0.4))
    full = describe_safety(SafetyReport(
        address=ADDR, score=75.0, rug_risk=RugRisk.LOW, analysed=True, coverage=0.95))
    assert thin["risk_level_fr"] == full["risk_level_fr"] == "FAIBLE"
    assert thin["confidence_fr"] != full["confidence_fr"]
    assert thin["partial"] and not full["partial"]


# --------------------------------------------------------------------------- #
# Liquidity (spec §8)
# --------------------------------------------------------------------------- #


def test_a_very_thin_pool_names_its_three_consequences():
    c = drco()
    liquidity = describe_liquidity(c, SETTINGS.buy_min_liquidity_usd)
    assert liquidity["severity"] == "critical"
    assert liquidity["headline_fr"] == "LIQUIDITÉ TRÈS FAIBLE"
    joined = " ".join(liquidity["consequences"]).lower()
    assert "glissement" in joined
    assert "manipulable" in joined
    assert "sortie" in joined


def test_a_sufficient_pool_raises_no_alarm():
    c = drco()
    c.liquidity_usd = 80_000.0
    liquidity = describe_liquidity(c, SETTINGS.buy_min_liquidity_usd)
    assert liquidity["severity"] == "ok"
    assert liquidity["consequences"] == []


def test_an_unknown_depth_is_never_read_as_a_deep_pool():
    c = drco()
    c.liquidity_usd = None
    liquidity = describe_liquidity(c, SETTINGS.buy_min_liquidity_usd)
    assert liquidity["severity"] == "unknown"
    assert liquidity["usd"] is None


# --------------------------------------------------------------------------- #
# History (spec §4, §13-14)
# --------------------------------------------------------------------------- #


def test_history_names_what_it_is_not_long_enough_for():
    c = drco()
    history = describe_history(c)
    assert history["insufficient_for"]
    first = history["insufficient_for"][0]
    assert first["have_fr"] == "2 min"
    assert first["needs_fr"] in ("5 min", "1,0 h")


def test_a_mature_pool_stops_warning_about_its_age():
    c = drco()
    c.pool_age_seconds = 7200.0
    history = describe_history(c)
    assert not history["very_recent"]
    assert history["insufficient_for"] == []
    assert history["warning"] is None


def test_confidence_can_progress_as_history_accumulates():
    """Spec §14: the card must improve on its own as windows elapse, without
    a restart or a new token."""
    from cryptopulse.scoring.robinhood_early import assess_confidence

    c = drco()
    # The hourly figures must actually be present, so the only thing keeping
    # them out of the count is the window that has not elapsed — which is the
    # thing time fixes on its own.
    c.pools[0].volume_usd = {"m5": 900.0, "h1": 12_000.0, "h24": 51_000.0}
    c.pools[0].buys, c.pools[0].sells = {"h1": 40}, {"h1": 22}
    c.pools[0].buyers, c.pools[0].sellers = {"h1": 31}, {"h1": 18}
    c.fdv_usd = 82_000.0

    young = assess_confidence(c)
    c.pool_age_seconds = 7200.0
    older = assess_confidence(c)
    assert older.score > young.score
    assert len(older.missing) < len(young.missing)


def test_an_unknown_age_never_pretends_to_be_a_long_history():
    c = drco()
    c.pool_age_seconds = None
    history = describe_history(c)
    assert history["available_seconds"] is None
    assert not history["very_recent"]
    assert "invérifiable" in history["warning"]


# --------------------------------------------------------------------------- #
# The explanation, for every decision (spec §21)
# --------------------------------------------------------------------------- #


def test_a_buy_has_nothing_to_explain_about_refusing():
    c = drco()
    c.liquidity_usd = 90_000.0
    c.early = EarlyScore(score=90.0)
    c.explosion = RobinhoodExplosion(score=95.0)
    c.confidence = DataConfidence(score=90.0, present=8, total=8)
    c.safety = SafetyReport(address=ADDR, score=95.0, rug_risk=RugRisk.LOW,
                            analysed=True, coverage=0.95)
    c.pool_age_seconds = 1800.0
    decision = RobinhoodTradeDecisionEngine(SETTINGS).decide_entry(c, now_ms=NOW)
    assert decision.action is TradeAction.BUY
    assert why_not_buy(decision) == []


def test_a_veto_is_reported_once_and_not_duplicated_as_a_floor():
    c = drco()
    c.safety = SafetyReport(
        address=ADDR, score=0.0, rug_risk=RugRisk.CRITICAL, analysed=True, coverage=0.9,
        findings=[SafetyFinding("HONEYPOT", Severity.CRITICAL,
                                "HONEYPOT — revente impossible", blocking=True)],
    )
    decision = RobinhoodTradeDecisionEngine(SETTINGS).decide_entry(c, now_ms=NOW)
    reasons = why_not_buy(decision)
    details = [r["detail_fr"] for r in reasons]
    assert len(details) == len(set(details))
    assert any(r["kind"] == "veto" for r in reasons)


def test_every_refusal_reason_is_readable_without_a_key():
    for reason in drco().presentation["why_not_buy"]:
        assert reason["detail_fr"]
        assert reason["kind"] in ("veto", "floor", "unavailable")
