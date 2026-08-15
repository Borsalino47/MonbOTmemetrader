"""Liquidity gate, safety gate and penalty behaviour."""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import AssetFeatures, TimeframeFeatures
from cryptopulse.risk.liquidity import LiquidityStatus, assess_liquidity
from cryptopulse.risk.penalties import compute_penalties
from cryptopulse.risk.safety import DexSafetyInputs, assess_cex_safety, dex_safety
from cryptopulse.scoring.pump_maturity import compute_pump_maturity
from tests.conftest import make_series

SETTINGS = CryptoPulseSettings()


def _asset(quote_volume=None, spread_bps=None, closes=None, n=300) -> AssetFeatures:
    rng = np.random.default_rng(21)
    closes = closes if closes is not None else 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.004, n)))
    per_tf = {
        tf: TimeframeFeatures.build(make_series(closes, timeframe=tf), min_bars=60)
        for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)
    }
    af = AssetFeatures(
        symbol="TESTUSDT", primary_timeframe=Timeframe.M5, timeframes=per_tf, quote_volume_24h=quote_volume
    )
    af.spread_bps = spread_bps
    return af


# --------------------------------------------------------------------------- #
# Liquidity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "volume,expected",
    [
        (500_000_000.0, LiquidityStatus.EXCELLENT),
        (50_000_000.0, LiquidityStatus.GOOD),
        (5_000_000.0, LiquidityStatus.ACCEPTABLE),
        (1_000_000.0, LiquidityStatus.POOR),
        (50_000.0, LiquidityStatus.DANGEROUS),
    ],
)
def test_liquidity_tiers_by_volume(volume, expected):
    assert assess_liquidity(_asset(volume), SETTINGS.risk).status is expected


def test_dangerous_liquidity_sets_the_veto():
    a = assess_liquidity(_asset(50_000.0), SETTINGS.risk)
    assert a.veto is True
    assert a.status is LiquidityStatus.DANGEROUS


def test_unknown_volume_is_unknown_not_zero():
    a = assess_liquidity(_asset(None), SETTINGS.risk)
    assert a.status is LiquidityStatus.UNKNOWN
    assert a.fraction is None, "unknown liquidity must not be scored as if it were measured"
    assert a.veto is False, "unknown is not the same as dangerous"
    assert "DATA_UNAVAILABLE" in " ".join(a.reasons)


def test_wide_spread_downgrades_an_otherwise_liquid_asset():
    tight = assess_liquidity(_asset(500_000_000.0, spread_bps=2.0), SETTINGS.risk)
    wide = assess_liquidity(_asset(500_000_000.0, spread_bps=200.0), SETTINGS.risk)
    assert tight.status is LiquidityStatus.EXCELLENT
    assert wide.status is LiquidityStatus.DANGEROUS
    assert wide.veto is True


def test_liquidity_status_ordering_is_consistent():
    assert LiquidityStatus.EXCELLENT.rank > LiquidityStatus.GOOD.rank > LiquidityStatus.ACCEPTABLE.rank
    assert LiquidityStatus.ACCEPTABLE.rank > LiquidityStatus.POOR.rank > LiquidityStatus.DANGEROUS.rank


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #


def test_safety_penalises_illiquidity_and_sets_hard_veto():
    af = _asset(50_000.0)
    liq = assess_liquidity(af, SETTINGS.risk)
    safety = assess_cex_safety(af, liq, SETTINGS.risk)
    assert safety.hard_veto is True
    assert safety.score < 50


def test_healthy_asset_scores_high_on_safety():
    af = _asset(500_000_000.0, spread_bps=3.0)
    liq = assess_liquidity(af, SETTINGS.risk)
    safety = assess_cex_safety(af, liq, SETTINGS.risk)
    assert safety.score >= 80
    assert safety.hard_veto is False


def test_safety_score_is_bounded():
    for vol in (50_000.0, 5_000_000.0, 900_000_000.0, None):
        af = _asset(vol)
        s = assess_cex_safety(af, assess_liquidity(af, SETTINGS.risk), SETTINGS.risk)
        assert 0.0 <= s.score <= 100.0


def test_every_safety_deduction_is_named():
    af = _asset(50_000.0)
    s = assess_cex_safety(af, assess_liquidity(af, SETTINGS.risk), SETTINGS.risk)
    assert s.reasons, "a safety score below 100 must say why"


def test_dex_safety_refuses_to_invent_a_number():
    """Phase 2 surface must fail loudly rather than return a plausible default."""
    with pytest.raises(NotImplementedError, match="NOT IMPLEMENTED"):
        dex_safety(DexSafetyInputs(liquidity_usd=25_000.0), SETTINGS.risk)


# --------------------------------------------------------------------------- #
# Penalties
# --------------------------------------------------------------------------- #


def test_penalties_are_capped():
    rng = np.random.default_rng(22)
    awful = 100 * np.exp(np.cumsum(rng.normal(-0.004, 0.05, 300)))
    af = _asset(60_000.0, spread_bps=300.0, closes=awful)
    liq = assess_liquidity(af, SETTINGS.risk)
    report = compute_penalties(af, compute_pump_maturity(af), liq, SETTINGS.scoring, SETTINGS.risk)
    assert report.total <= SETTINGS.risk.max_risk_penalty
    assert len(report.items) >= 3


def test_clean_asset_has_little_or_no_penalty():
    af = _asset(500_000_000.0, spread_bps=2.0)
    liq = assess_liquidity(af, SETTINGS.risk)
    report = compute_penalties(af, compute_pump_maturity(af), liq, SETTINGS.scoring, SETTINGS.risk)
    assert report.total < 15


def test_every_penalty_states_its_reason():
    af = _asset(60_000.0, spread_bps=300.0)
    liq = assess_liquidity(af, SETTINGS.risk)
    report = compute_penalties(af, compute_pump_maturity(af), liq, SETTINGS.scoring, SETTINGS.risk)
    for p in report.items:
        assert p.reason and p.points > 0
