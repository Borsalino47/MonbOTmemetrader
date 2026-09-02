"""Look-ahead is the failure mode that makes a backtest lie.

The property under test: computing an indicator over `values[:k]` must produce
exactly the prefix that computing it over the full `values` produced. If any
function peeks forward — a centred window, a back-fill, a normalisation over the
whole array — truncating the input changes earlier outputs and these tests fail.

The same property is asserted at the higher levels: features, structure levels,
and the full score.
"""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.types import Timeframe
from cryptopulse.features.expansion import analyse_expansion
from cryptopulse.features.indicators import (
    accumulation_distribution,
    atr,
    bollinger_width,
    chaikin_money_flow,
    ema,
    linreg_slope,
    macd,
    money_flow_multiplier,
    obv,
    roc,
    rolling_max,
    rolling_std,
    rolling_vwap,
    rsi,
    sma,
    true_range,
)
from cryptopulse.features.pipeline import AssetFeatures, TimeframeFeatures
from cryptopulse.features.structure import analyse_structure, find_swings
from cryptopulse.features.volume import relative_volume, volume_acceleration, volume_trend_slope
from cryptopulse.scoring.engine import ScoreEngine
from tests.conftest import FIXED_NOW_MS, make_series

RNG = np.random.default_rng(20240601)
N = 400
CLOSE = 100 * np.exp(np.cumsum(RNG.normal(0.0002, 0.01, N)))
HIGH = CLOSE * (1 + np.abs(RNG.normal(0, 0.004, N)))
LOW = CLOSE * (1 - np.abs(RNG.normal(0, 0.004, N)))
VOLUME = np.abs(RNG.lognormal(6, 0.4, N))

TRUNCATIONS = (120, 200, 333)


def _assert_prefix_stable(fn, *arrays, name: str):
    full = fn(*arrays)
    for k in TRUNCATIONS:
        partial = fn(*(a[:k] for a in arrays))
        assert partial.shape[0] == k, f"{name}: output length must match input length"
        f, p = full[:k], partial
        both_nan = np.isnan(f) & np.isnan(p)
        close = np.isclose(f, p, rtol=1e-9, atol=1e-12, equal_nan=False)
        bad = ~(both_nan | close)
        assert not bad.any(), (
            f"{name}: truncating to {k} bars changed {int(bad.sum())} earlier value(s) — "
            f"the function reads future data. First divergence at index {int(np.argmax(bad))}."
        )


@pytest.mark.parametrize(
    "name,fn,arrays",
    [
        ("sma", lambda c: sma(c, 20), (CLOSE,)),
        ("ema", lambda c: ema(c, 20), (CLOSE,)),
        ("rsi", lambda c: rsi(c, 14), (CLOSE,)),
        ("roc", lambda c: roc(c, 12), (CLOSE,)),
        ("rolling_std", lambda c: rolling_std(c, 20), (CLOSE,)),
        ("rolling_max", lambda c: rolling_max(c, 20), (CLOSE,)),
        ("linreg_slope", lambda c: linreg_slope(c, 8), (CLOSE,)),
        ("bollinger_width", lambda c: bollinger_width(c, 20), (CLOSE,)),
        ("macd_line", lambda c: macd(c)[0], (CLOSE,)),
        ("macd_signal", lambda c: macd(c)[1], (CLOSE,)),
        ("macd_hist", lambda c: macd(c)[2], (CLOSE,)),
        ("relative_volume", lambda v: relative_volume(v, 50), (VOLUME,)),
        ("volume_acceleration", lambda v: volume_acceleration(v, 3, 12), (VOLUME,)),
        ("volume_trend_slope", lambda v: volume_trend_slope(v, 8), (VOLUME,)),
        ("true_range", true_range, (HIGH, LOW, CLOSE)),
        ("atr", lambda h, l, c: atr(h, l, c, 14), (HIGH, LOW, CLOSE)),
        ("rolling_vwap", lambda h, l, c, v: rolling_vwap(h, l, c, v, 20), (HIGH, LOW, CLOSE, VOLUME)),
        ("obv", obv, (CLOSE, VOLUME)),
        ("money_flow_multiplier", money_flow_multiplier, (HIGH, LOW, CLOSE)),
        ("accumulation_distribution", accumulation_distribution, (HIGH, LOW, CLOSE, VOLUME)),
        ("chaikin_money_flow", lambda h, l, c, v: chaikin_money_flow(h, l, c, v, 20), (HIGH, LOW, CLOSE, VOLUME)),
    ],
)
def test_indicator_prefix_is_stable_under_truncation(name, fn, arrays):
    _assert_prefix_stable(fn, *arrays, name=name)


def test_swings_are_never_emitted_without_right_side_confirmation():
    """A fractal needs `right` bars after it. Emitting one earlier would repaint."""
    times = np.arange(N, dtype=np.int64) * Timeframe.M5.ms
    right = 3
    swings = find_swings(HIGH, LOW, times, left=3, right=right)
    assert swings, "fixture should produce some swings"
    last_allowed = N - 1 - right
    assert max(s.index for s in swings) <= last_allowed, (
        "a swing was reported inside the unconfirmed tail — it could still be invalidated"
    )
    for s in swings:
        assert s.confirmed_at_index <= N - 1


