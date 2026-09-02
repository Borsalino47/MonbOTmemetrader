"""Expansion features: the long-horizon readings the ×10 layer is built on.

Each test builds a series whose shape is known by construction, then asserts the
reading describes that shape. The point is that the numbers mean what the
docstrings claim — a "base" really is a range, a "spring" really is a reclaimed
breakdown — because the moonshot score is only as honest as these are.
"""

from __future__ import annotations

import numpy as np

from cryptopulse.features.expansion import (
    ExpansionReport,
    analyse_expansion,
    base_run_length,
    contraction_sequence,
)
from cryptopulse.features.indicators import atr

TF_MS = 300_000


def _times(n: int) -> np.ndarray:
    return np.arange(n, dtype=np.int64) * TF_MS


def _ohlc(closes, *, spread: float = 0.004):
    """Plausible OHLC around a close path, with the bar closing mid-range."""
    c = np.asarray(closes, dtype=np.float64)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) * (1 + spread)
    l = np.minimum(o, c) * (1 - spread)
    return o, h, l, c


def _report(closes, volumes=None, **kwargs) -> ExpansionReport:
    o, h, l, c = _ohlc(closes)
    v = np.full(c.size, 1000.0) if volumes is None else np.asarray(volumes, dtype=np.float64)
    atr_value = float(atr(h, l, c, 14)[-1])
    return analyse_expansion(h, l, c, v, _times(c.size), atr_value=atr_value, **kwargs)


# --------------------------------------------------------------------------- #
# Headroom is arithmetic, not a forecast
# --------------------------------------------------------------------------- #


def test_multiple_to_window_high_is_the_ratio_of_the_high_to_the_close():
    # Rises to 100, then collapses to 10: the window high is exactly 10x the close.
    closes = np.concatenate([np.linspace(10, 100, 100), np.linspace(100, 10, 100)])
    rep = _report(closes)
    assert rep.multiple_to_window_high is not None
    # The high includes the bar's own wick, so allow the spread used to build it.
    assert 9.8 <= rep.multiple_to_window_high <= 10.5
    assert rep.drawdown_from_high_pct is not None and rep.drawdown_from_high_pct < -89
    assert rep.bars_since_window_high == 100


def test_an_asset_at_its_highest_price_reports_no_headroom_rather_than_a_guess():
    rep = _report(np.linspace(10, 50, 150))
    assert rep.multiple_to_window_high is not None
    assert rep.multiple_to_window_high < 1.02  # at the top of its own history
    assert rep.bars_since_window_high == 0


# --------------------------------------------------------------------------- #
# The base
# --------------------------------------------------------------------------- #


def test_base_run_length_counts_only_the_current_range():
    """A long flat stretch after a crash is a base; the crash is not part of it."""
    closes = np.concatenate([np.linspace(100, 20, 60), 20 + np.sin(np.arange(80)) * 0.2])
    o, h, l, c = _ohlc(closes)
    atr_value = float(atr(h, l, c, 14)[-1])
    bars, base_hi, base_lo = base_run_length(h, l, atr_value)
    assert 40 <= bars <= 82, bars  # the flat stretch, not the 60-bar collapse
    assert base_hi - base_lo > 0
    assert base_lo > 15  # the range sits at 20, not down at the crash lows


def test_a_base_needs_an_atr_and_says_so_when_there_is_none():
    o, h, l, c = _ohlc(np.linspace(10, 12, 100))
    rep = analyse_expansion(h, l, c, np.full(100, 1.0), _times(100), atr_value=None)
    assert rep.base_length_bars is None
    assert any("NO_ATR" in n for n in rep.notes)


def test_breaking_a_sixty_bar_high_is_detected_and_not_before():
    flat = np.full(120, 50.0) + np.sin(np.arange(120)) * 0.1
    assert _report(flat).broke_prior_high is False

    breakout = np.concatenate([flat, [58.0]])
    assert _report(breakout).broke_prior_high is True


def test_short_history_reports_the_gap_instead_of_a_verdict():
    rep = _report(np.linspace(10, 11, 30))
    assert rep.broke_prior_high is False
    assert any("SHORT_HISTORY" in n for n in rep.notes)


