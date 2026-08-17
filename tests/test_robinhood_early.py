"""ROBINHOOD_EARLY_SCORE_V1, pump maturity and data confidence.

The property most of these defend: the engine favours the *beginning* of a
move. A token already up 400 % is spectacular and late, and a scorer that could
not tell those apart would be an expensive way to buy tops.
"""

from __future__ import annotations

import pytest

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.hunter.robinhood_discovery import TokenCandidate
from cryptopulse.providers.geckoterminal import PoolSnapshot, TokenRef
from cryptopulse.scoring.robinhood_early import (
    EARLY_WEIGHTS,
    assess_confidence,
    assess_maturity,
    early_fingerprint,
    score_early,
)

SETTINGS = RobinhoodSettings()
ADDR = "0x" + "ab" * 20


def pool(
    *, liquidity=40_000.0, volume=None, changes=None, txns=None, fdv=82_000.0
) -> PoolSnapshot:
    p = PoolSnapshot(
        pool_address="0xpool",
        name="XYZ / WETH",
        dex="uniswap-v4",
        base=TokenRef(address=ADDR, symbol="XYZ"),
        quote=TokenRef(address="0x" + "ee" * 20, symbol="WETH"),
        created_at_ms=None,
        base_price_usd=0.00042,
        liquidity_usd=liquidity,
        fdv_usd=fdv,
    )
    p.volume_usd = volume if volume is not None else {"m5": 3000.0, "h1": 12000.0, "h24": 90000.0}
    p.price_change_pct = changes if changes is not None else {"h1": 12.0, "h6": 20.0, "h24": 25.0}
    t = txns if txns is not None else {"buys": {"m5": 20, "h1": 90}, "sells": {"m5": 6, "h1": 30},
                                       "buyers": {"h1": 45}, "sellers": {"h1": 20}}
    p.buys, p.sells, p.buyers, p.sellers = t["buys"], t["sells"], t.get("buyers", {}), t.get("sellers", {})
    return p


def candidate(*, age=600.0, pools=None, liquidity=40_000.0, fdv=82_000.0) -> TokenCandidate:
    c = TokenCandidate(address=ADDR, symbol="XYZ", name="Xyz")
    c.pools = pools if pools is not None else [pool(liquidity=liquidity, fdv=fdv)]
    c.pool_age_seconds = age
    c.price_usd = 0.00042
    c.liquidity_usd = liquidity
    c.fdv_usd = fdv
    return c


# --------------------------------------------------------------------------- #
# Pump maturity
# --------------------------------------------------------------------------- #


def test_a_quiet_fresh_token_is_early():
    m = assess_maturity(candidate(pools=[pool(changes={"h1": 3.0, "h6": 5.0, "h24": 6.0})]))
    assert m.known is True
    assert m.score < 25


def test_a_token_already_up_hundreds_of_percent_is_late():
    m = assess_maturity(candidate(pools=[pool(changes={"h1": 420.0, "h6": 800.0, "h24": 1200.0})]))
    assert m.score >= 70
    assert m.is_late is True
    assert any("étendu" in r for r in m.reasons)


def test_a_retracement_after_a_run_counts_as_late_not_early():
    """The move happened and is being given back. Reading -30 % as 'nothing has
    started' would be the most expensive possible misreading."""
    m = assess_maturity(candidate(pools=[pool(changes={"h1": -35.0, "h6": -40.0})]))
    assert m.score > 0
    assert any("repli" in r for r in m.reasons)


def test_volume_climax_is_recognised():
    m = assess_maturity(candidate(pools=[pool(
        volume={"m5": 8000.0, "h1": 20000.0, "h24": 40000.0},
        changes={"h1": 10.0},
    )]))
    assert any("climax" in r for r in m.reasons)


def test_sellers_taking_over_is_a_late_signal():
    m = assess_maturity(candidate(pools=[pool(
        txns={"buys": {"m5": 5, "h1": 40}, "sells": {"m5": 20, "h1": 60}},
        changes={"h1": 10.0},
    )]))
    assert any("vendeurs majoritaires" in r for r in m.reasons)