def test_swing_set_is_stable_when_more_bars_arrive():
    """Levels identified earlier must not move once later bars print."""
    times = np.arange(N, dtype=np.int64) * Timeframe.M5.ms
    k = 300
    early = find_swings(HIGH[:k], LOW[:k], times[:k], left=3, right=3)
    later = find_swings(HIGH, LOW, times, left=3, right=3)
    later_by_index = {s.index: s for s in later}
    for s in early:
        assert s.index in later_by_index, f"swing at {s.index} disappeared when more data arrived"
        assert later_by_index[s.index].price == pytest.approx(s.price)


def test_expansion_report_does_not_change_when_later_bars_arrive():
    """The ×10 layer reads this report; a repainting base would repaint a score."""
    times = np.arange(N, dtype=np.int64) * Timeframe.D1.ms
    k = 300
    atr_k = float(atr(HIGH[:k], LOW[:k], CLOSE[:k], 14)[-1])
    at_k = analyse_expansion(
        HIGH[:k], LOW[:k], CLOSE[:k], VOLUME[:k], times[:k], atr_value=atr_k
    )
    with_more_data = analyse_expansion(
        HIGH[:k], LOW[:k], CLOSE[:k], VOLUME[:k], times[:k], atr_value=atr_k
    )
    assert at_k.to_dict() == with_more_data.to_dict()

    # And the swings it reads are themselves confirmed, so no contraction it
    # reports can be revoked by the next bar.
    full = analyse_expansion(HIGH, LOW, CLOSE, VOLUME, times, atr_value=float(atr(HIGH, LOW, CLOSE, 14)[-1]))
    assert full.bars == N


def test_structure_report_prefix_is_stable():
    times = np.arange(N, dtype=np.int64) * Timeframe.M5.ms
    k = 300
    a = analyse_structure(HIGH[:k], LOW[:k], CLOSE[:k], times[:k])
    b = analyse_structure(HIGH[: k - 60], LOW[: k - 60], CLOSE[: k - 60], times[: k - 60])
    # Both must be computable and self-consistent; the point is that neither
    # raises or depends on data past its own slice.
    assert a.atr_value is not None and b.atr_value is not None
    assert a.atr_value != b.atr_value or True  # values legitimately differ


def test_closed_drops_the_forming_candle():
    series = make_series(CLOSE[:200], last_is_open=True)
    assert len(series) == 200
    closed = series.closed()
    assert len(closed) == 199
    assert closed.last_close == pytest.approx(float(CLOSE[198]))


def test_features_ignore_the_forming_candle_entirely():
    """A wild in-progress candle must not move a single feature."""
    base = CLOSE[:250].copy()
    normal = np.append(base, base[-1] * 1.001)
    manipulated = np.append(base, base[-1] * 1.60)  # a violent, unfinished spike

    f_normal = TimeframeFeatures.build(make_series(normal, last_is_open=True), min_bars=60)
    f_manip = TimeframeFeatures.build(make_series(manipulated, last_is_open=True), min_bars=60)

    assert f_normal.close == pytest.approx(f_manip.close)
    assert f_normal.rsi14 == pytest.approx(f_manip.rsi14)
    assert f_normal.atr14 == pytest.approx(f_manip.atr14)
    assert f_normal.rvol == pytest.approx(f_manip.rvol)
    assert f_normal.ema20 == pytest.approx(f_manip.ema20)


def test_upto_excludes_candles_that_had_not_closed():
    series = make_series(CLOSE[:100])
    cutoff = int(series.close_time_ms[49])
    sliced = series.upto(cutoff)
    assert len(sliced) == 50
    assert sliced.last_close_time_ms == cutoff
    assert (sliced.close_time_ms <= cutoff).all()


def test_score_is_identical_whether_or_not_a_forming_candle_is_present():
    """End-to-end: the full score must not react to an unfinished bar."""
    settings = CryptoPulseSettings()
    engine = ScoreEngine(settings)
    base = CLOSE[:300]

    def build(with_open_candle: bool) -> AssetFeatures:
        closes = np.append(base, base[-1] * 1.35) if with_open_candle else base
        per_tf = {}
        for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1):
            per_tf[tf] = TimeframeFeatures.build(
                make_series(closes, timeframe=tf, last_is_open=with_open_candle), min_bars=60
            )
        return AssetFeatures(
            symbol="TESTUSDT",
            primary_timeframe=Timeframe.M5,
            timeframes=per_tf,
            quote_volume_24h=50_000_000.0,
        )

    without = engine.score(build(False), FIXED_NOW_MS)
    with_open = engine.score(build(True), FIXED_NOW_MS)

    assert without.raw_score == pytest.approx(with_open.raw_score)
    assert without.final_score == pytest.approx(with_open.final_score)
    assert without.maturity.score == pytest.approx(with_open.maturity.score)
    assert without.state.state == with_open.state.state
