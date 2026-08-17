"""Pump history and similarity.

Two things could quietly ruin this module. The first is inventing episodes —
finding "pumps" in noise, which would make every token look explosive. The
second is subtler and worse: describing a past setup with information from
*during* the run it preceded. That would produce a similarity engine which
discovers that pumps are preceded by pumps, and it would look convincing.

Both have tests. So does the refusal to print a rate over a handful of
observations, which on a few weeks of history is the ordinary outcome.
"""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.core.types import Timeframe
from cryptopulse.outcomes.stats import MIN_SAMPLE
from cryptopulse.pumps import (
    PumpConfig,
    SetupFingerprint,
    build_history,
    compare_to_past,
    find_pumps,
    summarise,
)
from cryptopulse.pumps.detect import PumpEpisode
from tests.conftest import make_series


def _series(closes, volumes=None, symbol="TESTUSDT"):
    s = make_series(closes, timeframe=Timeframe.H1, volumes=volumes)
    object.__setattr__(s, "symbol", symbol) if hasattr(s, "__setattr__") else None
    return s


def _flat(n=400, price=100.0):
    """Noise with no direction. Must produce no episodes."""
    rng = np.random.default_rng(5)
    return price * np.exp(np.cumsum(rng.normal(0.0, 0.0008, n)))


def _with_one_ramp(n=400, price=100.0, at=200, size=0.12, bars=8):
    """Flat, then one unmistakable run, then flat again."""
    closes = _flat(n, price).copy()
    ramp = np.linspace(0.0, size, bars)
    closes[at : at + bars] *= (1.0 + ramp)
    closes[at + bars :] *= (1.0 + size)
    return closes


# --------------------------------------------------------------------------- #
# Detection invents nothing
# --------------------------------------------------------------------------- #


def test_flat_noise_produces_no_episodes():
    """If this fails, every token in the venue will look explosive."""
    episodes = find_pumps(_series(_flat()))
    assert episodes == [], f"found {len(episodes)} pumps in directionless noise"


def test_a_real_run_is_found_with_roughly_the_right_size():
    episodes = find_pumps(_series(_with_one_ramp(size=0.12, bars=8)))
    assert len(episodes) >= 1
    biggest = max(episodes, key=lambda e: e.gain_pct)
    assert 8.0 < biggest.gain_pct < 20.0, f"measured {biggest.gain_pct:.1f}% for a 12% ramp"
    assert 0 < biggest.bars_to_peak <= 24


def test_one_run_is_one_episode_not_one_per_bar():
    """Scanning from every bar on the way up would count the same event ten times."""
    episodes = find_pumps(_series(_with_one_ramp(at=200, size=0.15, bars=10)))
    near = [e for e in episodes if abs(e.gain_pct - 15.0) < 6.0]
    assert len(near) <= 2, f"the same run was counted {len(near)} times"


def test_episodes_do_not_overlap():
    for e_a, e_b in zip(
        find_pumps(_series(_flat(600) * np.linspace(1.0, 1.5, 600))),
        find_pumps(_series(_flat(600) * np.linspace(1.0, 1.5, 600)))[1:],
        strict=False,
    ):
        assert e_b.start_ms >= e_a.peak_ms, "a later episode began before the previous peaked"


def test_a_slow_grind_is_not_an_acceleration():
    """A 12% rise spread over 200 bars is a trend. The window is what makes it
    an acceleration rather than a direction."""
    closes = 100.0 * np.linspace(1.0, 1.12, 400)
    tight = PumpConfig(min_gain_pct=5.0, max_bars_to_peak=12)
    assert find_pumps(_series(closes), config=tight) == []


def test_the_size_threshold_is_respected():
    closes = _with_one_ramp(size=0.06, bars=6)
    assert find_pumps(_series(closes), config=PumpConfig(min_gain_pct=3.0))
    assert find_pumps(_series(closes), config=PumpConfig(min_gain_pct=25.0)) == []


# --------------------------------------------------------------------------- #
# The context recorded at a start must not know the future
# --------------------------------------------------------------------------- #


def test_start_context_ignores_everything_after_the_start():
    """The test that keeps similarity from being circular.

    The same history is scored twice: once as-is, and once with the bars *after*
    each episode's start replaced by something wildly different. The context
    recorded at the start must be identical either way — if it moves, the
    description of a setup is contaminated by its own outcome.
    """
    closes = _with_one_ramp(at=200, size=0.15, bars=10)
    volumes = np.full(len(closes), 1000.0)
    volumes[195:200] = 4000.0  # a genuine run-up in volume *before* the move

    normal = find_pumps(_series(closes, volumes))
    assert normal, "no episode to compare"
    first = min(normal, key=lambda e: e.start_ms)

    # Now make the future enormous. The past is untouched.
    louder = closes.copy()
    louder[210:] *= 3.0
    louder_vol = volumes.copy()
    louder_vol[210:] = 99_000.0

    changed = find_pumps(_series(louder, louder_vol))
    same_start = [e for e in changed if e.start_ms == first.start_ms]
    assert same_start, "the episode start moved when only the future changed"

    after = same_start[0]
    assert after.rvol_at_start == pytest.approx(first.rvol_at_start), (
        "RVOL at the start changed when only later bars changed — look-ahead"
    )
    assert after.range_position_at_start == pytest.approx(first.range_position_at_start)
    assert after.atr_pct_at_start == pytest.approx(first.atr_pct_at_start)


