"""The ×10 layer: what it claims, and — more importantly — what it refuses to claim.

Most of these tests are about restraint. A scanner that ranks candidates for a
ten-fold move is one bad default away from implying a probability it does not
have, scoring a daily base off a five-minute chart, or treating a missing market
cap as a small one. Each of those has a test here.
"""

from __future__ import annotations

import numpy as np

from cryptopulse.config.settings import CryptoPulseSettings, MoonshotSettings
from cryptopulse.core.types import AssetValuation, Timeframe
from cryptopulse.features.pipeline import AssetFeatures, TimeframeFeatures
from cryptopulse.scoring.moonshot import MoonshotStage, assess_moonshot
from cryptopulse.scoring.pump_maturity import PumpMaturity
from tests.conftest import FIXED_NOW_MS, make_series

CFG = MoonshotSettings()


def _features(
    closes,
    *,
    volumes=None,
    timeframe: Timeframe = Timeframe.D1,
    valuation: AssetValuation | None = None,
    rs: float | None = None,
    rvol_percentile: float | None = None,
    quote_volume_24h: float = 50_000_000.0,
) -> AssetFeatures:
    series = make_series(closes, timeframe=timeframe, volumes=volumes, end_time_ms=FIXED_NOW_MS)
    tf_feats = TimeframeFeatures.build(series, min_bars=20)
    return AssetFeatures(
        symbol="TESTUSDT",
        primary_timeframe=timeframe,
        timeframes={timeframe: tf_feats},
        quote_volume_24h=quote_volume_24h,
        valuation=valuation,
        rs_vs_benchmark_pct=rs,
        benchmark_symbol="BTCUSDT (1d, 6 bars)" if rs is not None else None,
        rvol_percentile_universe=rvol_percentile,
    )


def _maturity(score: float = 10.0) -> PumpMaturity:
    return PumpMaturity(score=score, components={}, reasons=[], coverage=1.0)


def _based_series(n: int = 200, level: float = 30.0) -> np.ndarray:
    """A long, quiet range — the shape a moonshot candidate starts from."""
    return level + np.sin(np.arange(n) * 0.35) * level * 0.02


def _crashed_then_based(n: int = 260) -> np.ndarray:
    """Rallied to 10x, collapsed, and has been flat ever since."""
    up = np.linspace(30, 300, 60)
    down = np.linspace(300, 30, 60)
    base = 30 + np.sin(np.arange(n - 120) * 0.3) * 0.6
    return np.concatenate([up, down, base])


# --------------------------------------------------------------------------- #
# What it refuses to do
# --------------------------------------------------------------------------- #


def test_it_will_not_read_a_daily_base_off_an_intraday_chart():
    af = _features(_based_series(200), timeframe=Timeframe.M5)
    m = assess_moonshot(af, _maturity(), CFG)
    assert m.stage is MoonshotStage.UNKNOWN
    assert m.score == 0.0
    assert any("intraday" in u for u in m.unknowns)


def test_too_little_history_is_unknown_rather_than_a_low_score():
    af = _features(_based_series(40))
    m = assess_moonshot(af, _maturity(), CFG)
    assert m.stage is MoonshotStage.UNKNOWN
    assert any("bars of history" in u for u in m.unknowns)


def test_a_missing_market_cap_is_unknown_and_never_treated_as_small():
    af = _features(_based_series(200), valuation=None)
    m = assess_moonshot(af, _maturity(), CFG)
    assert m.capacity is None
    assert any("market cap unknown" in u for u in m.unknowns)
    # The composite renormalises over what exists rather than scoring capacity 0.
    assert m.score > 0
    assert m.ignition is not None


def test_the_payload_never_presents_the_score_as_a_likelihood():
    af = _features(_based_series(200))
    payload = assess_moonshot(af, _maturity(), CFG).to_dict()
    assert payload["label"].endswith("/100")
    assert "probability" not in str(payload).lower()
    assert "%" not in payload["label"]


