"""Grading Robinhood decisions against the bars that followed them.

WHY THIS FILE MATTERS MORE THAN THE ENGINES IT CHECKS

Every Robinhood engine so far produces an opinion under weights nobody has
validated. This is the only machinery that can ever show one of them to be
wrong, so the things it must never do are the things tested hardest:

  * settle a window that has not elapsed (invariant 14);
  * re-grade a window already recorded (invariant 10);
  * count an unfetchable window as a loss (invariant 11);
  * show a rate without its `n` (invariant 12);
  * resolve against a neighbouring bar instead of the exact one.
"""

from __future__ import annotations

import pytest

from cryptopulse.outcomes.robinhood_outcomes import (
    RH_HORIZONS,
    RobinhoodCosts,
    RobinhoodHorizonStatus,
    RobinhoodHorizonTracker,
)
from cryptopulse.outcomes.robinhood_stats import (
    MIN_SAMPLE,
    build_robinhood_performance,
)

ADDR = "0x" + "ab" * 20
POOL = "0xpool"
STEP = 5 * 60_000
T0 = 1_700_000_000_000 // STEP * STEP   # aligned on a 5-minute boundary


def candles(n: int = 400, *, start_ms: int = T0, price: float = 1.0, drift: float = 0.0):
    """A flat-or-drifting series, oldest first, as the client returns it."""
    out = []
    for i in range(n):
        p = price * (1.0 + drift * i)
        out.append((start_ms + i * STEP, p, p * 1.02, p * 0.98, p, 1000.0))
    return out