def test_maturity_with_no_data_is_unknown_not_zero():
    """'Probably has not started' is a claim, and it cannot be made from an
    absence of data — that is exactly how a dead token looks brand new."""
    empty = TokenCandidate(address=ADDR, symbol=None, name=None)
    m = assess_maturity(empty)
    assert m.known is False
    assert m.score == 50.0
    assert any("inconnue" in r for r in m.reasons)


# --------------------------------------------------------------------------- #
# Data confidence
# --------------------------------------------------------------------------- #


def test_confidence_counts_the_inputs_that_exist_and_have_elapsed():
    """A pool old enough for every window has full confidence."""
    full = assess_confidence(candidate(age=7200.0))
    assert full.score == 100.0
    assert full.missing == []


def test_a_bare_token_has_low_confidence_and_names_what_is_missing():
    bare = TokenCandidate(address=ADDR, symbol=None, name=None)
    c = assess_confidence(bare)
    assert c.score == 0.0
    assert "prix" in c.missing and "liquidité" in c.missing


def test_confidence_is_separate_from_the_score():
    """Spec §52: a two-minute-old token can be genuinely early and barely
    measurable at once. Collapsing the two hides whichever the reader needed."""
    young = candidate(age=90.0, pools=[pool(volume={"m5": 5000.0, "h1": 9000.0})])
    early = score_early(young, SETTINGS)
    conf = assess_confidence(young)
    assert early.score > 60, "the token can still look early"
    assert conf.score <= 50, "while half its inputs do not yet exist"
    assert not hasattr(early, "confidence"), "the two must not be blended into one object"
    # And the score says which of its own reasons rest on an unelapsed window,
    # so the reasons list is never read as pure evidence.
    assert any("fenêtre non écoulée" in r for r in early.risks)


def test_an_unelapsed_window_does_not_count_as_data():
    """Found while testing: a 90-second-old pool scored 100 % confidence
    because every field was present — including an `h1` volume covering an hour
    that mostly predates the pool. The youngest tokens, which this screen exists
    to surface, would have looked like the best-measured ones."""
    young = assess_confidence(candidate(age=90.0))
    ten_min = assess_confidence(candidate(age=600.0))
    hour_old = assess_confidence(candidate(age=3700.0))
    assert young.score < ten_min.score < hour_old.score
    assert hour_old.score == 100.0
    # At ten minutes the five-minute figures are real and the hourly ones are
    # not — per-input rather than one blanket cap on age.
    assert not any("accélération" in m for m in ten_min.missing)
    assert any("volume 1 h" in m and "non écoulée" in m for m in ten_min.missing)
    assert any("accélération" in m for m in young.missing)


def test_an_unknown_age_makes_every_windowed_input_unverifiable():
    c = candidate()
    c.pool_age_seconds = None
    conf = assess_confidence(c)
    assert conf.score < 50.0
    assert any("invérifiable" in m for m in conf.missing)


# --------------------------------------------------------------------------- #
# The early score
# --------------------------------------------------------------------------- #


def test_weights_sum_to_one_hundred():
    assert sum(EARLY_WEIGHTS.values()) == pytest.approx(100.0)


def test_an_accelerating_fresh_token_scores_well():
    s = score_early(candidate(age=400.0, pools=[pool(
        volume={"m5": 4000.0, "h1": 9000.0, "h24": 30000.0},
        changes={"h1": 8.0, "h6": 10.0},
        txns={"buys": {"m5": 40, "h1": 100}, "sells": {"m5": 8, "h1": 25},
              "buyers": {"h1": 55}, "sellers": {"h1": 18}},
    )]), SETTINGS)
    assert s.score > 65
    assert s.why


def test_a_late_token_scores_below_an_early_one_with_identical_flow():
    """The whole point of the engine, isolated: only the price extension
    differs."""
    flow = dict(volume={"m5": 4000.0, "h1": 9000.0, "h24": 30000.0},
                txns={"buys": {"m5": 40, "h1": 100}, "sells": {"m5": 8, "h1": 25},
                      "buyers": {"h1": 55}, "sellers": {"h1": 18}})
    early = score_early(candidate(pools=[pool(changes={"h1": 6.0, "h6": 9.0}, **flow)]), SETTINGS)
    late = score_early(candidate(pools=[pool(changes={"h1": 480.0, "h6": 900.0}, **flow)]), SETTINGS)
    assert late.score < early.score