# --------------------------------------------------------------------------- #
# Headroom
# --------------------------------------------------------------------------- #


def test_an_asset_far_below_a_price_it_printed_scores_headroom():
    deep = assess_moonshot(_features(_crashed_then_based()), _maturity(), CFG)
    shallow = assess_moonshot(_features(_based_series(200)), _maturity(), CFG)
    assert deep.headroom is not None and shallow.headroom is not None
    assert deep.headroom > shallow.headroom
    assert deep.multiple_to_window_high is not None and deep.multiple_to_window_high > 5


def test_headroom_saturates_at_the_configured_target_multiple():
    cfg = MoonshotSettings(target_multiple=5.0)
    m = assess_moonshot(_features(_crashed_then_based()), _maturity(), cfg)
    assert m.headroom == 100.0  # 10x available against a 5x target
    assert m.target_multiple == 5.0


def test_a_very_old_high_is_flagged_as_a_reference_not_a_magnet():
    closes = np.concatenate([np.linspace(300, 30, 20), 30 + np.sin(np.arange(300) * 0.3) * 0.5])
    m = assess_moonshot(_features(closes), _maturity(), CFG)
    assert any("old" in c for c in m.caveats)


# --------------------------------------------------------------------------- #
# Capacity
# --------------------------------------------------------------------------- #


def test_a_small_cap_has_more_capacity_for_a_ten_fold_than_a_large_one():
    small = _features(_based_series(200), valuation=AssetValuation("TEST", market_cap_usd=30_000_000))
    large = _features(_based_series(200), valuation=AssetValuation("TEST", market_cap_usd=40_000_000_000))
    m_small = assess_moonshot(small, _maturity(), CFG)
    m_large = assess_moonshot(large, _maturity(), CFG)
    assert m_small.capacity > 80
    assert m_large.capacity < 10
    assert m_small.score > m_large.score
    assert any("very few assets have ever reached" in c for c in m_large.caveats)


def test_an_upper_bound_is_reported_as_a_floor_on_capacity_not_as_a_measurement():
    af = _features(
        _based_series(200),
        valuation=AssetValuation("TEST", market_cap_usd=None, market_cap_upper_bound_usd=200_000_000),
    )
    m = assess_moonshot(af, _maturity(), CFG)
    assert m.capacity is not None
    assert any("at least" in r and "below" in r for r in m.reasons)


def test_heavy_dilution_lowers_capacity_and_says_why():
    diluted = _features(
        _based_series(200),
        valuation=AssetValuation(
            "TEST", market_cap_usd=50_000_000, circulating_supply=100, total_supply=1000
        ),
    )
    clean = _features(_based_series(200), valuation=AssetValuation("TEST", market_cap_usd=50_000_000))
    m_diluted = assess_moonshot(diluted, _maturity(), CFG)
    m_clean = assess_moonshot(clean, _maturity(), CFG)
    assert m_diluted.capacity < m_clean.capacity
    assert any("circulating" in c for c in m_diluted.caveats)


def test_an_ambiguous_ticker_is_surfaced_rather_than_trusted_silently():
    af = _features(
        _based_series(200),
        valuation=AssetValuation("TEST", market_cap_usd=50_000_000, ambiguous_symbol=True),
    )
    m = assess_moonshot(af, _maturity(), CFG)
    assert any("more than one asset" in c for c in m.caveats)


# --------------------------------------------------------------------------- #
# Ignition and stages
# --------------------------------------------------------------------------- #


def test_volume_arriving_on_a_long_base_raises_ignition():
    n = 220
    closes = _based_series(n)
    quiet = np.full(n, 1000.0)
    arriving = np.concatenate([np.full(n - 8, 1000.0), np.linspace(2000, 9000, 8)])
    m_quiet = assess_moonshot(_features(closes, volumes=quiet), _maturity(), CFG)
    m_loud = assess_moonshot(_features(closes, volumes=arriving), _maturity(), CFG)
    assert m_loud.ignition > m_quiet.ignition
    assert m_loud.components["volume_regime"] > m_quiet.components["volume_regime"]