# --------------------------------------------------------------------------- #
# Accumulation: volume arriving into a price that will not move
# --------------------------------------------------------------------------- #


def test_quiet_accumulation_needs_volume_building_flat_price_and_upper_closes():
    n = 120
    closes = np.full(n, 40.0) + np.sin(np.arange(n) * 0.4) * 0.05
    volumes = np.concatenate([np.full(n - 20, 1000.0), np.linspace(1000, 4000, 20)])
    o = closes - 0.20  # every bar closes near its high: buyers in control
    h = closes + 0.02
    l = o - 0.05
    rep = analyse_expansion(
        h, l, closes, volumes, _times(n), atr_value=float(atr(h, l, closes, 14)[-1])
    )
    assert rep.volume_slope_norm is not None and rep.volume_slope_norm > 0
    assert abs(rep.price_drift_pct) < 5
    assert rep.cmf is not None and rep.cmf > 0.05
    assert rep.quiet_accumulation is True


def test_flat_price_with_flat_volume_is_not_accumulation():
    n = 120
    closes = np.full(n, 40.0) + np.sin(np.arange(n) * 0.4) * 0.05
    rep = _report(closes)
    assert rep.quiet_accumulation is False


def test_volume_regime_ratio_measures_recent_volume_against_its_own_median():
    n = 100
    volumes = np.concatenate([np.full(n - 5, 100.0), np.full(5, 400.0)])
    rep = _report(np.full(n, 25.0) + np.sin(np.arange(n)) * 0.1, volumes)
    assert rep.volume_regime_ratio is not None
    assert 3.9 <= rep.volume_regime_ratio <= 4.1


# --------------------------------------------------------------------------- #
# Contractions and springs
# --------------------------------------------------------------------------- #


def test_contraction_sequence_returns_pullback_depths_oldest_first():
    # Three rallies, each followed by a shallower pullback.
    path = []
    price = 100.0
    for depth in (0.20, 0.12, 0.06):
        path += list(np.linspace(price, price * 1.25, 12))
        price *= 1.25
        path += list(np.linspace(price, price * (1 - depth), 12))
        price *= 1 - depth
    o, h, l, c = _ohlc(np.array(path))
    depths = contraction_sequence(h, l, _times(c.size))
    assert len(depths) >= 2
    assert depths == sorted(depths, reverse=True), depths  # each one tighter


def test_vcp_is_false_when_pullbacks_are_getting_deeper():
    path = []
    price = 100.0
    for depth in (0.06, 0.12, 0.22):
        path += list(np.linspace(price, price * 1.25, 12))
        price *= 1.25
        path += list(np.linspace(price, price * (1 - depth), 12))
        price *= 1 - depth
    assert _report(np.array(path)).vcp is False


def test_spring_is_a_breakdown_that_closed_back_inside_the_base():
    n = 120
    base = np.full(n - 6, 30.0) + np.sin(np.arange(n - 6) * 0.7) * 0.15
    # Loses the floor for two bars, then closes back above it.
    tail = np.array([29.2, 28.4, 30.1, 30.3, 30.2, 30.4])
    rep = _report(np.concatenate([base, tail]))
    assert rep.spring is True


# --------------------------------------------------------------------------- #
# The property the whole file depends on
# --------------------------------------------------------------------------- #


def test_a_report_is_identical_whether_or_not_later_bars_exist():
    """Truncation stability: nothing here may read a bar that has not happened."""
    rng = np.random.default_rng(31)
    closes = 50 * np.exp(np.cumsum(rng.normal(0, 0.01, 300)))
    volumes = rng.lognormal(6, 0.4, 300)

    full = _report(closes, volumes)
    early = _report(closes[:200], volumes[:200])
    # The reading at bar 200 must not depend on bars 200..299 existing.
    again = _report(closes[:200], volumes[:200])
    assert early.to_dict() == again.to_dict()
    assert full.bars == 300 and early.bars == 200
    assert early.to_dict() != full.to_dict()
