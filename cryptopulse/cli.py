"""Command line entry points.

    python -m cryptopulse.cli doctor     # live round-trip against the provider
    python -m cryptopulse.cli scan       # one scan, printed as a table
    python -m cryptopulse.cli radar      # autonomous loop: scan, alert, notify
    python -m cryptopulse.cli universe   # what the Robinhood filter resolves to
    python -m cryptopulse.cli serve      # API + dashboard
    python -m cryptopulse.cli backtest   # historical replay
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
from datetime import UTC, datetime

from cryptopulse.config.settings import get_settings
from cryptopulse.core.clock import SYSTEM_CLOCK
from cryptopulse.core.logging import configure_logging, get_logger

log = get_logger("cli")

PROVIDERS = ("binance", "kraken", "fixture")
UNIVERSES = ("volume", "robinhood")
RANK_MODES = ("setup", "moonshot", "blend")
VALUATIONS = ("none", "coingecko")


def _settings_with_provider(args):
    """Settings with the run's flags applied, so switching mode is one word.

    Flags override the corresponding CP_* variables for this invocation only;
    nothing is written to disk.
    """
    settings = get_settings()
    for attr, target in (
        ("provider", ("providers", "market_data")),
        ("valuation", ("providers", "valuation")),
        ("universe", ("scanner", "universe")),
        ("rank", ("scanner", "rank_mode")),
    ):
        choice = getattr(args, attr, None)
        if choice:
            section, field = target
            setattr(getattr(settings, section), field, choice)
    interval = getattr(args, "interval", None)
    if interval:
        settings.scanner.scan_interval_seconds = interval
    return settings


def _add_provider_flag(parser) -> None:
    parser.add_argument(
        "--provider", choices=PROVIDERS, default=None,
        help="market data source for this run (default: CP_PROVIDER_MARKET_DATA)",
    )


def _add_radar_flags(parser) -> None:
    parser.add_argument(
        "--universe", choices=UNIVERSES, default=None,
        help="robinhood = only assets believed tradable on Robinhood (default: CP_SCAN_UNIVERSE)",
    )
    parser.add_argument(
        "--rank", choices=RANK_MODES, default=None,
        help="how to order the table (default: CP_SCAN_RANK_MODE)",
    )
    parser.add_argument(
        "--valuation", choices=VALUATIONS, default=None,
        help="market cap source; without one the ×10 capacity reading stays unknown",
    )


# --------------------------------------------------------------------------- #
# doctor — the command that turns IMPLEMENTED into LIVE VERIFIED
# --------------------------------------------------------------------------- #


async def cmd_doctor(args) -> int:
    """Round-trip every endpoint and check the parsed values against themselves.

    This exists because the connector was written without network access. Rather
    than asserting it works, this command proves or disproves it against the real
    API and prints which.
    """
    settings = _settings_with_provider(args)
    from cryptopulse.core.types import Timeframe
    from cryptopulse.providers.registry import build_market_provider

    # No cache: this command exists to prove the live API behaves as documented,
    # and a cached answer would prove only that the cache works.
    provider = build_market_provider(settings, SYSTEM_CLOCK, use_cache=False)
    # Venues disagree on naming — BTCUSDT on Binance, XBTUSDT on Kraken — so the
    # provider names the symbol it is certain to have rather than the CLI guessing.
    ref = provider.reference_symbol
    checks: list[tuple[str, bool, str]] = []
    print(f"\nCRYPTO PULSE AI — provider doctor\nprovider: {provider.name}  reference: {ref}\n" + "-" * 68)

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    # 1. connectivity
    unreachable = False
    try:
        health = await provider.health()
        record("health/ping", health.available, health.detail or f"{health.latency_ms:.0f} ms")
        unreachable = not health.available
    except Exception as exc:
        record("health/ping", False, f"{type(exc).__name__}: {exc}")
        unreachable = True

    if unreachable:
        # A bare "403 Forbidden" tells the user nothing actionable, and the
        # difference between "your sandbox blocks this host", "your region is
        # geo-blocked" and "your DNS is broken" changes what they must do next.
        base = (
            settings.providers.kraken_base_url
            if settings.providers.market_data == "kraken"
            else settings.providers.binance_base_url
        )
        host = base.split("://", 1)[-1].split("/")[0]
        for line in await _diagnose_egress(host):
            print(line)
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
        tickers = await provider.get_tickers_24h([ref])
        t = tickers.get(ref)
        ok = t is not None and t.last_price > 0 and t.quote_volume_24h > 0 and t.low_24h <= t.last_price <= t.high_24h
        detail = (
            f"BTC {t.last_price:,.2f}, 24h range {t.low_24h:,.2f}-{t.high_24h:,.2f}, vol {t.quote_volume_24h:,.0f}"
            if t
            else f"no {ref} row"
        )
        record("ticker/24hr consistency (low <= last <= high)", ok, detail)
    except Exception as exc:
        record("ticker/24hr consistency", False, f"{type(exc).__name__}: {exc}")

    # 4. klines — the field-order check that matters most
    try:
        series = await provider.get_ohlcv(ref, Timeframe.M5, 120)
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
        if tickers.get(ref):
            last = tickers[ref].last_price
            kline_close = closed.last_close
            drift = abs(kline_close - last) / last * 100
            ok = drift < 2.0
            record("kline close agrees with ticker price", ok, f"{drift:.3f}% apart")
    except Exception as exc:
        record("klines", False, f"{type(exc).__name__}: {exc}")

    # 7. order book
    try:
        book = await provider.get_order_book(ref, 50)
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

    # 8. valuation source, when one is configured
    if settings.providers.valuation != "none":
        from cryptopulse.providers.registry import build_valuation_provider

        valuation = build_valuation_provider(settings, SYSTEM_CLOCK)
        try:
            base = provider.reference_symbol.replace(settings.scanner.quote_asset, "") or "BTC"
            from cryptopulse.universe.symbols import canonical_base

            vals = await valuation.get_valuations([canonical_base(base)])
            v = vals.get(canonical_base(base))
            # Bitcoin's cap must be the largest in the ranking and its rank 1. If
            # a ticker collision had handed us the wrong asset, this is where it
            # shows up rather than three weeks later in a score.
            ok = v is not None and (v.market_cap_usd or 0) > 1e9 and v.rank == 1
            detail = (
                f"{v.symbol} cap {v.market_cap_usd:,.0f} USD, rank {v.rank}"
                if v and v.market_cap_usd
                else "no market cap returned for the reference asset"
            )
            record("valuation → reference asset cap + rank", ok, detail)
        except Exception as exc:
            record("valuation", False, f"{type(exc).__name__}: {exc}")
        finally:
            if valuation is not None:
                await valuation.close()

    await provider.close()
    return _summarise(checks)


def _hosts_to_allow(host: str) -> list[str]:
    """Every host the chosen venue needs, so one trip through the settings is enough.

    Binance has a market-data mirror the connector fails over to automatically;
    listing only the primary would make the fallback path fail on its own.
    """
    if "binance" in host:
        return ["api.binance.com", "data-api.binance.vision"]
    return [host]


async def _diagnose_egress(host: str) -> list[str]:
    """Work out *why* the provider is unreachable, and say what to do about it.

    "403 Forbidden" is not an answer a user can act on. Four very different
    faults produce a failed ping, and each has a different fix:

      * DNS does not resolve            -> resolver / container networking
      * a sandbox egress allowlist      -> add the host to the environment
      * the venue geo-blocks the IP     -> different region, or Binance.US
      * traffic dies with no response   -> firewall or no route

    The distinguishing test is a request that bypasses any local proxy: if the
    host answers at all, the network works and something is choosing to refuse.
    Sandbox gateways say so in an `x-deny-reason` header; venues do not.
    """
    import os
    import socket

    import httpx

    out: list[str] = ["", "-" * 68, "NETWORK DIAGNOSIS", "-" * 68]

    # 1. Name resolution.
    try:
        await asyncio.to_thread(socket.getaddrinfo, host, 443)
        out.append(f"  DNS            resolves {host}")
    except OSError as exc:
        out += [
            f"  DNS            FAILS for {host}: {exc}",
            "",
            "  The host does not resolve, so nothing else can be diagnosed.",
            "  Fix the container's resolver before looking at anything else.",
        ]
        return out

    # 2. A request that ignores HTTPS_PROXY. `trust_env=False` is the point:
    #    it separates "the network refuses" from "the local proxy refuses".
    status: int | None = None
    deny_reason: str | None = None
    body = ""
    transport_error: str | None = None

    # `trust_env=False` drops the proxy vars — which is the point — but it also
    # drops SSL_CERT_FILE / REQUESTS_CA_BUNDLE, so a TLS-terminating gateway
    # would fail verification and be misread as "no route". Carry the CA bundle
    # across explicitly.
    ca_bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    verify = ca_bundle if (ca_bundle and os.path.exists(ca_bundle)) else True

    try:
        async with httpx.AsyncClient(trust_env=False, timeout=12.0, verify=verify) as client:
            resp = await client.get(f"https://{host}/api/v3/ping")
        status = resp.status_code
        deny_reason = resp.headers.get("x-deny-reason")
        body = resp.text[:200]
    except Exception as exc:
        transport_error = f"{type(exc).__name__}: {exc}"

    if transport_error is not None:
        tls_intercepted = "CERTIFICATE_VERIFY_FAILED" in transport_error or "self-signed" in transport_error
        out.append(f"  direct request FAILS: {transport_error}")
        if tls_intercepted:
            out += [
                "",
                "  TLS verification failed against a self-signed chain, which means a",
                "  proxy is intercepting and re-terminating TLS. So there IS a gateway",
                "  in front of this host, and the refusal is almost certainly its egress",
                "  policy rather than a network fault.",
                "",
                "  Point the CA bundle at the gateway's certificate and re-run, e.g.",
                "  SSL_CERT_FILE=/path/to/ca-bundle.crt, then follow whatever host",
                "  allowlist that gateway enforces.",
            ]
        else:
            out += [
                "",
                "  DNS resolves but no response came back at all. That is a firewall",
                "  or a missing route rather than a policy refusal — a policy would",
                "  answer with a status code. Check egress firewall rules.",
            ]
        return out

    out.append(f"  direct request HTTP {status}" + (f"  x-deny-reason: {deny_reason}" if deny_reason else ""))

    if status == 200:
        out += [
            "",
            "  The host is reachable when the local proxy is bypassed, so the",
            "  network is fine and the failure is in the proxy configuration.",
            "  Check HTTPS_PROXY and whether that proxy allows this host.",
        ]
        return out

    if deny_reason or "allowlist" in body.lower():
        out += [
            "",
            "  A sandbox egress gateway refused this host — the request never",
            "  reached the exchange. This is an environment setting, not a fault",
            "  in the connector and not a problem with Binance.",
            "",
            "  If you are on Claude Code on the web: open claude.ai/code, click the",
            "  cloud icon above the message box, hover your environment and open its",
            "  settings, set Network access to Custom, and add these to",
            "  Allowed domains (keep 'Also include default list of common package",
            "  managers' checked so pip and npm keep working):",
            "",
        ] + [f"      {h}" for h in _hosts_to_allow(host)] + [
            "",
            "  Then start a NEW session — a running session keeps the policy it",
            "  started with. Full reference: code.claude.com/docs/en/cloud-environments",
        ]
        return out

    if status in (403, 451):
        out += [
            "",
            f"  The venue itself answered {status} with no sandbox deny header, which",
            "  usually means the source IP's region is restricted. Run from a",
            "  permitted region, or point the connector at the venue for your",
            "  jurisdiction (US users: api.binance.us, a different API contract",
            "  this connector does not implement).",
        ]
        return out

    out += ["", f"  Unexpected status {status}. Body: {body[:120]}"]
    return out


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
    settings = _settings_with_provider(args)
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
    print(f"provider={scanner.provider.name}  universe={settings.scanner.universe}/{report.universe_size}  "
          f"scanned={report.scanned}  ok={report.succeeded}  failed={report.failed}  {report.duration_ms}ms  "
          f"regime={scanner.regime.trend.value}  rank={settings.scanner.rank_mode}")
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

    _print_moonshot_block(report, settings)

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


def _print_moonshot_block(report, settings) -> None:
    """The ×10 view: ranked by the daily reading, not by today's move.

    Printed as its own block rather than as extra columns, because it answers a
    different question on a different horizon and mixing the two in one table is
    how a user ends up buying an exhausted pump believing it was a base.
    """
    if not settings.moonshot.enabled:
        return
    rows = [r for r in report.results if r.moonshot is not None and r.moonshot.stage.value != "UNKNOWN"]
    rows.sort(key=lambda r: r.moonshot.score, reverse=True)
    target = settings.moonshot.target_multiple

    print(f"\n{'=' * 132}")
    print(f"×{target:.0f} RADAR — resemblance to a pre-expansion state   "
          f"[{settings.moonshot.timeframe.value} · MOONSHOT_ENGINE_V1 · ranking, not a forecast]")
    print("=" * 132)
    if not rows:
        unknown = sum(1 for r in report.results if r.moonshot and r.moonshot.stage.value == "UNKNOWN")
        print(f"  No asset produced a reading ({unknown} lacked the history needed). "
              f"Increase CP_MOON_CANDLES or check the {settings.moonshot.timeframe.value} feed.")
        return

    print(f"{'#':>3} {'ASSET':<12} {'MOON':>6} {'IGNIT':>6} {'HEAD':>6} {'CAP':>6} {'x→HIGH':>8} "
          f"{'MATUR':>6} {'STAGE':<13} WHY")
    print("-" * 132)
    for i, r in enumerate(rows[:10], 1):
        m = r.moonshot
        cap = "  n/a" if m.capacity is None else f"{m.capacity:6.1f}"
        head = "  n/a" if m.headroom is None else f"{m.headroom:6.1f}"
        ign = "  n/a" if m.ignition is None else f"{m.ignition:6.1f}"
        mult = "     n/a" if m.multiple_to_window_high is None else f"{m.multiple_to_window_high:7.1f}x"
        why = m.reasons[0] if m.reasons else (m.caveats[0] if m.caveats else "")
        print(f"{i:>3} {r.symbol:<12} {m.score:>6.1f} {ign} {head} {cap} {mult} "
              f"{r.maturity.score:>6.1f} {m.stage.value:<13} {why[:52]}")

    best = rows[0].moonshot
    if best.unknowns:
        print(f"\n  Not measured for {rows[0].symbol}: {best.unknowns[0]}")
    print("\n  A high score means 'looks like assets have looked before large expansions'. Most will not do it.")


# --------------------------------------------------------------------------- #
# radar — the autonomous loop
# --------------------------------------------------------------------------- #


async def cmd_radar(args) -> int:
    """Scan, alert and notify on a loop until stopped. No dashboard required.

    Three properties make this safe to leave running:

    * **it survives anything a scan can throw** — one failed cycle backs off and
      the next one runs;
    * **it says where alerts go before it starts**, so a misconfigured channel is
      discovered at 09:00 rather than at 03:14;
    * **it stops cleanly** on SIGINT/SIGTERM, closing the provider and flushing
      the database rather than being killed mid-write.
    """
    settings = _settings_with_provider(args)
    from cryptopulse.api.service import ScannerService
    from cryptopulse.providers.registry import is_synthetic

    service = ScannerService(settings)
    scanner = service.scanner
    interval = settings.scanner.scan_interval_seconds

    print(f"\n{'=' * 96}")
    print(f"CRYPTO PULSE AI — RADAR   [{settings.scoring.engine_version}]")
    print(f"{'=' * 96}")
    print(f"  provider        {scanner.provider.name}"
          f"{'   *** SYNTHETIC — NOT MARKET DATA ***' if is_synthetic(scanner.provider) else ''}")
    print(f"  universe        {settings.scanner.universe}   quote={settings.scanner.quote_asset}   "
          f"benchmark={scanner.benchmark_symbol}")
    print(f"  ranking         {settings.scanner.rank_mode}")
    print(f"  moonshot        enabled={settings.moonshot.enabled}  timeframe="
          f"{settings.moonshot.timeframe.value}  target=×{settings.moonshot.target_multiple:.0f}  "
          f"valuation={settings.providers.valuation}")
    print(f"  interval        {interval}s")
    for ch in service.notifiers.describe():
        status = "ready" if ch["configured"] else f"INERT — set {ch['missing_setting']}"
        print(f"  alert channel   {ch['channel']:<10} {status}")
    if not service.notifiers.describe():
        print("  alert channel   NONE — set CP_ALERT_CHANNELS or alerts go nowhere")
    print(f"{'=' * 96}\n")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no add_signal_handler
            loop.add_signal_handler(sig, stop.set)

    cycles = 0
    failures = 0
    try:
        while not stop.is_set():
            cycles += 1
            try:
                report = await service.run_once()
                failures = 0
                _print_heartbeat(service, report, cycles)
            except Exception as exc:
                failures += 1
                log.error("radar_cycle_failed", cycle=cycles, error=str(exc)[:300], consecutive=failures)
                print(f"[{_now()}] cycle {cycles} FAILED: {type(exc).__name__}: {str(exc)[:160]}")

            if args.once or stop.is_set():
                break

            # Back off when the feed is down: hammering a dead provider wastes the
            # rate-limit budget that will be needed the moment it recovers.
            delay = interval if failures == 0 else min(interval * (2**failures), 900)
            if failures:
                print(f"[{_now()}] backing off {delay}s after {failures} consecutive failure(s)")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
    finally:
        print(f"\n[{_now()}] stopping — {cycles} cycle(s) completed")
        await service.stop()
    return 0


def _now() -> str:
    return datetime.now(tz=UTC).strftime("%H:%M:%S")


def _print_heartbeat(service, report, cycle: int) -> None:
    """One line per cycle, plus the rows that actually matter."""
    alerts = service.last_alerts
    candidates = [r for r in report.results if r.moonshot and r.moonshot.is_candidate]
    print(
        f"[{_now()}] cycle {cycle:<5} scanned={report.scanned:<4} ok={report.succeeded:<4} "
        f"failed={report.failed:<3} {report.duration_ms}ms  alerts={len(alerts)}  "
        f"moonshot_candidates={len(candidates)}"
    )
    for r in report.results[:5]:
        m = r.moonshot
        moon = f"moon={m.score:5.1f} {m.stage.value:<12}" if m else "moon=  n/a"
        print(f"        {r.symbol:<12} opp={r.final_score:5.1f}  {moon}  {r.state.state.value}")
    for a in alerts:
        print(f"    >>> ALERT [{a.kind.value}/{a.level.value}] {a.symbol}: {a.headline}")
    if service.notifiers.last_results:
        summary = ", ".join(
            f"{d.channel}:{d.delivered}/{d.delivered + d.failed}" for d in service.notifiers.last_results
        )
        print(f"        delivery {summary}")


# --------------------------------------------------------------------------- #
# universe — what the Robinhood filter actually resolves to
# --------------------------------------------------------------------------- #


async def cmd_universe(args) -> int:
    """Show the Robinhood universe, and optionally refresh it from Robinhood.

    Worth running before trusting a radar session: it prints exactly which
    listed assets this venue carries and which it does not, so a silent gap in
    coverage becomes a visible one.
    """
    settings = _settings_with_provider(args)
    from cryptopulse.providers.registry import build_market_provider
    from cryptopulse.universe.robinhood import (
        SNAPSHOT_BASES,
        fetch_live_catalog,
        load_bases,
        resolve_universe,
    )

    if args.refresh:
        print(f"\nAsking Robinhood for its live currency-pair catalogue...\n  {'-' * 60}")
        bases, note = await fetch_live_catalog()
        if not bases:
            print(f"  FAILED: {note}")
            print("\n  This has never succeeded from inside this project's sandbox. Robinhood publishes")
            print("  no supported public market-data API, so treat a failure here as expected rather")
            print("  than broken, and maintain the list by hand instead:")
            print("    CP_SCAN_ROBINHOOD_FILE=<path to a JSON file of base assets>")
            return 1
        print(f"  OK: {note}")
        added = sorted(set(bases) - set(SNAPSHOT_BASES))
        removed = sorted(set(SNAPSHOT_BASES) - set(bases))
        print(f"  vs the built-in snapshot: +{len(added)} {added}  -{len(removed)} {removed}")
        payload = {"as_of": datetime.now(tz=UTC).date().isoformat(), "source": "robinhood-api", "bases": bases}
        from pathlib import Path

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2))
        print(f"  written to {out}\n  set CP_SCAN_ROBINHOOD_FILE={out} to use it")
        return 0

    bases, source, as_of = load_bases(
        file_path=settings.scanner.robinhood_file,
        extra=settings.scanner.robinhood_extra,
        exclude=settings.scanner.robinhood_exclude,
    )
    print(f"\nRobinhood universe — {len(bases)} base assets   (source: {source}, as of {as_of})")
    print("  " + ", ".join(bases))

    provider = build_market_provider(settings, SYSTEM_CLOCK)
    print(f"\nResolving against {provider.name} / {settings.scanner.quote_asset}...")
    try:
        tickers = await provider.get_tickers_24h()
    except Exception as exc:
        print(f"  could not read the venue's symbols: {type(exc).__name__}: {exc}")
        await provider.close()
        return 1

    res = resolve_universe(bases, list(tickers.keys()), settings.scanner.quote_asset, source=source, as_of=as_of)
    print(f"  {len(res.symbols)} tradable here:")
    for base, symbol in sorted(res.by_base.items()):
        t = tickers.get(symbol)
        vol = f"{t.quote_volume_24h:>16,.0f}" if t else " " * 16
        rename = f"   (listed as {symbol})" if not symbol.startswith(base) else ""
        print(f"    {base:<8} {symbol:<12} 24h vol {vol}{rename}")
    if res.missing:
        print(f"\n  {len(res.missing)} not carried by this venue against "
              f"{settings.scanner.quote_asset}: {', '.join(res.missing)}")
        print("  Those are simply not scanned. Try another quote asset or another venue if you need them.")
    for note in res.notes:
        print(f"\n  ** {note}")
    await provider.close()
    return 0


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #


async def cmd_backtest(args) -> int:
    settings = _settings_with_provider(args)
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


async def _resolve_moonshot(args, settings, provider) -> int:
    """Grade the ×10 axis and print what the journal actually knows.

    Deliberately blunt about emptiness: on a young journal the correct output is
    "nothing has settled yet", not a table of zeroes that looks like a result.
    """
    from cryptopulse.backtest.labels import label_config_by_name
    from cryptopulse.database import repo
    from cryptopulse.outcomes.stats import build_moonshot_performance
    from cryptopulse.outcomes.tracker import OutcomeTracker

    label = label_config_by_name(settings.moonshot.label_config)
    tracker = OutcomeTracker(settings, provider, label_config=label, clock=SYSTEM_CLOCK)

    print(f"\n{'=' * 76}\n×10 OUTCOME TRACKER — {label.name}\n{'=' * 76}")
    print(f"Definition: {label.describe()}\n")

    counts = repo.moonshot_counts()
    print(f"Journal: {counts['readings_journalled']} ×10 readings recorded "
          f"({counts['candidates_journalled']} of them at an early stage)")

    pending = repo.pending_moonshot_signals(tracker.ready_before_ms(), args.limit)
    print(f"{len(pending)} reading(s) old enough to grade "
          f"({label.horizon_bars} {tracker.timeframe.value} bars must have elapsed).")

    if pending:
        report = await tracker.resolve(pending)
        written = repo.save_moonshot_resolutions(report.resolutions)
        print(f"\nresolved={report.resolved}  still_pending={report.still_pending}  "
              f"unresolvable={report.unresolvable}  written={written}  ({report.duration_ms}ms)")
        if report.by_label:
            print("by label: " + ", ".join(f"{k}={v}" for k, v in sorted(report.by_label.items())))
        for sym, err in list(report.errors.items())[:8]:
            print(f"  {sym}: {err}")
        counts = repo.moonshot_counts()

    print(f"\nSettled: {counts['settled']}  ·  pending: {counts['pending_evaluation']}  "
          f"·  unresolvable: {counts['unresolvable']}")

    rows = repo.resolved_moonshot_signals()
    if not rows:
        print("\nNothing has settled yet, so there is nothing to report. That is the honest state of")
        print(f"this layer: a {label.horizon_bars}-day horizon means the first verdicts arrive "
              f"{label.horizon_bars} days after the first scan.")
        return 0

    perf = build_moonshot_performance(rows).to_dict()
    o = perf["overall"]
    print(f"\n{'-' * 76}\nREALISED ×10 PERFORMANCE\n{'-' * 76}")
    print(f"  n={o['n']}  wins={o['wins']}  losses={o['losses']}  timeouts={o['timeouts']}")
    print(f"  win rate at the label target   {_fmt_pct(o['win_rate'])}")
    print(f"  expectancy                     {_fmt(o['expectancy_pct'])}% per reading")
    print(f"  best multiple reached          x{_fmt(perf['best_multiple'])}")
    print(f"  median multiple reached        x{_fmt(perf['median_multiple'])}")

    print("\n  How far did they actually go?")
    for rung in perf["multiple_distribution"]:
        share = "n/a" if rung["share"] is None else f"{rung['share'] * 100:5.1f}%"
        print(f"    reached x{rung['at_least']:<5g} {rung['n']:>5}   {share}")

    if perf["by_stage"]:
        print("\n  By stage at signal time:")
        for b in perf["by_stage"]:
            flag = "  (sample too small)" if b["insufficient_sample"] else ""
            print(f"    {b['key']:<14} n={b['n']:<5} win {_fmt_pct(b['win_rate']):>7}  "
                  f"expectancy {_fmt(b['expectancy_pct']):>7}%{flag}")

    if perf["by_capacity_known"]:
        print("\n  Did knowing the market cap help?")
        for b in perf["by_capacity_known"]:
            print(f"    {b['key']:<18} n={b['n']:<5} win {_fmt_pct(b['win_rate']):>7}  "
                  f"expectancy {_fmt(b['expectancy_pct']):>7}%")

    for note in perf["notes"]:
        print(f"\n  ** {note}")
    return 0


async def cmd_resolve(args) -> int:
    settings = _settings_with_provider(args)
    from cryptopulse.backtest.labels import DEFAULT_LABEL_CONFIGS, label_config_by_name
    from cryptopulse.database import repo
    from cryptopulse.database.session import init_engine
    from cryptopulse.outcomes.stats import build_performance
    from cryptopulse.outcomes.tracker import OutcomeTracker
    from cryptopulse.providers.registry import build_market_provider

    init_engine(settings.database)
    provider = build_market_provider(settings, SYSTEM_CLOCK)

    if args.axis == "moonshot":
        rc = await _resolve_moonshot(args, settings, provider)
        await provider.close()
        return rc

    label = label_config_by_name(args.label, DEFAULT_LABEL_CONFIGS[1])
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

    p_doc = sub.add_parser("doctor", help="verify the configured provider against its live API")
    _add_provider_flag(p_doc)

    p_scan = sub.add_parser("scan", help="run one scan and print the ranking")
    p_scan.add_argument("--limit", type=int, default=30)
    p_scan.add_argument("--json", action="store_true")
    _add_provider_flag(p_scan)
    _add_radar_flags(p_scan)

    p_radar = sub.add_parser("radar", help="autonomous loop: scan, alert, notify, repeat")
    p_radar.add_argument("--interval", type=int, default=None, help="seconds between scans")
    p_radar.add_argument("--once", action="store_true", help="run a single cycle and exit")
    _add_provider_flag(p_radar)
    _add_radar_flags(p_radar)

    p_uni = sub.add_parser("universe", help="show what the Robinhood universe resolves to on this venue")
    p_uni.add_argument("--refresh", action="store_true", help="try to read Robinhood's live catalogue")
    p_uni.add_argument("--out", type=str, default="data/robinhood_universe.json")
    _add_provider_flag(p_uni)
    _add_radar_flags(p_uni)

    p_bt = sub.add_parser("backtest", help="replay the scorer over history")
    p_bt.add_argument("--symbols", type=str, default=None, help="comma-separated, default: config majors")
    p_bt.add_argument("--bars", type=int, default=1000)
    p_bt.add_argument("--min-score", type=float, default=65.0, dest="min_score")
    p_bt.add_argument("--label", type=str, default="standard_2R")
    _add_provider_flag(p_bt)

    p_res = sub.add_parser("resolve", help="grade emitted signals against what actually happened")
    p_res.add_argument("--limit", type=int, default=500)
    p_res.add_argument("--label", type=str, default="standard_2R")
    p_res.add_argument(
        "--axis", choices=("setup", "moonshot"), default="setup",
        help="which thesis to grade: the intraday setup, or the ×10 reading (weeks, daily bars)",
    )
    _add_provider_flag(p_res)

    p_serve = sub.add_parser("serve", help="run the API and dashboard")
    p_serve.add_argument("--host", type=str, default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--reload", action="store_true")

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    if args.command == "serve":
        return cmd_serve(args)
    handler = {
        "doctor": cmd_doctor,
        "scan": cmd_scan,
        "radar": cmd_radar,
        "universe": cmd_universe,
        "backtest": cmd_backtest,
        "resolve": cmd_resolve,
    }[args.command]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main())