class Gecko:
    """Serves a fixed candle series and counts what it was asked for."""

    def __init__(self, series=None, fail: bool = False):
        self.series = series if series is not None else candles()
        self.calls = 0
        self.fail = fail

    async def pool_ohlcv(self, pool_address, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("indexer down")
        return self.series


def signal(**kw) -> dict:
    row = {
        "id": 1,
        "contract_address": ADDR,
        "symbol": "XYZ",
        "pool_address": POOL,
        "timestamp_ms": T0,
        "price_usd": 1.0,
        "action": "BUY",
        "recorded_horizons": [],
    }
    row.update(kw)
    return row


@pytest.fixture
def tracker() -> RobinhoodHorizonTracker:
    return RobinhoodHorizonTracker(Gecko(), costs=RobinhoodCosts(round_trip_pct=0.0))


# --------------------------------------------------------------------------- #
# The windows themselves
# --------------------------------------------------------------------------- #


def test_the_four_windows_match_the_binance_ones():
    """Two engines reported over different windows cannot be compared at all,
    and 15m is the window the explosion engine makes a claim about."""
    assert [h.name for h in RH_HORIZONS] == ["15m", "1h", "4h", "24h"]


async def test_a_window_that_has_not_elapsed_is_pending_and_never_written(tracker):
    """Invariant 14: reporting the current price for an unfinished window turns
    "not yet" into a result."""
    now = T0 + 10 * 60_000     # ten minutes: even 15m has not closed
    results, report = await tracker.track([signal()], now_ms=now)
    assert results == []
    assert report.pending == len(RH_HORIZONS)
    assert report.resolved == 0


async def test_nothing_elapsed_costs_no_request():
    gecko = Gecko()
    t = RobinhoodHorizonTracker(gecko)
    _, report = await t.track([signal()], now_ms=T0 + 60_000)
    assert gecko.calls == 0 and report.requests_used == 0


async def test_an_elapsed_window_resolves(tracker):
    now = T0 + 30 * 60_000
    results, report = await tracker.track([signal()], now_ms=now)
    fifteen = [r for r in results if r.horizon == "15m"]
    assert len(fifteen) == 1
    assert fifteen[0].status is RobinhoodHorizonStatus.RESOLVED
    assert report.resolved >= 1
    # The later windows have not closed and are absent, not settled.
    assert {r.horizon for r in results} == {"15m"}


async def test_entry_is_the_open_of_the_bar_after_the_decision():
    """The close that produced the decision is not an entry: entering at it is
    look-ahead written into the measurement."""
    series = candles(drift=0.01)
    t = RobinhoodHorizonTracker(Gecko(series), costs=RobinhoodCosts(round_trip_pct=0.0))
    results, _ = await t.track([signal()], now_ms=T0 + 30 * 60_000)
    result = results[0]
    assert result.entry_price == series[1][1]
    assert result.entry_price != series[0][4]


async def test_the_decision_bar_is_matched_exactly_never_by_neighbour():
    """Resolving against the nearest bar would shift every window by up to a
    full bar, always in whichever direction the clock happened to fall."""
    series = candles(start_ms=T0 + 2 * 60_000)   # 2 minutes off the grid
    t = RobinhoodHorizonTracker(Gecko(series))
    results, report = await t.track([signal()], now_ms=T0 + 30 * 60_000)
    assert all(r.status is RobinhoodHorizonStatus.UNRESOLVABLE for r in results)
    assert report.unresolvable >= 1
    assert any("absente de la série" in (r.note or "") for r in results)


async def test_a_decision_with_no_pool_is_unresolvable_with_its_reason():
    """The candle endpoint is keyed on the pool. Skipping silently would leave
    a row that looks pending forever."""
    t = RobinhoodHorizonTracker(Gecko())
    results, report = await t.track(
        [signal(pool_address=None)], now_ms=T0 + 48 * 3600_000
    )
    assert results and all(r.status is RobinhoodHorizonStatus.UNRESOLVABLE for r in results)
    assert all("aucun pool" in (r.note or "") for r in results)
    assert report.unresolvable == len(RH_HORIZONS)


async def test_an_indexer_failure_grades_nothing_rather_than_guessing():
    t = RobinhoodHorizonTracker(Gecko(fail=True))
    results, report = await t.track([signal()], now_ms=T0 + 30 * 60_000)
    assert results == []
    assert report.errors
    assert report.resolved == 0


async def test_windows_already_recorded_are_not_regraded():
    """Invariant 10. Re-grading would silently rewrite history."""
    gecko = Gecko()
    t = RobinhoodHorizonTracker(gecko)
    rows = [signal(recorded_horizons=["15m", "1h", "4h", "24h"])]
    results, report = await t.track(rows, now_ms=T0 + 48 * 3600_000)
    assert results == []
    assert gecko.calls == 0, "une ligne déjà complète ne doit rien coûter"


async def test_costs_are_deducted_and_the_gross_figure_kept():
    t = RobinhoodHorizonTracker(Gecko(candles(drift=0.001)),
                                costs=RobinhoodCosts(round_trip_pct=3.0))
    results, _ = await t.track([signal()], now_ms=T0 + 30 * 60_000)
    r = results[0]
    assert r.change_pct is not None and r.net_change_pct is not None
    assert r.net_change_pct == pytest.approx(r.change_pct - 3.0, abs=1e-6)


async def test_success_is_defined_in_exactly_one_place():
    """Invariant 15: storage and statistics both read `is_success`, so the
    criterion cannot drift between them."""
    t = RobinhoodHorizonTracker(Gecko(candles(drift=0.01)),
                                costs=RobinhoodCosts(round_trip_pct=3.0))
    results, _ = await t.track([signal()], now_ms=T0 + 30 * 60_000)
    r = results[0]
    assert r.is_success is (r.net_change_pct > 0.0)
    assert r.to_row()["success"] == r.is_success


async def test_an_unresolved_window_has_no_verdict_rather_than_a_false_one():
    """"We could not tell" and "it lost" are different facts (invariant 11)."""
    t = RobinhoodHorizonTracker(Gecko())
    results, _ = await t.track([signal(pool_address=None)], now_ms=T0 + 48 * 3600_000)
    assert all(r.is_success is None for r in results)
    assert all(r.to_row()["success"] is None for r in results)


async def test_the_report_says_what_it_could_not_settle(tracker):
    payload = (await tracker.track([signal()], now_ms=T0 + 30 * 60_000))[1].to_dict()
    assert "jamais réglée au prix actuel" in payload["note"]
    assert set(payload) >= {"resolved", "pending", "unresolvable", "requests_used"}


def test_the_cost_model_says_it_is_modelled_not_measured():
    described = RobinhoodCosts().describe()
    assert "hypothèse" in described["note"]
    assert described["round_trip_pct"] > 0


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def row(**kw) -> dict:
    base = {
        "signal_id": 1, "symbol": "XYZ", "contract_address": ADDR,
        "horizon": "15m", "action": "BUY", "strength": "STANDARD",
        "early_score": 80.0, "explosion_score": 85.0, "rug_risk": "LOW",
        "hard_veto": False, "pool_age_seconds": 600.0,
        "change_pct": 5.0, "net_change_pct": 2.0,
        "max_gain_pct": 8.0, "max_drawdown_pct": -2.0, "success": True,
    }
    base.update(kw)
    return base


def test_every_rate_carries_its_count():
    """Invariant 12. A 100 % success rate over three windows is not a finding."""
    perf = build_robinhood_performance([row() for _ in range(3)])
    for table in ("overall", "by_action", "by_early_band", "by_explosion_band"):
        for bucket in perf[table]:
            assert "n" in bucket
            if bucket["success_rate"] is not None:
                assert bucket["n"] > 0


def test_small_buckets_are_flagged_rather_than_hidden():
    """On a chain this young, insufficient is the ordinary case — hiding it
    would leave a blank screen that reads as breakage."""
    perf = build_robinhood_performance([row() for _ in range(3)])
    assert all(b["insufficient_sample"] for b in perf["overall"] if b["n"])
    assert any("ne doit être lu comme un résultat" in n for n in perf["notes"])


def test_a_large_enough_bucket_is_not_flagged():
    perf = build_robinhood_performance([row() for _ in range(MIN_SAMPLE + 5)])
    fifteen = next(b for b in perf["overall"] if b["horizon"] == "15m")
    assert fifteen["n"] == MIN_SAMPLE + 5
    assert not fifteen["insufficient_sample"]


def test_the_decision_table_is_ordered_buy_then_watch_then_avoid():
    """The comparison this whole table exists for has to read top to bottom."""
    rows = [row(action=a) for a in ("AVOID", "WATCH", "BUY")]
    perf = build_robinhood_performance(rows)
    fifteen = [b["bucket"] for b in perf["by_action"] if b["horizon"] == "15m"]
    assert fifteen == ["BUY", "WATCH", "AVOID"]


def test_a_blocked_explosion_score_is_its_own_band():
    """Zero is a statement — a gate fired — not simply a low score."""
    perf = build_robinhood_performance([row(explosion_score=0.0)])
    assert any(b["bucket"] == "0 (bloqué)" for b in perf["by_explosion_band"])


def test_an_unknown_age_is_dropped_rather_than_bucketed():
    """Invariant 69: filing it under "< 15 min" would put it at the top of the
    list people act on fastest."""
    perf = build_robinhood_performance([row(pool_age_seconds=None)])
    assert perf["by_age_bucket"] == []


def test_the_explosion_claim_horizon_is_pointed_at_explicitly():
    perf = build_robinhood_performance([row()])
    assert perf["explosion_claim_horizon"] == "15m"
    assert any("bulletin du score d'explosion" in n for n in perf["notes"])


def test_the_basis_is_named_so_gross_and_net_cannot_be_confused():
    net = build_robinhood_performance([row()], use_net=True)
    gross = build_robinhood_performance([row()], use_net=False)
    assert net["return_basis"] == "net_change_pct"
    assert gross["return_basis"] == "change_pct"
    assert any("brutes" in n for n in gross["notes"])


def test_an_empty_journal_produces_a_shape_rather_than_an_error():
    """The screen must render on the first day, when nothing has been graded."""
    perf = build_robinhood_performance([])
    assert perf["overall"] and all(b["n"] == 0 for b in perf["overall"])
    assert all(b["success_rate"] is None for b in perf["overall"])


def test_robinhood_statistics_are_never_pooled_with_binance():
    """Two engines, two markets. A combined rate would move when either changed
    and would identify neither."""
    perf = build_robinhood_performance([row()])
    assert perf["market"] == "ROBINHOOD"
    assert perf["chain_id"] == 4663