def test_decelerating_volume_is_not_rewarded():
    s = score_early(candidate(pools=[pool(volume={"m5": 200.0, "h1": 12000.0})]), SETTINGS)
    vol = next(c for c in s.components if c.name == "volume_acceleration")
    assert vol.points == 0.0
    assert vol.caveats


def test_more_sells_than_buys_is_a_caveat_not_points():
    s = score_early(candidate(pools=[pool(
        txns={"buys": {"m5": 3, "h1": 20}, "sells": {"m5": 15, "h1": 80}, "buyers": {"h1": 8}},
    )]), SETTINGS)
    dom = next(c for c in s.components if c.name == "buyer_dominance")
    assert dom.points < EARLY_WEIGHTS["buyer_dominance"] * 0.4
    assert any("ventes" in c for c in dom.caveats)


def test_illiquid_tokens_score_zero_on_liquidity_and_say_why():
    s = score_early(candidate(liquidity=200.0), SETTINGS)
    liq = next(c for c in s.components if c.name == "liquidity_quality")
    assert liq.points == 0.0
    assert any("sortie non garantie" in c for c in liq.caveats)


def test_a_very_young_pool_is_not_given_the_maximum_freshness():
    """Under two minutes there is barely any flow to read; a top score would be
    confidence without data."""
    s = score_early(candidate(age=45.0), SETTINGS)
    fresh = next(c for c in s.components if c.name == "freshness")
    assert 0 < fresh.points < EARLY_WEIGHTS["freshness"]
    assert any("mesurable" in c for c in fresh.caveats)


def test_a_large_fdv_leaves_no_room():
    s = score_early(candidate(fdv=50_000_000.0), SETTINGS)
    room = next(c for c in s.components if c.name == "room_left")
    assert room.points == 0.0


def test_missing_inputs_are_marked_unavailable_not_merely_zero():
    """'Absent' and 'measured and poor' both score zero, but only one of them
    is the token's fault, and the screen has to be able to say which."""
    bare = TokenCandidate(address=ADDR, symbol=None, name=None)
    s = score_early(bare, SETTINGS)
    assert s.score == 0.0
    assert set(s.unavailable_components) >= {
        "volume_acceleration", "transaction_acceleration", "buyer_dominance",
        "freshness", "liquidity_quality", "room_left",
    }


def test_every_component_that_scores_explains_itself():
    """The rule that has held since the first engine: no points without a
    stated reason."""
    s = score_early(candidate(), SETTINGS)
    for c in s.components:
        if c.points > 0:
            assert c.reasons, f"{c.name} scored {c.points} with no reason"


def test_the_score_is_shown_out_of_100_never_as_a_percentage():
    d = score_early(candidate(), SETTINGS).to_dict()
    assert d["label"].endswith("/100")
    assert "%" not in d["label"]


def test_the_fingerprint_moves_with_the_weights_or_thresholds():
    a = early_fingerprint(RobinhoodSettings())
    b = early_fingerprint(RobinhoodSettings(discovery_min_liquidity_usd=5000.0))
    assert a != b


def test_the_score_carries_its_engine_version():
    d = score_early(candidate(), SETTINGS).to_dict()
    assert d["engine_version"] == "ROBINHOOD_EARLY_SCORE_V1"
    assert d["weights_fingerprint"]


def test_early_and_opportunity_are_different_engines():
    """Spec §4 and invariant 37: never one blended number.

    Checked on the imports rather than on the text, because the module
    legitimately *names* the other three engines in its docstring to explain
    why it is not one of them.
    """
    import ast
    import pathlib

    import cryptopulse.scoring.robinhood_early as mod

    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any("scoring.engine" in m or "scoring.components" in m for m in imported), imported
    assert mod.EARLY_ENGINE_VERSION != "SCORE_ENGINE_V1"