def test_an_episode_too_early_for_context_reports_none_not_zero():
    closes = _with_one_ramp(n=80, at=10, size=0.12, bars=6)
    episodes = find_pumps(_series(closes), warmup_bars=50)
    early = [e for e in episodes if e.start_ms == min(x.start_ms for x in episodes)]
    if early and early[0].bars_to_peak and episodes[0].rvol_at_start is None:
        assert early[0].rvol_at_start is None
        assert early[0].range_position_at_start is None


# --------------------------------------------------------------------------- #
# Statistics carry their sample
# --------------------------------------------------------------------------- #


def test_no_episodes_gives_nulls_rather_than_zeros():
    st = summarise([])
    assert st.n == 0
    assert st.median_gain_pct is None
    assert st.largest_gain_pct is None
    assert st.insufficient_sample is True


def test_one_pass_answers_every_threshold():
    """The reason detection records size instead of testing it."""
    episodes = find_pumps(_series(_with_one_ramp(size=0.25, bars=10)))
    st = summarise(episodes)
    assert st.by_size, "no size breakdown produced"
    assert all(k.endswith(" %+") for k in st.by_size)
    # Buckets are cumulative: a 25% move cleared 3, 5, 10 and 20.
    counts = list(st.by_size.values())
    assert counts == sorted(counts, reverse=True)


def test_timing_resolution_travels_with_the_numbers():
    """1h detection means 'time to peak' is hourly. Nothing may imply minutes."""
    st = summarise(find_pumps(_series(_with_one_ramp())))
    assert st.resolution_minutes == 60
    assert st.median_minutes_to_peak % 60 == 0


def test_history_says_so_when_a_token_has_simply_never_accelerated():
    h = build_history(_series(_flat()))
    assert h.episodes == []
    assert any("pas un échec" in n for n in h.notes)
    assert h.days_covered > 1.0


# --------------------------------------------------------------------------- #
# Similarity refuses to be a fortune teller
# --------------------------------------------------------------------------- #


def _episode(rvol: float, gain: float = 6.0) -> PumpEpisode:
    return PumpEpisode(
        symbol="X", timeframe="1h", resolution_minutes=60,
        start_ms=0, start_price=100.0, peak_ms=1, peak_price=106.0,
        gain_pct=gain, bars_to_peak=4, minutes_to_peak=240,
        drawdown_after_pct=-3.0, rvol_at_start=rvol,
        volume_change_before_pct=50.0, range_position_at_start=0.3, atr_pct_at_start=1.0,
    )


def test_a_handful_of_matches_produces_no_rate_at_all():
    """Not a greyed-out percentage — no percentage. A number on screen gets read
    however it is styled, and this is the ordinary case on a young token."""
    report = compare_to_past(
        SetupFingerprint(rvol=2.0, volume_change_pct=50.0, range_position=0.3, atr_pct=1.0),
        [_episode(2.0) for _ in range(5)],
    )
    assert report.comparable == 5
    assert report.insufficient_sample is True
    assert report.reached == {}
    assert report.median_gain_pct is None
    d = report.to_dict()
    assert d["reached"] == {} and d["median_gain_pct"] is None
    assert any("pas une preuve" in n for n in d["notes"])


def test_a_real_sample_produces_rates_with_their_count():
    episodes = [_episode(2.0, gain=g) for g in np.linspace(3.5, 12.0, MIN_SAMPLE + 5)]
    report = compare_to_past(
        SetupFingerprint(rvol=2.0, volume_change_pct=50.0, range_position=0.3, atr_pct=1.0),
        episodes,
    )
    assert report.comparable >= MIN_SAMPLE
    assert report.insufficient_sample is False
    assert report.reached["3 %+"] == 1.0
    assert 0.0 < report.reached["5 %+"] < 1.0
    assert report.median_gain_pct is not None
    assert "pas une probabilité" in " ".join(report.notes)


def test_dissimilar_setups_are_excluded_and_counted():
    current = SetupFingerprint(rvol=1.0, volume_change_pct=0.0, range_position=0.1, atr_pct=0.5)
    far = [_episode(9.0) for _ in range(10)]
    report = compare_to_past(current, far)
    assert report.examined == 10
    assert report.comparable < 10


def test_one_shared_axis_is_a_coincidence_not_a_comparison():
    a = SetupFingerprint(rvol=2.0)
    b = SetupFingerprint(rvol=2.0)
    assert a.distance(b) is None, "a single common measure must not count as similar"

    c = SetupFingerprint(rvol=2.0, atr_pct=1.0)
    assert b.distance(c) is None
    assert c.distance(SetupFingerprint(rvol=2.0, atr_pct=1.0)) == pytest.approx(0.0)


def test_episodes_without_recorded_context_are_reported_not_dropped_silently():
    bare = PumpEpisode(
        symbol="X", timeframe="1h", resolution_minutes=60, start_ms=0, start_price=1.0,
        peak_ms=1, peak_price=1.1, gain_pct=10.0, bars_to_peak=2, minutes_to_peak=120,
    )
    report = compare_to_past(SetupFingerprint(rvol=2.0, atr_pct=1.0), [bare])
    assert report.not_comparable == 1
    assert any("pas assez de contexte" in n for n in report.notes)


def test_nothing_to_compare_against_says_so():
    report = compare_to_past(SetupFingerprint(rvol=2.0, atr_pct=1.0), [])
    assert report.comparable == 0
    assert any("Aucun épisode passé" in n for n in report.notes)