def test_an_extended_move_is_exhaustion_and_the_score_is_capped():
    af = _features(np.concatenate([_based_series(180), np.linspace(30, 95, 40)]))
    m = assess_moonshot(af, _maturity(score=85.0), CFG)
    assert m.stage is MoonshotStage.EXHAUSTION
    assert m.score <= CFG.exhaustion_score_cap
    assert any("not an early entry" in c for c in m.caveats)


def test_a_markup_already_under_way_is_capped_below_an_early_candidate():
    running = np.concatenate([_based_series(180, level=30.0), np.linspace(30, 75, 30)])
    m = assess_moonshot(_features(running), _maturity(score=40.0), CFG)
    assert m.stage in (MoonshotStage.EXPANSION, MoonshotStage.IGNITION)
    if m.stage is MoonshotStage.EXPANSION:
        assert m.score <= CFG.expansion_score_cap
        assert any("already under way" in c for c in m.caveats)


def test_a_trending_asset_with_no_base_is_neutral_not_dormant():
    m = assess_moonshot(_features(np.linspace(10, 26, 200)), _maturity(score=30.0), CFG)
    assert m.stage in (MoonshotStage.NEUTRAL, MoonshotStage.EXPANSION, MoonshotStage.IGNITION)
    assert m.stage is not MoonshotStage.DORMANT


def test_is_candidate_requires_both_an_early_stage_and_a_high_score():
    af = _features(_based_series(200))
    m = assess_moonshot(af, _maturity(), CFG)
    if m.stage in (MoonshotStage.ACCUMULATION, MoonshotStage.IGNITION):
        assert m.is_candidate == (m.score >= 60.0)
    else:
        assert m.is_candidate is False


# --------------------------------------------------------------------------- #
# Explainability
# --------------------------------------------------------------------------- #


def test_every_missing_input_is_named_rather_than_silently_skipped():
    """Unknown must be visible. A hole in the inputs is a hole in the reading."""
    af = _features(_based_series(200), rs=None, rvol_percentile=None, valuation=None)
    m = assess_moonshot(af, _maturity(), CFG)
    joined = " ".join(m.unknowns)
    assert "market cap" in joined
    assert "relative strength" in joined
    assert "volume rank" in joined
    assert m.coverage < 1.0


def test_cross_asset_context_is_used_when_the_scanner_supplies_it():
    strong = _features(_based_series(200), rs=30.0, rvol_percentile=0.95)
    weak = _features(_based_series(200), rs=-20.0, rvol_percentile=0.1)
    m_strong = assess_moonshot(strong, _maturity(), CFG)
    m_weak = assess_moonshot(weak, _maturity(), CFG)
    assert m_strong.ignition > m_weak.ignition
    assert any("Outperforming" in r or "outperforming" in r for r in m_strong.reasons)
    assert any("lagging" in c for c in m_weak.caveats)


def test_a_reading_carries_its_engine_version_and_the_timeframe_it_came_from():
    m = assess_moonshot(_features(_based_series(200)), _maturity(), CFG)
    assert m.engine_version == "MOONSHOT_ENGINE_V1"
    assert m.timeframe == "1d"
    assert m.to_dict()["disclaimer"]


def test_the_score_engine_attaches_a_reading_without_touching_the_opportunity_score():
    """The ×10 axis is separate: it must never move `final_score`."""
    from cryptopulse.scoring.engine import ScoreEngine

    settings = CryptoPulseSettings()
    af = _features(_based_series(200))
    with_moon = ScoreEngine(settings).score(af, FIXED_NOW_MS)

    settings_off = CryptoPulseSettings()
    settings_off.moonshot.enabled = False
    without = ScoreEngine(settings_off).score(af, FIXED_NOW_MS)

    assert with_moon.moonshot is not None
    assert without.moonshot is None
    assert with_moon.final_score == without.final_score
