"""Indicator correctness against hand-computable references."""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.features.indicators import (
    accumulation_distribution,
    atr,
    bollinger,
    bollinger_width,
    chaikin_money_flow,
    consecutive_up,
    ema,
    linreg_slope,
    macd,
    money_flow_multiplier,
    obv,
    roc,
    rolling_max,
    rolling_min,
    rolling_std,
    rolling_vwap,
    rsi,
    sma,
    true_range,
)
from cryptopulse.features.volume import relative_volume, volume_acceleration


def test_sma_matches_manual_mean():
    v = np.array([1, 2, 3, 4, 5, 6], dtype=float)
    out = sma(v, 3)
    assert np.isnan(out[0]) and np.isnan(out[1])
    np.testing.assert_allclose(out[2:], [2.0, 3.0, 4.0, 5.0])


def test_sma_warmup_is_nan_not_zero():
    out = sma(np.arange(10, dtype=float), 5)
    assert np.isnan(out[:4]).all(), "warm-up must be nan so callers cannot mistake it for a real 0"


def test_ema_seeded_with_sma_then_recursive():
    v = np.array([10, 11, 12, 13, 14], dtype=float)
    out = ema(v, 3)
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2] == pytest.approx(11.0)  # SMA seed
    alpha = 2 / 4
    assert out[3] == pytest.approx(alpha * 13 + (1 - alpha) * 11.0)
    assert out[4] == pytest.approx(alpha * 14 + (1 - alpha) * out[3])


def test_ema_of_constant_series_is_that_constant():
    out = ema(np.full(50, 7.0), 10)
    np.testing.assert_allclose(out[9:], 7.0)


def test_rsi_all_gains_is_100():
    out = rsi(np.arange(1, 40, dtype=float), 14)
    assert out[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_zero():
    out = rsi(np.arange(40, 1, -1, dtype=float), 14)
    assert out[-1] == pytest.approx(0.0, abs=1e-9)


def test_rsi_bounded_and_warm_up_nan():
    rng = np.random.default_rng(0)
    prices = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    out = rsi(prices, 14)
    valid = out[~np.isnan(out)]
    assert valid.size > 150
    assert valid.min() >= 0.0 and valid.max() <= 100.0
    assert np.isnan(out[0])


def test_true_range_uses_previous_close():
    high = np.array([10.0, 12.0])
    low = np.array([9.0, 11.0])
    close = np.array([9.5, 11.5])
    tr = true_range(high, low, close)
    assert tr[0] == pytest.approx(1.0)
    # max(12-11, |12-9.5|, |11-9.5|) = 2.5
    assert tr[1] == pytest.approx(2.5)


def test_atr_positive_and_scales_with_range():
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.normal(0, 1, 100))
    high, low = close + 2, close - 2
    a = atr(high, low, close, 14)
    valid = a[~np.isnan(a)]
    assert valid.size > 80 and (valid > 0).all()


def test_bollinger_bands_ordered_and_width_positive():
    rng = np.random.default_rng(2)
    close = 100 + np.cumsum(rng.normal(0, 0.5, 120))
    upper, mid, lower = bollinger(close, 20, 2.0)
    ok = ~np.isnan(mid)
    assert (upper[ok] >= mid[ok]).all() and (mid[ok] >= lower[ok]).all()
    bw = bollinger_width(close, 20)
    assert (bw[~np.isnan(bw)] > 0).all()


def test_bollinger_width_narrower_when_volatility_drops():
    calm = np.concatenate([np.full(40, 100.0) + np.linspace(0, 0.01, 40)])
    wild = 100 + np.cumsum(np.random.default_rng(3).normal(0, 2.0, 40))
    assert np.nanmean(bollinger_width(calm, 20)) < np.nanmean(bollinger_width(wild, 20))


def test_rolling_std_matches_numpy():
    v = np.arange(1, 11, dtype=float)
    out = rolling_std(v, 4)
    assert out[3] == pytest.approx(np.std(v[:4]))
    assert out[-1] == pytest.approx(np.std(v[-4:]))


def test_macd_histogram_is_line_minus_signal():
    rng = np.random.default_rng(4)
    close = 100 + np.cumsum(rng.normal(0, 0.4, 200))
    line, sig, hist = macd(close)
    ok = ~np.isnan(hist)
    np.testing.assert_allclose(hist[ok], (line - sig)[ok])


def test_vwap_between_low_and_high_range():
    rng = np.random.default_rng(5)
    close = 50 + np.cumsum(rng.normal(0, 0.2, 100))
    high, low = close + 0.5, close - 0.5
    vol = rng.uniform(100, 1000, 100)
    v = rolling_vwap(high, low, close, vol, 20)
    ok = ~np.isnan(v)
    assert (v[ok] >= low[ok].min()).all() and (v[ok] <= high[ok].max()).all()


