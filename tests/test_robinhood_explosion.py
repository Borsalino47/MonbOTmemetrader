"""ROBINHOOD_EXPLOSION_15M_V1.

Two properties matter most here. A burst of *selling* must never score as
imminent upside — it is a burst either way, and confusing the two would be the
most expensive mistake in the module. And a veto must zero the score rather
than reduce it, so a trap cannot outrank a tradeable token in a sorted list.
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
from cryptopulse.scoring.robinhood_explosion import (
    RH_EXPLOSION_HORIZON_MINUTES,
    RH_EXPLOSION_WEIGHTS,
    explosion_fingerprint,
    score_explosion,
)

SETTINGS = RobinhoodSettings()
ADDR = "0x" + "ab" * 20


def pool(*, liquidity=60_000.0, volume=None, changes=None, txns=None) -> PoolSnapshot:
    p = PoolSnapshot(
        pool_address="0xpool", name="XYZ / WETH", dex="uniswap-v4",
        base=TokenRef(address=ADDR, symbol="XYZ"),
        quote=TokenRef(address="0x" + "ee" * 20, symbol="WETH"),
        created_at_ms=None, base_price_usd=0.00042, liquidity_usd=liquidity,
    )
    p.volume_usd = volume if volume is not None else {"m5": 5000.0, "h1": 12000.0}
    p.price_change_pct = changes if changes is not None else {"m5": 6.0, "h1": 15.0}
    t = txns if txns is not None else {"buys": {"m5": 60, "h1": 120}, "sells": {"m5": 8, "h1": 40}}
    p.buys, p.sells = t["buys"], t["sells"]
    p.buyers, p.sellers = t.get("buyers", {}), t.get("sellers", {})
    return p


def candidate(*, liquidity=60_000.0, safety=None, **pool_kwargs) -> TokenCandidate:
    c = TokenCandidate(address=ADDR, symbol="XYZ", name="Xyz")
    c.pools = [pool(liquidity=liquidity, **pool_kwargs)]
    c.liquidity_usd = liquidity
    c.pool_age_seconds = 900.0
    c.safety = safety
    return c


def clean_safety() -> SafetyReport:
    return SafetyReport(address=ADDR, score=95.0, rug_risk=RugRisk.LOW, analysed=True)


def vetoed_safety() -> SafetyReport:
    return SafetyReport(
        address=ADDR, score=0.0, rug_risk=RugRisk.CRITICAL, analysed=True,
        findings=[SafetyFinding("HONEYPOT", Severity.CRITICAL, "HONEYPOT — revente impossible",
                                blocking=True)],
    )


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #


def test_weights_sum_to_one_hundred():
    assert sum(RH_EXPLOSION_WEIGHTS.values()) == pytest.approx(100.0)


def test_the_horizon_is_part_of_the_fingerprint():
    """Invariant 38: the same weights over a different window are a different
    claim, so the window cannot be a free parameter."""
    import cryptopulse.scoring.robinhood_explosion as mod

    original = mod.RH_EXPLOSION_HORIZON_MINUTES
    before = explosion_fingerprint(SETTINGS)
    mod.RH_EXPLOSION_HORIZON_MINUTES = 60
    try:
        assert explosion_fingerprint(SETTINGS) != before
    finally:
        mod.RH_EXPLOSION_HORIZON_MINUTES = original


def test_the_horizon_matches_the_one_already_measured():
    """15 minutes because outcomes/horizons.py records exactly that window —
    this is the Robinhood score that will be checkable first."""
    from cryptopulse.outcomes.horizons import HORIZONS

    assert RH_EXPLOSION_HORIZON_MINUTES == 15
    assert any(h.minutes == 15 for h in HORIZONS)


def test_the_score_is_never_presented_as_a_probability():
    d = score_explosion(candidate(safety=clean_safety()), SETTINGS).to_dict()
    assert d["label"].endswith("/100")
    assert "%" not in d["label"]
    assert "pas une probabilité" in d["note"]


# --------------------------------------------------------------------------- #
# The gates
# --------------------------------------------------------------------------- #


def test_a_safety_veto_zeroes_rather_than_reduces():
    """Invariant 40 and 72. A reduced score would still let a honeypot outrank
    a sound token in a sorted list."""
    s = score_explosion(candidate(safety=vetoed_safety()), SETTINGS)
    assert s.score == 0.0
    assert s.vetoed is True
    assert "HONEYPOT" in (s.veto_reason or "")


def test_a_zero_always_carries_its_reason():
    """A silent zero is indistinguishable from a calm market."""
    for c in (candidate(safety=vetoed_safety()), candidate(liquidity=200.0)):
        s = score_explosion(c, SETTINGS)
        assert s.score == 0.0
        assert s.veto_reason
        assert s.caveats


def test_illiquid_tokens_are_vetoed_not_celebrated():
    """A 10 % move on nine hundred dollars is a trap, not an opportunity."""
    s = score_explosion(candidate(liquidity=300.0), SETTINGS)
    assert s.vetoed is True
    assert s.score == 0.0


# --------------------------------------------------------------------------- #
# The signal
# --------------------------------------------------------------------------- #


def test_a_genuine_burst_scores_high():
    s = score_explosion(candidate(
        safety=clean_safety(),
        volume={"m5": 9000.0, "h1": 12000.0},
        txns={"buys": {"m5": 90, "h1": 130}, "sells": {"m5": 10, "h1": 40}},
        changes={"m5": 8.0, "h1": 20.0},
    ), SETTINGS)
    assert s.score > 80
    assert s.reasons


def test_a_selling_burst_does_not_score_as_imminent_upside():
    """The mistake this module most needs to avoid: a burst of selling is also
    a burst. Volume and trades are identical; only the direction differs."""
    buying = score_explosion(candidate(
        safety=clean_safety(),
        volume={"m5": 9000.0, "h1": 12000.0},
        txns={"buys": {"m5": 90, "h1": 130}, "sells": {"m5": 10, "h1": 40}},
        changes={"m5": 6.0},
    ), SETTINGS)
    selling = score_explosion(candidate(
        safety=clean_safety(),
        volume={"m5": 9000.0, "h1": 12000.0},
        txns={"buys": {"m5": 10, "h1": 130}, "sells": {"m5": 90, "h1": 40}},
        changes={"m5": -6.0},
    ), SETTINGS)
    assert selling.score < buying.score
    assert any("vendeurs majoritaires" in c for c in selling.caveats)


def test_a_bounce_inside_a_decline_is_discounted():
    """+3 % in five minutes after −40 % in an hour is a bounce, not a start."""
    healthy = score_explosion(candidate(safety=clean_safety(), changes={"m5": 3.0, "h1": 10.0}), SETTINGS)
    bouncing = score_explosion(candidate(safety=clean_safety(), changes={"m5": 3.0, "h1": -40.0}), SETTINGS)
    assert bouncing.score < healthy.score
    assert any("rebond" in c for c in bouncing.caveats)


def test_a_quiet_token_scores_low():
    s = score_explosion(candidate(
        safety=clean_safety(),
        volume={"m5": 100.0, "h1": 12000.0},
        txns={"buys": {"m5": 1, "h1": 100}, "sells": {"m5": 1, "h1": 90}},
        changes={"m5": 0.1, "h1": 0.5},
    ), SETTINGS)
    assert s.score < 30


def test_missing_inputs_are_listed_rather_than_silently_zero():
    bare = TokenCandidate(address=ADDR, symbol=None, name=None)
    bare.safety = clean_safety()
    s = score_explosion(bare, SETTINGS)
    assert s.score == 0.0
    assert set(s.unavailable) >= {"volume_burst", "trade_burst", "buy_pressure", "thrust", "depth"}
    assert s.caveats


def test_a_token_with_no_safety_analysis_is_still_scored_but_the_caller_gates():
    """This engine only vetoes on a *present* veto. A missing safety report is
    handled by the decision layer, which refuses a BUY on thin data (§53) —
    duplicating that rule here would make it two rules that could drift."""
    s = score_explosion(candidate(safety=None), SETTINGS)
    assert s.vetoed is False
    assert s.score > 0


def test_explosion_and_early_are_separate_engines():
    """Invariant 37: they answer different questions and must be able to
    disagree. A six-hour-old climber can be explosive and not early."""
    from cryptopulse.scoring.robinhood_early import EARLY_ENGINE_VERSION, score_early

    old_climber = candidate(
        safety=clean_safety(),
        volume={"m5": 9000.0, "h1": 12000.0},
        txns={"buys": {"m5": 90, "h1": 130}, "sells": {"m5": 10, "h1": 40}},
        changes={"m5": 8.0, "h1": 250.0, "h6": 600.0},
    )
    old_climber.pool_age_seconds = 6 * 3600.0
    old_climber.fdv_usd = 40_000_000.0

    boom = score_explosion(old_climber, SETTINGS)
    early = score_early(old_climber, SETTINGS)
    assert boom.score > early.score, "explosive but late — the two must diverge"
    assert boom.version != EARLY_ENGINE_VERSION
    assert boom.weights_fingerprint


def test_every_component_that_scores_has_a_reason_or_is_neutral():
    s = score_explosion(candidate(safety=clean_safety()), SETTINGS)
    assert sum(s.components.values()) == pytest.approx(s.score)
    assert s.reasons, "a score above zero must be explainable"
