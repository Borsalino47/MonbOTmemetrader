#!/usr/bin/env python
"""Pipeline verification harness: scan across simulated time, then grade the journal.

WHAT THIS PROVES: that the full loop works end to end — scan, persist, wait,
resolve, aggregate. Signals are written at one simulated instant and graded later
against the bars that followed, exactly as the live service does.

WHAT THIS DOES NOT PROVE: anything about the strategy. It runs on the SYNTHETIC
fixture provider, so every price is generated. The win rate it prints describes
made-up candles. It is a test of the plumbing, and the output says so.

The only reason this can work offline at all is that the fixture provider is
time-anchored: the candle at a given timestamp is the same whenever you ask, so
a signal recorded at T can genuinely be graded against T+1, T+2, ...

    python scripts/simulate_journal.py --scans 60 --step-bars 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Force the synthetic provider before settings are first read.
os.environ.setdefault("CP_PROVIDER_MARKET_DATA", "fixture")
os.environ.setdefault("CP_SCAN_MIN_QUOTE_VOLUME_24H", "100000")

from cryptopulse.backtest.labels import DEFAULT_LABEL_CONFIGS  # noqa: E402
from cryptopulse.config.settings import CryptoPulseSettings  # noqa: E402
from cryptopulse.core.clock import FrozenClock  # noqa: E402
from cryptopulse.core.logging import configure_logging  # noqa: E402
from cryptopulse.database import repo  # noqa: E402
from cryptopulse.database.session import init_engine, reset_engine  # noqa: E402
from cryptopulse.outcomes.stats import build_performance  # noqa: E402
from cryptopulse.outcomes.tracker import OutcomeTracker  # noqa: E402
from cryptopulse.providers.fixture import FixtureProvider  # noqa: E402
from cryptopulse.scanner.cex import CexScanner  # noqa: E402
from cryptopulse.scanner.memory import ScoreMemory  # noqa: E402

START_MS = 1_780_000_000_000  # fixed simulated start, so runs are comparable


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="simulate a signal journal and grade it")
    parser.add_argument("--scans", type=int, default=60, help="number of simulated scan passes")
    parser.add_argument("--step-bars", type=int, default=3, help="primary-timeframe bars between scans")
    parser.add_argument("--label", type=str, default="standard_2R")
    parser.add_argument("--db", type=str, default="sqlite:///data/simulation.db")
    # The ×10 axis needs its own simulated timeline: a 30-day horizon cannot be
    # graded inside the 15 hours the intraday pass covers.
    parser.add_argument("--moon-scans", type=int, default=40, dest="moon_scans",
                        help="simulated scan passes for the ×10 axis (0 to skip)")
    parser.add_argument("--moon-step-days", type=int, default=3, dest="moon_step_days",
                        help="days between ×10 passes")
    args = parser.parse_args(argv)

    configure_logging("WARNING", False)

    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.database.url = args.db
    settings.scanner.min_quote_volume_24h = 100_000.0

    db_path = args.db.replace("sqlite:///", "")
    if db_path and db_path != ":memory:" and Path(db_path).exists():
        Path(db_path).unlink()
    reset_engine()
    init_engine(settings.database)

    tf_ms = settings.scanner.primary_timeframe.ms
    clock = FrozenClock(START_MS)
    provider = FixtureProvider(clock=clock)
    scanner = CexScanner(settings, provider=provider, memory=ScoreMemory(), clock=clock)

    print("=" * 78)
    print("CRYPTO PULSE AI — journal simulation")
    print("=" * 78)
    print(f"provider   : {provider.name}  (SYNTHETIC — generated candles, not a market)")
    print(f"scans      : {args.scans} passes, {args.step_bars} x {settings.scanner.primary_timeframe.value} apart")
    print(f"database   : {settings.database.url}")
    print()

    written_total = 0
    for i in range(args.scans):
        report = await scanner.scan()
        written = repo.persist_scan(report, provider=provider.name, regime=scanner.regime.trend.value)
        written_total += written
        if (i + 1) % 10 == 0 or i == 0:
            print(f"  scan {i + 1:>3}/{args.scans}  ok={report.succeeded:>2}  "
                  f"failed={report.failed}  signals+={written:>2}  total={written_total}")
        clock.advance(args.step_bars * tf_ms / 1000)

    counts = repo.outcome_counts()
    print(f"\nJournal built: {counts['total_signals']} signals, all pending "
          f"(win rate is null by construction at this point).")

    # Let time pass so the horizons elapse, then grade.
    label = next((c for c in DEFAULT_LABEL_CONFIGS if c.name == args.label), DEFAULT_LABEL_CONFIGS[1])
    clock.advance((label.horizon_bars + 5) * tf_ms / 1000)
    tracker = OutcomeTracker(settings, provider, label_config=label, clock=clock)

    print(f"\nAdvanced {label.horizon_bars + 5} bars. Grading with '{label.name}'...")
    print(f"  {label.describe()}\n")

    pending = repo.pending_signals(tracker.ready_before_ms(), limit=5000)
    print(f"  {len(pending)} signal(s) old enough to grade")
    resolution = await tracker.resolve(pending)
    saved = repo.save_resolutions(resolution.resolutions)
    print(f"  resolved={resolution.resolved}  still_pending={resolution.still_pending}  "
          f"unresolvable={resolution.unresolvable}  saved={saved}")
    if resolution.by_label:
        print("  by label: " + ", ".join(f"{k}={v}" for k, v in sorted(resolution.by_label.items())))

    stats = repo.signal_stats()
    print(f"\n{'-' * 78}\nSIGNAL STATS\n{'-' * 78}")
    print(f"  total={stats['total_signals']}  settled={stats['settled']}  "
          f"pending={stats['pending_evaluation']}  unresolvable={stats['unresolvable']}")
    print(f"  win_rate = {stats['win_rate']}")
    print(f"  {stats['win_rate_note']}")

    rows = repo.resolved_signals()
    if not rows:
        print("\nNo settled signals — nothing to aggregate.")
        await scanner.close()
        return 0

    perf = build_performance(rows).to_dict()
    o = perf["overall"]
    print(f"\n{'-' * 78}\nREALISED PERFORMANCE (net of modelled costs)\n{'-' * 78}")
    print(f"  n={o['n']}  wins={o['wins']}  losses={o['losses']}  timeouts={o['timeouts']}")
    print(f"  win rate       {_pct(o['win_rate'])}")
    print(f"  expectancy     {_num(o['expectancy_pct'])}% per signal")
    print(f"  avg win/loss   {_num(o['avg_win_pct'])}% / {_num(o['avg_loss_pct'])}%")
    print(f"  profit factor  {_num(o['profit_factor'])}")
    print(f"  avg MFE/MAE    {_num(o['avg_mfe_atr'])} / {_num(o['avg_mae_atr'])} ATR")

    if perf["by_score_band"]:
        print(f"\n  {'score band':<12} {'n':>5} {'win rate':>10} {'expectancy':>12}")
        for b in perf["by_score_band"]:
            flag = " *" if b["insufficient_sample"] else ""
            print(f"  {b['key']:<12} {b['n']:>5} {_pct(b['win_rate']):>10} {_num(b['expectancy_pct']):>11}%{flag}")

    if perf["by_state"]:
        print(f"\n  {'setup state':<14} {'n':>5} {'win rate':>10} {'expectancy':>12}")
        for b in perf["by_state"]:
            flag = " *" if b["insufficient_sample"] else ""
            print(f"  {b['key']:<14} {b['n']:>5} {_pct(b['win_rate']):>10} {_num(b['expectancy_pct']):>11}%{flag}")

    if perf["component_edge"]:
        print(f"\n  {'component':<12} {'winners':>9} {'losers':>9} {'edge':>8}")
        for c in perf["component_edge"]:
            flag = " *" if c["insufficient_sample"] else ""
            print(f"  {c['component']:<12} {c['avg_points_winners']:>9.2f} "
                  f"{c['avg_points_losers']:>9.2f} {c['edge']:>+8.2f}{flag}")

    print("\n  (* = sample below the minimum; not a finding)")

    baseline = await _random_baseline(provider, settings, label, clock)
    if baseline:
        print(f"\n{'-' * 78}\nBASELINE — same bars, entries every 5th bar, no scoring at all\n{'-' * 78}")
        print(f"  n={baseline['n']}  win rate {_pct(baseline['win_rate'])}")
        delta = (o["win_rate"] or 0) - baseline["win_rate"]
        verdict = "ABOVE" if delta > 0.02 else "BELOW" if delta < -0.02 else "level with"
        print(f"  scanner {_pct(o['win_rate'])} vs baseline {_pct(baseline['win_rate'])} "
              f"-> scanner is {verdict} random entry ({delta * 100:+.1f} pts)")
        if delta < -0.02:
            print("\n  A score that selects worse than random entry is the single most useful")
            print("  thing this harness can tell you. On synthetic data it says nothing about")
            print("  markets — but run the same comparison on real history and a negative")
            print("  delta means the weights are actively harmful, not merely unvalidated.")

    for note in perf["notes"]:
        print(f"\n  ** {note}")

    if args.moon_scans > 0 and settings.moonshot.enabled:
        await _moonshot_section(settings, args)

    print("\n" + "=" * 78)
    print("REMINDER: synthetic candles. This verifies the pipeline, not the strategy.")
    print("=" * 78)

    await scanner.close()
    return 0


async def _moonshot_section(settings, args) -> None:
    """Build a ×10 journal across months of simulated time, grade it, benchmark it.

    Separate timeline on purpose. The intraday pass above advances a few 5-minute
    bars at a time and covers well under a day; a thesis with a 30-day horizon
    graded on that timeline would be 100% pending, which proves nothing.

    The baseline is the part that matters. A ×2-in-30-days barrier is reached by
    chance often enough that a moonshot win rate means nothing on its own — the
    only informative number is the difference between the scanner's readings and
    entries picked at random from the same daily bars.
    """
    from cryptopulse.backtest.labels import label_config_by_name
    from cryptopulse.outcomes.stats import build_moonshot_performance

    label = label_config_by_name(settings.moonshot.label_config)
    day_ms = 86_400_000
    clock = FrozenClock(START_MS - args.moon_scans * args.moon_step_days * day_ms)
    provider = FixtureProvider(clock=clock)
    scanner = CexScanner(settings, provider=provider, memory=ScoreMemory(), clock=clock)

    print(f"\n{'=' * 78}")
    print(f"×10 AXIS — {label.name}")
    print("=" * 78)
    print(f"building {args.moon_scans} passes {args.moon_step_days} day(s) apart "
          f"({args.moon_scans * args.moon_step_days} days of simulated history)")

    journalled = 0
    for _ in range(args.moon_scans):
        report = await scanner.scan()
        journalled += repo.persist_scan(
            report,
            provider=provider.name,
            regime=scanner.regime.trend.value,
            moonshot_journal_min_score=settings.moonshot.journal_min_score,
        )
        clock.advance(args.moon_step_days * day_ms / 1000)
    await scanner.close()

    counts = repo.moonshot_counts()
    print(f"  {counts['readings_journalled']} ×10 readings journalled "
          f"({counts['candidates_journalled']} at an early stage), {journalled} rows written")

    # Time passes, then grade.
    clock.advance((label.horizon_bars + 3) * day_ms / 1000)
    grader = FixtureProvider(clock=clock)
    tracker = OutcomeTracker(settings, grader, label_config=label, clock=clock)
    pending = repo.pending_moonshot_signals(tracker.ready_before_ms(), 5000)
    report = await tracker.resolve(pending)
    repo.save_moonshot_resolutions(report.resolutions)
    print(f"  graded: resolved={report.resolved} pending={report.still_pending} "
          f"unresolvable={report.unresolvable}")

    rows = repo.resolved_moonshot_signals()
    if not rows:
        print("  nothing settled — nothing to report.")
        return

    perf = build_moonshot_performance(rows).to_dict()
    o = perf["overall"]
    print(f"\n  n={o['n']}  win rate at ×{label.target_multiple:g} {_pct(o['win_rate'])}  "
          f"expectancy {_num(o['expectancy_pct'])}%")
    print(f"  best multiple reached x{_num(perf['best_multiple'])}  "
          f"median x{_num(perf['median_multiple'])}")
    print(f"\n  {'reached':<12} {'n':>5} {'share':>8}")
    for rung in perf["multiple_distribution"]:
        share = "n/a" if rung["share"] is None else f"{rung['share'] * 100:.1f}%"
        print(f"  x{rung['at_least']:<11g} {rung['n']:>5} {share:>8}")

    if perf["by_stage"]:
        print(f"\n  {'stage':<14} {'n':>5} {'win rate':>10} {'expectancy':>12}")
        for b in perf["by_stage"]:
            flag = " *" if b["insufficient_sample"] else ""
            print(f"  {b['key']:<14} {b['n']:>5} {_pct(b['win_rate']):>10} "
                  f"{_num(b['expectancy_pct']):>11}%{flag}")

    baseline = await _random_moonshot_baseline(grader, settings, label)
    if baseline:
        print("\n  BASELINE — same daily bars, entries every 10th bar, no scoring")
        print(f"  n={baseline['n']}  win rate {_pct(baseline['win_rate'])}  "
              f"median multiple x{_num(baseline['median_multiple'])}")
        delta = (o["win_rate"] or 0) - baseline["win_rate"]
        verdict = "ABOVE" if delta > 0.02 else "BELOW" if delta < -0.02 else "level with"
        print(f"  ×10 ranking is {verdict} random daily entry ({delta * 100:+.1f} pts)")
        if delta <= 0.02:
            print("\n  On synthetic candles this says nothing about markets. On real history it")
            print("  would be the whole answer: a ×10 layer that does not beat a random daily")
            print("  entry is a stage machine with extra steps.")

    for note in perf["notes"]:
        print(f"\n  ** {note}")


async def _random_moonshot_baseline(provider, settings, label) -> dict | None:
    """Entries chosen from the same daily bars with no scoring whatsoever."""
    import numpy as np

    from cryptopulse.backtest.labels import Outcome, label_signal
    from cryptopulse.providers.fixture import _UNIVERSE

    tally: dict[str, int] = {}
    multiples: list[float] = []
    for symbol, *_ in _UNIVERSE[:12]:
        try:
            series = (await provider.get_ohlcv(symbol, label.timeframe, 900)).closed()
        except Exception:
            continue
        for i in range(50, len(series) - label.horizon_bars - 2, 10):
            r = label_signal(series.high, series.low, series.close, series.open, i, 0.0, label)
            if r.outcome is Outcome.UNRESOLVED:
                continue
            tally[r.outcome.value] = tally.get(r.outcome.value, 0) + 1
            multiples.append(r.max_multiple)
    n = sum(tally.values())
    if n < 50:
        return None
    return {
        "n": n,
        "win_rate": tally.get("WIN", 0) / n,
        "median_multiple": float(np.median(multiples)) if multiples else None,
        "by_label": tally,
    }


async def _random_baseline(provider, settings, label, clock) -> dict | None:
    """Same bars, same label, entries chosen with no scoring whatsoever.

    Spec rule: always compare a model against a simple baseline. Without this the
    scanner's win rate is a number with nothing to be better or worse than — and
    a 2:1 barrier already implies roughly one third wins on any martingale, which
    is easy to mistake for skill.
    """
    import numpy as np

    from cryptopulse.backtest.labels import Outcome, label_signal
    from cryptopulse.features.indicators import atr as atr_fn

    tally: dict[str, int] = {}
    symbols = [s for s, *_ in __import__("cryptopulse.providers.fixture", fromlist=["_UNIVERSE"])._UNIVERSE][:10]
    for symbol in symbols:
        try:
            series = (await provider.get_ohlcv(symbol, settings.scanner.primary_timeframe, 900)).closed()
        except Exception:
            continue
        a = atr_fn(series.high, series.low, series.close, 14)
        for i in range(200, len(series) - label.horizon_bars - 2, 5):
            if not np.isfinite(a[i]):
                continue
            r = label_signal(series.high, series.low, series.close, series.open, i, float(a[i]), label)
            if r.outcome is Outcome.UNRESOLVED:
                continue
            tally[r.outcome.value] = tally.get(r.outcome.value, 0) + 1
    n = sum(tally.values())
    if n < 50:
        return None
    return {"n": n, "win_rate": tally.get("WIN", 0) / n, "by_label": tally}


def _num(x, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
