"""Command line entry points.

    python -m cryptopulse.cli doctor     # live round-trip against the provider
    python -m cryptopulse.cli scan       # one scan, printed as a table
    python -m cryptopulse.cli serve      # API + dashboard
    python -m cryptopulse.cli backtest   # historical replay
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from cryptopulse.config.settings import get_settings
from cryptopulse.core.clock import SYSTEM_CLOCK
from cryptopulse.core.logging import configure_logging, get_logger

log = get_logger("cli")


# --------------------------------------------------------------------------- #
# doctor — the command that turns IMPLEMENTED into LIVE VERIFIED
# --------------------------------------------------------------------------- #


async def cmd_doctor(args) -> int:
    """Round-trip every endpoint and check the parsed values against themselves.

    This exists because the connector was written without network access. Rather
    than asserting it works, this command proves or disproves it against the real
    API and prints which.
    """
    settings = get_settings()
    from cryptopulse.core.types import Timeframe
    from cryptopulse.providers.registry import build_market_provider

    provider = build_market_provider(settings, SYSTEM_CLOCK)
    checks: list[tuple[str, bool, str]] = []
    print(f"\nCRYPTO PULSE AI — provider doctor\nprovider: {provider.name}\n" + "-" * 68)

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    # 1. connectivity
    try:
        health = await provider.health()
        record("health/ping", health.available, health.detail or f"{health.latency_ms:.0f} ms")
        if not health.available:
            print("\nCannot reach the provider. Everything below is skipped.")
            await provider.close()
            _summarise(checks)
            return 1
    except Exception as exc:
        record("health/ping", False, f"{type(exc).__name__}: {exc}")
        await provider.close()
        _summarise(checks)
        return 1

    # 2. exchangeInfo
    try:
        symbols = await provider.list_symbols(settings.scanner.quote_asset)
        ok = len(symbols) > 50 and all(s.quote == settings.scanner.quote_asset.upper() for s in symbols)
        record("exchangeInfo → symbols", ok, f"{len(symbols)} {settings.scanner.quote_asset} pairs")
    except Exception as exc:
        record("exchangeInfo → symbols", False, f"{type(exc).__name__}: {exc}")

    # 3. tickers
    tickers = {}
    try:
        tickers = await provider.get_tickers_24h(["BTCUSDT", "ETHUSDT"])
        t = tickers.get("BTCUSDT")
        ok = t is not None and t.last_price > 0 and t.quote_volume_24h > 0 and t.low_24h <= t.last_price <= t.high_24h
        detail = (
            f"BTC {t.last_price:,.2f}, 24h range {t.low_24h:,.2f}-{t.high_24h:,.2f}, vol {t.quote_volume_24h:,.0f}"
            if t
            else "no BTCUSDT row"
        )
        record("ticker/24hr consistency (low <= last <= high)", ok, detail)
    except Exception as exc:
        record("ticker/24hr consistency", False, f"{type(exc).__name__}: {exc}")

    # 4. klines — the field-order check that matters most
    try:
        series = await provider.get_ohlcv("BTCUSDT", Timeframe.M5, 120)
        n = len(series)
        checks_ok = True
        details = []

        ordered = bool((series.open_time_ms[1:] > series.open_time_ms[:-1]).all()) if n > 1 else True
        checks_ok &= ordered
        details.append(f"{n} candles, ascending={ordered}")

        # If field indices were wrong, high/low invariants would break immediately.
        invariant = bool(
            (series.high >= series.low).all()
            and (series.high >= series.open).all()
            and (series.high >= series.close).all()
            and (series.low <= series.open).all()
            and (series.low <= series.close).all()
        )
        checks_ok &= invariant
        details.append(f"OHLC invariants={invariant}")

        spacing = bool(((series.open_time_ms[1:] - series.open_time_ms[:-1]) == Timeframe.M5.ms).all()) if n > 1 else True
        details.append(f"5m spacing={spacing}")

        record("klines field mapping + invariants", checks_ok, "; ".join(details))

        # 5. the open-candle rule
        closed = series.closed()
        dropped = len(series) - len(closed)
        now = SYSTEM_CLOCK.now_ms()
        ok = closed.last_close_time_ms <= now
        record(
            "in-progress candle excluded",
            ok,
            f"dropped {dropped} forming candle(s); newest closed bar ended "
            f"{(now - closed.last_close_time_ms) / 1000:.0f}s ago",
        )

        # 6. cross-check klines against the ticker
        if tickers.get("BTCUSDT"):
            last = tickers["BTCUSDT"].last_price
            kline_close = closed.last_close
            drift = abs(kline_close - last) / last * 100
            ok = drift < 2.0
            record("kline close agrees with ticker price", ok, f"{drift:.3f}% apart")
    except Exception as exc:
        record("klines", False, f"{type(exc).__name__}: {exc}")

    # 7. order book
    try:
        book = await provider.get_order_book("BTCUSDT", 50)
        ok = (
            book.best_bid is not None
            and book.best_ask is not None
            and book.best_bid < book.best_ask
            and book.spread_bps is not None
            and book.spread_bps >= 0
        )
        record("depth → bid < ask, spread >= 0", ok, f"spread {book.spread_bps:.2f} bps, imbalance {book.imbalance():+.3f}")
    except Exception as exc:
        record("depth", False, f"{type(exc).__name__}: {exc}")

    await provider.close()
    return _summarise(checks)


def _summarise(checks) -> int:
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    print("-" * 68)
    if passed == total:
        print(f"LIVE VERIFIED — {passed}/{total} checks passed against the real API.\n")
        return 0
    print(f"NOT VERIFIED — {passed}/{total} checks passed. Fix the failures above before trusting this feed.\n")
    return 1


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #


async def cmd_scan(args) -> int:
    settings = get_settings()
    from cryptopulse.alerts.engine import AlertEngine
    from cryptopulse.database import repo
    from cryptopulse.database.session import init_engine
    from cryptopulse.scanner.cex import CexScanner

    init_engine(settings.database)
    scanner = CexScanner(settings)
    report = await scanner.scan()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
        await scanner.close()
        return 0

    print(f"\n{'=' * 132}")
    print(f"CRYPTO PULSE AI — LIVE MARKET SCANNER   [{settings.scoring.engine_version}]")
    print(f"provider={scanner.provider.name}  universe={report.universe_size}  scanned={report.scanned}  "
          f"ok={report.succeeded}  failed={report.failed}  {report.duration_ms}ms  regime={scanner.regime.trend.value}")
    for note in report.notes:
        print(f"  ** {note}")
    print("=" * 132)
    header = (
        f"{'#':>3} {'ASSET':<12} {'PRICE':>13} {'5m%':>7} {'RVOL':>6} {'OPP':>6} {'RAW':>6} {'PEN':>6} "
        f"{'ACCEL':>6} {'MATUR':>6} {'SAFE':>5} {'CONF':>5} {'LIQ':<10} {'STATE':<13}"
    )
    print(header)
    print("-" * 132)
    for i, r in enumerate(report.results[: args.limit], 1):
        f = r.features.primary if r.features else None
        print(
            f"{i:>3} {r.symbol:<12} {r.price:>13.8g} "
            f"{(f.roc1 if f and f.roc1 is not None else 0):>7.2f} "
            f"{(f.rvol if f and f.rvol is not None else 0):>6.2f} "
            f"{r.final_score:>6.1f} {r.raw_score:>6.1f} {-r.risk_penalty:>6.1f} "
            f"{r.acceleration.momentum_acceleration:>6.1f} {r.maturity.score:>6.1f} "
            f"{r.safety.score:>5.0f} {r.confidence.score:>5.0f} "
            f"{r.liquidity.status.value:<10} {r.state.state.value:<13}"
        )

    if report.errors:
        print(f"\n{len(report.errors)} asset(s) failed:")
        for sym, err in list(report.errors.items())[:10]:
            print(f"  {sym}: {err}")

    engine = AlertEngine(settings.alerts, settings.scoring)
    alerts = engine.evaluate(report.results, SYSTEM_CLOCK.now_ms())
    if alerts:
        print(f"\n{'=' * 60}\nALERTS ({len(alerts)})\n{'=' * 60}")
        for a in alerts[:5]:
            print("\n" + a.format_text())
    else:
        print("\nNo alerts fired (thresholds or gates not met).")

    written = repo.persist_scan(report, provider=scanner.provider.name, regime=scanner.regime.trend.value)
    repo.persist_alerts(alerts)
    print(f"\nPersisted {written} signal rows to {settings.database.url}")

    await scanner.close()
    return 0


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #


async def cmd_backtest(args) -> int:
    settings = get_settings()
    from cryptopulse.backtest.engine import BacktestConfig, BacktestEngine
    from cryptopulse.backtest.labels import DEFAULT_LABEL_CONFIGS
    from cryptopulse.core.types import Timeframe
    from cryptopulse.providers.registry import build_market_provider, is_synthetic

    provider = build_market_provider(settings, SYSTEM_CLOCK)
    symbols = args.symbols.split(",") if args.symbols else settings.scanner.always_include[:5]
    timeframes = [settings.scanner.primary_timeframe, Timeframe.H1, Timeframe.H4]

    series_by_symbol: dict[str, dict] = {}
    for symbol in symbols:
        by_tf = {}
        for tf in timeframes:
            try:
                by_tf[tf] = (await provider.get_ohlcv(symbol, tf, args.bars)).closed()
            except Exception as exc:
                print(f"  {symbol} {tf.value}: {type(exc).__name__}: {exc}")
        if settings.scanner.primary_timeframe in by_tf:
            series_by_symbol[symbol] = by_tf

    if not series_by_symbol:
        print("No usable history fetched — cannot backtest.")
        await provider.close()
        return 1

    synthetic = is_synthetic(provider)
    engine = BacktestEngine(settings, BacktestConfig(min_score=args.min_score))
    label = next((c for c in DEFAULT_LABEL_CONFIGS if c.name == args.label), DEFAULT_LABEL_CONFIGS[1])

    result = engine.run(
        series_by_symbol,
        primary_tf=settings.scanner.primary_timeframe,
        label_config=label,
        data_source=provider.name,
        synthetic=synthetic,
    )
    await provider.close()

    print(f"\n{'=' * 76}\nBACKTEST — {label.name}\n{'=' * 76}")
    print(f"Definition: {label.describe()}\n")
    if synthetic:
        print("*** SYNTHETIC DATA — this measures the pipeline, NOT the strategy. ***\n")
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0


# --------------------------------------------------------------------------- #
# resolve — grade emitted signals against what actually happened
# --------------------------------------------------------------------------- #


async def cmd_resolve(args) -> int:
    settings = get_settings()
    from cryptopulse.backtest.labels import DEFAULT_LABEL_CONFIGS
    from cryptopulse.database import repo
    from cryptopulse.database.session import init_engine
    from cryptopulse.outcomes.stats import build_performance
    from cryptopulse.outcomes.tracker import OutcomeTracker
    from cryptopulse.providers.registry import build_market_provider

    init_engine(settings.database)
    provider = build_market_provider(settings, SYSTEM_CLOCK)
    label = next((c for c in DEFAULT_LABEL_CONFIGS if c.name == args.label), DEFAULT_LABEL_CONFIGS[1])
    tracker = OutcomeTracker(settings, provider, label_config=label, clock=SYSTEM_CLOCK)

    print(f"\n{'=' * 76}\nOUTCOME TRACKER — {label.name}\n{'=' * 76}")
    print(f"Definition: {label.describe()}\n")

    pending = repo.pending_signals(tracker.ready_before_ms(), args.limit)
    print(f"{len(pending)} signal(s) old enough to grade.")
    if not pending:
        counts = repo.outcome_counts()
        print(f"Journal: {counts['total_signals']} signals, {counts['pending_evaluation']} awaiting their horizon.")
        await provider.close()
        return 0

    report = await tracker.resolve(pending)
    written = repo.save_resolutions(report.resolutions)
    await provider.close()

    print(f"\nresolved={report.resolved}  still_pending={report.still_pending}  "
          f"unresolvable={report.unresolvable}  written={written}  ({report.duration_ms}ms)")
    if report.by_label:
        print("by label: " + ", ".join(f"{k}={v}" for k, v in sorted(report.by_label.items())))
    if report.errors:
        print(f"\n{len(report.errors)} symbol(s) failed:")
        for sym, err in list(report.errors.items())[:8]:
            print(f"  {sym}: {err}")

    counts = repo.outcome_counts()
    print(f"\nJournal: {counts['total_signals']} signals · {counts['settled']} settled · "
          f"{counts['pending_evaluation']} pending · {counts['unresolvable']} unresolvable")

    rows = repo.resolved_signals()
    if rows:
        perf = build_performance(rows).to_dict()
        o = perf["overall"]
        print(f"\n{'-' * 76}\nREALISED PERFORMANCE (net of costs)\n{'-' * 76}")
        print(f"  n={o['n']}  wins={o['wins']}  losses={o['losses']}  timeouts={o['timeouts']}")
        print(f"  win rate      {_fmt_pct(o['win_rate'])}")
        print(f"  expectancy    {_fmt(o['expectancy_pct'])}% per signal")
        print(f"  profit factor {_fmt(o['profit_factor'])}")
        print(f"  avg MFE/MAE   {_fmt(o['avg_mfe_atr'])} / {_fmt(o['avg_mae_atr'])} ATR")
        if o["insufficient_sample"]:
            print(f"\n  ** Only {o['n']} settled signals (minimum {perf['min_sample']}). "
                  "Not a finding — a smoke test. **")
        for note in perf["notes"]:
            print(f"  ** {note}")

        if perf["component_edge"]:
            print(f"\n{'-' * 76}\nCOMPONENT EDGE — avg points, winners vs losers\n{'-' * 76}")
            for c in perf["component_edge"]:
                flag = "  (sample too small)" if c["insufficient_sample"] else ""
                print(f"  {c['component']:<12} winners {c['avg_points_winners']:>6.2f}   "
                      f"losers {c['avg_points_losers']:>6.2f}   edge {c['edge']:>+6.2f}{flag}")
    return 0


def _fmt(x, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _fmt_pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #


def cmd_serve(args) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "cryptopulse.api.app:app",
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )
    return 0


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cryptopulse", description="CRYPTO PULSE AI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="verify the configured provider against its live API")

    p_scan = sub.add_parser("scan", help="run one scan and print the ranking")
    p_scan.add_argument("--limit", type=int, default=30)
    p_scan.add_argument("--json", action="store_true")

    p_bt = sub.add_parser("backtest", help="replay the scorer over history")
    p_bt.add_argument("--symbols", type=str, default=None, help="comma-separated, default: config majors")
    p_bt.add_argument("--bars", type=int, default=1000)
    p_bt.add_argument("--min-score", type=float, default=65.0, dest="min_score")
    p_bt.add_argument("--label", type=str, default="standard_2R")

    p_res = sub.add_parser("resolve", help="grade emitted signals against what actually happened")
    p_res.add_argument("--limit", type=int, default=500)
    p_res.add_argument("--label", type=str, default="standard_2R")

    p_serve = sub.add_parser("serve", help="run the API and dashboard")
    p_serve.add_argument("--host", type=str, default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--reload", action="store_true")

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    if args.command == "serve":
        return cmd_serve(args)
    handler = {"doctor": cmd_doctor, "scan": cmd_scan, "backtest": cmd_backtest, "resolve": cmd_resolve}[args.command]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main())