def test_roc_percent_change():
    v = np.array([100.0, 110.0, 121.0])
    out = roc(v, 1)
    assert out[1] == pytest.approx(10.0)
    assert out[2] == pytest.approx(10.0)


def test_rolling_max_min():
    v = np.array([3.0, 1.0, 4.0, 1.0, 5.0])
    np.testing.assert_allclose(rolling_max(v, 3)[2:], [4.0, 4.0, 5.0])
    np.testing.assert_allclose(rolling_min(v, 3)[2:], [1.0, 1.0, 1.0])


def test_linreg_slope_on_a_straight_line():
    v = np.arange(0, 20, dtype=float) * 2.5
    out = linreg_slope(v, 5)
    assert out[-1] == pytest.approx(2.5)


def test_consecutive_up_counts_trailing_green_candles():
    close = np.array([10.0, 11.0, 12.0, 13.0])
    open_ = np.array([10.5, 10.5, 11.5, 12.5])
    assert consecutive_up(close, open_) == 3  # first candle is red
    assert consecutive_up(np.array([5.0, 4.0, 3.0]), np.array([6.0, 5.0, 4.0])) == 0


def test_relative_volume_excludes_current_bar_from_baseline():
    vol = np.concatenate([np.full(50, 100.0), [400.0]])
    rv = relative_volume(vol, 50)
    # Baseline is the previous 50 bars (all 100), so 400/100 = 4.0 exactly.
    assert rv[-1] == pytest.approx(4.0)


def test_volume_acceleration_positive_when_volume_ramps():
    vol = np.concatenate([np.full(20, 100.0), np.linspace(100, 400, 12)])
    va = volume_acceleration(vol, 3, 12)
    assert va[-1] > 20.0


def test_volume_acceleration_near_zero_when_flat():
    va = volume_acceleration(np.full(40, 250.0), 3, 12)
    assert abs(va[-1]) < 1e-6


# --------------------------------------------------------------------------- #
# Accumulation / distribution
# --------------------------------------------------------------------------- #


def test_money_flow_multiplier_maps_close_position_to_minus_one_and_one():
    high = np.array([10.0, 10.0, 10.0])
    low = np.array([8.0, 8.0, 8.0])
    close = np.array([10.0, 8.0, 9.0])  # on the high, on the low, mid-range
    mfm = money_flow_multiplier(high, low, close)
    assert mfm[0] == pytest.approx(1.0)
    assert mfm[1] == pytest.approx(-1.0)
    assert mfm[2] == pytest.approx(0.0)


def test_a_bar_with_no_range_is_neutral_rather_than_a_division_by_zero():
    mfm = money_flow_multiplier(np.array([5.0]), np.array([5.0]), np.array([5.0]))
    assert mfm[0] == 0.0
    assert np.isfinite(mfm).all()


def test_chaikin_money_flow_is_positive_when_bars_close_at_their_highs():
    n = 40
    high = np.full(n, 10.0)
    low = np.full(n, 9.0)
    buying = chaikin_money_flow(high, low, np.full(n, 10.0), np.full(n, 100.0), 20)
    selling = chaikin_money_flow(high, low, np.full(n, 9.0), np.full(n, 100.0), 20)
    assert buying[-1] == pytest.approx(1.0)
    assert selling[-1] == pytest.approx(-1.0)


def test_chaikin_money_flow_stays_bounded():
    rng = np.random.default_rng(5)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    high = close * 1.01
    low = close * 0.99
    cmf = chaikin_money_flow(high, low, close, rng.lognormal(5, 0.5, 200), 20)
    finite = cmf[~np.isnan(cmf)]
    assert finite.size > 0
    assert np.all(np.abs(finite) <= 1.0)


def test_obv_signs_volume_by_the_direction_of_the_close():
    close = np.array([10.0, 11.0, 10.5, 10.5, 12.0])
    volume = np.array([100.0, 200.0, 300.0, 400.0, 500.0])
    out = obv(close, volume)
    # First bar has no prior close, so it contributes nothing.
    assert out.tolist() == [0.0, 200.0, -100.0, -100.0, 400.0]


def test_accumulation_distribution_rises_on_strength_and_falls_on_weakness():
    n = 30
    high, low, volume = np.full(n, 10.0), np.full(n, 9.0), np.full(n, 100.0)
    up = accumulation_distribution(high, low, np.full(n, 10.0), volume)
    down = accumulation_distribution(high, low, np.full(n, 9.0), volume)
    assert up[-1] > up[0]
    assert down[-1] < down[0]
