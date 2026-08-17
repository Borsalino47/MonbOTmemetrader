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
import os
import sys
import time

# Stamped before the heavy imports below, so `server_ready_ms` measures the wait
# the user actually experiences rather than the wait after numpy, SQLAlchemy and
# FastAPI have already loaded. On a phone those imports are the larger half of
# the launch, and a metric that excluded them would have said the start was fast
# while the screen was still blank.
os.environ.setdefault("CP_PROCESS_START", str(time.time()))

from cryptopulse.config.settings import get_settings
from cryptopulse.core.clock import SYSTEM_CLOCK
from cryptopulse.core.logging import configure_logging, get_logger

log = get_logger("cli")

PROVIDERS = ("binance", "kraken", "fixture")


def _settings_with_provider(args):
    """Settings with `--provider` applied, so switching venue is one word.

    The flag overrides CP_PROVIDER_MARKET_DATA for this invocation only; nothing
    is written to disk.
    """
    settings = get_settings()
    choice = getattr(args, "provider", None)
    if choice:
        settings.providers.market_data = choice
    return settings


def _add_provider_flag(parser) -> None:
    parser.add_argument(
        "--provider", choices=PROVIDERS, default=None,
        help="market data source for this run (default: CP_PROVIDER_MARKET_DATA)",
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

    # No cache: `doctor` exists to prove the network round trip works. An answer
    # served from memory would report success without having contacted anything.
    provider = build_market_provider(settings, SYSTEM_CLOCK, cached=False)
    # Venues disagree on naming — BTCUSDT on Binance, XBTUSDT on Kraken — so the
    # provider names the symbol it is certain to have rather than the CLI guessing.
    ref = provider.reference_symbol
    checks: list[tuple[str, bool, str]] = []
    print(
        f"\nCRYPTO PULSE AI — diagnostic du fournisseur\n"
        f"fournisseur : {provider.name}  référence : {ref}\n" + "-" * 68
    )

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  [{'OK  ' if ok else 'ÉCHEC'}] {name}" + (f" — {detail}" if detail else ""))

    # 1. connectivity
    unreachable = False
    try:
        health = await provider.health()
        record("joignabilité du fournisseur", health.available, health.detail or f"{health.latency_ms:.0f} ms")
        unreachable = not health.available
    except Exception as exc:
        record("joignabilité du fournisseur", False, f"{type(exc).__name__} : {exc}")
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
        record("liste des paires", ok, f"{len(symbols)} paires en {settings.scanner.quote_asset}")
    except Exception as exc:
        record("liste des paires", False, f"{type(exc).__name__} : {exc}")

    # 3. tickers
    tickers = {}
    try:
        tickers = await provider.get_tickers_24h([ref])
        t = tickers.get(ref)
        ok = t is not None and t.last_price > 0 and t.quote_volume_24h > 0 and t.low_24h <= t.last_price <= t.high_24h
        detail = (
            f"BTC {t.last_price:,.2f}, plage 24 h {t.low_24h:,.2f}-{t.high_24h:,.2f}, "
            f"volume {t.quote_volume_24h:,.0f}"
            if t
            else f"aucune ligne {ref}"
        )
        record("cohérence du ticker 24 h (bas <= dernier <= haut)", ok, detail)
    except Exception as exc:
        record("cohérence du ticker 24 h", False, f"{type(exc).__name__} : {exc}")

    # 4. klines — the field-order check that matters most
    try:
        series = await provider.get_ohlcv(ref, Timeframe.M5, 120)
        n = len(series)
        checks_ok = True
        details = []

        ordered = bool((series.open_time_ms[1:] > series.open_time_ms[:-1]).all()) if n > 1 else True
        checks_ok &= ordered
        details.append(f"{n} bougies, ordre croissant={ordered}")

        # If field indices were wrong, high/low invariants would break immediately.
        invariant = bool(
            (series.high >= series.low).all()
            and (series.high >= series.open).all()
            and (series.high >= series.close).all()
            and (series.low <= series.open).all()
            and (series.low <= series.close).all()
        )
        checks_ok &= invariant
        details.append(f"invariants OHLC={invariant}")

        spacing = bool(((series.open_time_ms[1:] - series.open_time_ms[:-1]) == Timeframe.M5.ms).all()) if n > 1 else True
        details.append(f"espacement 5 min={spacing}")

        record("correspondance des champs de bougies + invariants", checks_ok, " ; ".join(details))

        # 5. the open-candle rule
        closed = series.closed()
        dropped = len(series) - len(closed)
        now = SYSTEM_CLOCK.now_ms()
        ok = closed.last_close_time_ms <= now
        record(
            "bougie en cours exclue",
            ok,
            f"{dropped} bougie(s) en formation écartée(s) ; la dernière bougie close "
            f"s'est terminée il y a {(now - closed.last_close_time_ms) / 1000:.0f} s",
        )

        # 6. cross-check klines against the ticker
        if tickers.get(ref):
            last = tickers[ref].last_price
            kline_close = closed.last_close
            drift = abs(kline_close - last) / last * 100
            ok = drift < 2.0
            record("clôture des bougies cohérente avec le prix du ticker", ok, f"{drift:.3f} % d'écart")
    except Exception as exc:
        record("bougies", False, f"{type(exc).__name__} : {exc}")

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
        record(
            "carnet d'ordres : achat < vente, écart >= 0",
            ok,
            f"écart {book.spread_bps:.2f} pdb, déséquilibre {book.imbalance():+.3f}",
        )
    except Exception as exc:
        record("carnet d'ordres", False, f"{type(exc).__name__} : {exc}")

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

    out: list[str] = ["", "-" * 68, "DIAGNOSTIC RÉSEAU", "-" * 68]

    # 1. Name resolution.
    try:
        await asyncio.to_thread(socket.getaddrinfo, host, 443)
        out.append(f"  DNS            résout {host}")
    except OSError as exc:
        out += [
            f"  DNS            ÉCHOUE pour {host} : {exc}",
            "",
            "  Le nom d'hôte ne se résout pas : rien d'autre ne peut être diagnostiqué.",
            "  Corrigez le résolveur DNS de la machine avant de regarder autre chose.",
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
        out.append(f"  requête directe ÉCHOUE : {transport_error}")
        if tls_intercepted:
            out += [
                "",
                "  La vérification TLS a échoué sur une chaîne auto-signée : un proxy",
                "  intercepte et re-termine le TLS. Il y a donc bien une passerelle devant",
                "  cet hôte, et le refus vient presque certainement de sa politique de",
                "  sortie, pas d'une panne réseau.",
                "",
                "  Pointez le bundle de certificats sur celui de la passerelle et relancez,",
                "  par exemple SSL_CERT_FILE=/chemin/vers/ca-bundle.crt, puis respectez la",
                "  liste d'hôtes autorisés que cette passerelle impose.",
            ]
        else:
            out += [
                "",
                "  Le DNS résout mais aucune réponse n'est revenue. C'est un pare-feu ou",
                "  une route manquante, pas un refus de politique — une politique répondrait",
                "  avec un code de statut. Vérifiez les règles du pare-feu sortant.",
            ]
        return out

    out.append(f"  requête directe HTTP {status}" + (f"  x-deny-reason : {deny_reason}" if deny_reason else ""))

    if status == 200:
        out += [
            "",
            "  L'hôte est joignable lorsque le proxy local est contourné : le réseau va",
            "  bien et la panne est dans la configuration du proxy. Vérifiez HTTPS_PROXY",
            "  et si ce proxy autorise cet hôte.",
        ]
        return out

    if deny_reason or "allowlist" in body.lower():
        out += [
            "",
            "  Une passerelle de sortie a refusé cet hôte — la requête n'a jamais atteint",
            "  le fournisseur. C'est un réglage d'environnement, pas un défaut du",
            "  connecteur ni un problème de la plateforme ou de la chaîne.",
            "",
            "  Sur Claude Code sur le web : ouvrez claude.ai/code, cliquez sur l'icône",
            "  nuage au-dessus de la zone de message, survolez votre environnement et",
            "  ouvrez ses réglages, passez « Network access » sur « Custom », puis ajoutez",
            "  ceci aux domaines autorisés (laissez cochée l'option qui inclut la liste par",
            "  défaut des gestionnaires de paquets, pour que pip et npm continuent de",
            "  fonctionner) :",
            "",
        ] + [f"      {h}" for h in _hosts_to_allow(host)] + [
            "",
            "  Démarrez ensuite une NOUVELLE session — une session en cours conserve la",
            "  politique avec laquelle elle a démarré.",
            "  Référence : code.claude.com/docs/en/cloud-environments",
        ]
        return out

    if status in (403, 451):
        out += [
            "",
            f"  La plateforme elle-même a répondu {status} sans en-tête de refus de",
            "  passerelle, ce qui signifie en général que la région de l'adresse IP est",
            "  restreinte. Lancez depuis une région autorisée, ou pointez le connecteur",
            "  sur la plateforme de votre juridiction (aux États-Unis : api.binance.us,",
            "  un contrat d'API différent que ce connecteur n'implémente pas).",
        ]
        return out

    out += ["", f"  Statut inattendu {status}. Corps : {body[:120]}"]
    return out


def _summarise(checks) -> int:
    passed = sum(1 for _, ok, _ in checks if ok)
    total = len(checks)
    print("-" * 68)
    if passed == total:
        print(f"FLUX LIVE VÉRIFIÉ — {passed}/{total} contrôles réussis contre la vraie API.\n")
        return 0
    print(
        f"NON VÉRIFIÉ — {passed}/{total} contrôles réussis. Corrigez les échecs ci-dessus "
        "avant de faire confiance à ce flux.\n"
    )
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


async def cmd_resolve(args) -> int:
    settings = _settings_with_provider(args)
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


# --------------------------------------------------------------------------- #
# verify — did the predictions hold up 15m / 1h / 4h / 24h later?
# --------------------------------------------------------------------------- #


async def cmd_verify(args) -> int:
    settings = _settings_with_provider(args)
    from cryptopulse.database import repo
    from cryptopulse.database.session import init_engine
    from cryptopulse.outcomes.horizons import HORIZONS, HorizonTracker
    from cryptopulse.outcomes.stats import build_horizon_performance
    from cryptopulse.providers.registry import build_market_provider, is_synthetic

    init_engine(settings.database)
    provider = build_market_provider(settings, SYSTEM_CLOCK)
    tracker = HorizonTracker(settings, provider, clock=SYSTEM_CLOCK)

    print(f"\n{'=' * 76}\nSIGNAL VERIFICATION — {', '.join(h.name for h in HORIZONS)} after each signal\n{'=' * 76}")
    print(f"Provider: {provider.name}" + ("   ** SYNTHETIC — not market data **" if is_synthetic(provider) else ""))
    print("Entry:    the open of the bar after the signal.")
    print(f"Success:  net change above zero after costs ({tracker.costs.describe()}).\n")

    pending = repo.signals_needing_horizons(tracker.ready_before_ms(), args.limit)
    print(f"{len(pending)} signal(s) with at least one window that may have closed.")
    if pending:
        report = await tracker.track(pending)
        written = repo.save_horizons(report.results)
        print(f"resolved={report.resolved}  pending={report.pending}  "
              f"unresolvable={report.unresolvable}  written={written}  ({report.duration_ms}ms)")
        if report.errors:
            print(f"\n{len(report.errors)} symbol(s) failed:")
            for sym, err in list(report.errors.items())[:8]:
                print(f"  {sym}: {err}")
    await provider.close()

    rows = repo.horizon_rows()
    if not rows:
        print("\nNo horizon window has closed yet. Nothing to report — that is not a zero.")
        return 0

    perf = build_horizon_performance(rows)
    print(f"\n{'-' * 76}\nWHAT ACTUALLY HAPPENED AFTER EACH SIGNAL\n{'-' * 76}")
    print(f"  {'window':<8} {'n':>5} {'success':>9} {'median':>9} {'average':>9} "
          f"{'best':>9} {'worst':>9} {'max DD':>9}")
    for b in perf["overall"]:
        flag = "  (sample too small)" if b["insufficient_sample"] else ""
        print(f"  {b['horizon']:<8} {b['n']:>5} {_fmt_pct(b['success_rate']):>9} "
              f"{_fmt(b['median_change_pct']):>8}% {_fmt(b['avg_change_pct']):>8}% "
              f"{_fmt(b['best_change_pct']):>8}% {_fmt(b['worst_change_pct']):>8}% "
              f"{_fmt(b['avg_max_drawdown_pct']):>8}%{flag}")

    bands = [b for b in perf["by_score_band"] if b["n"] > 0]
    if bands:
        print(f"\n{'-' * 76}\nBY SCORE BAND — does a higher score actually do better?\n{'-' * 76}")
        print(f"  {'band':<8} {'window':<8} {'n':>5} {'success':>9} {'median':>9}")
        for b in bands:
            flag = "  (sample too small)" if b["insufficient_sample"] else ""
            print(f"  {b['key']:<8} {b['horizon']:<8} {b['n']:>5} "
                  f"{_fmt_pct(b['success_rate']):>9} {_fmt(b['median_change_pct']):>8}%{flag}")

    for note in perf["notes"]:
        print(f"\n  ** {note}")
    return 0


def _fmt(x, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _fmt_pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #


def cmd_db_path(args) -> int:
    """Print the resolved database file path, and nothing else.

    Exists because a shell script cannot reliably work this out. `CP_DB_URL` may
    live in `.env` and never reach the shell environment, so
    `${CP_DB_URL##*sqlite:///}` under `set -u` aborts the script before it can
    back anything up — which is how a backup step ended up being the thing that
    prevented the backup. The settings object already knows the answer; this
    hands it to the shell.

    Prints nothing and exits 1 for a non-SQLite URL: there is no file to back up
    for Postgres, and printing something file-shaped would invite `cp` to create
    a garbage path.
    """
    url = get_settings().database.url
    if not url.startswith("sqlite:///"):
        print(f"not a sqlite database: {url.split('://', 1)[0]}://…", file=sys.stderr)
        return 1
    path = url.replace("sqlite:///", "", 1)
    if path == ":memory:":
        print("in-memory database — nothing on disk to back up", file=sys.stderr)
        return 1
    print(path)
    return 0


async def cmd_doctor_robinhood(args) -> int:
    """The Robinhood Chain doctor — §59: runnable on the phone, prints its proof.

    Entirely independent of the Binance doctor: a green Binance says nothing
    about this chain, and the two never share a check or a verdict.
    """
    from cryptopulse.providers.robinhood import ROBINHOOD_CHAIN_ID, RobinhoodRpc
    from cryptopulse.providers.robinhood_verify import verify_robinhood_chain

    settings = get_settings()
    rpc = RobinhoodRpc(settings.robinhood)
    print(
        f"\nCRYPTO PULSE AI — Robinhood Chain doctor"
        f"\nendpoint: {rpc.endpoint_host}  chaîne attendue: {ROBINHOOD_CHAIN_ID}\n" + "-" * 68
    )

    result = await verify_robinhood_chain(rpc, timeout_seconds=30.0)
    for check in result.checks:
        flag = "PASS" if check.ok else "FAIL"
        core = " (bloquant)" if check.core and not check.ok else ""
        print(f"  [{flag}] {check.name}{core}" + (f" — {check.detail}" if check.detail else ""))

    print("-" * 68)
    print(f"{result.emoji} {result.label_fr} — {result.passed}/{len(result.checks)} en {result.duration_ms} ms")
    if result.error:
        print(f"   raison : {result.error}")
    if result.state.value == "FAILED":
        host = rpc.endpoint_host
        for line in await _diagnose_egress(host):
            print(line)
    await rpc.close()

    # The indexer is a second, independent source. A live chain says nothing
    # about it, so it gets its own round trip and its own verdict.
    print("\n" + "-" * 68)
    print("SOURCE DES DONNÉES DE MARCHÉ (indexeur DEX)")
    print("-" * 68)
    gecko_ok = await _check_discovery_source(settings)

    print()
    return 0 if (result.verified and gecko_ok) else 1


async def _check_discovery_source(settings) -> bool:
    """Prove the indexer answers, knows this network, and returns usable pools.

    Three checks rather than one ping: an indexer that is up but has never
    heard of `robinhood` would answer every request with an empty list, which
    is indistinguishable from a quiet chain unless the network id is verified
    separately.
    """
    from cryptopulse.hunter.robinhood_discovery import discover_new_tokens
    from cryptopulse.providers.geckoterminal import GeckoTerminalClient

    client = GeckoTerminalClient(settings.robinhood)
    ok = True
    try:
        try:
            networks = await client.list_networks()
            known = client.network in networks
            print(f"  [{'PASS' if known else 'FAIL'}] réseau '{client.network}' connu de l'indexeur"
                  f" — {len(networks)} réseaux listés")
            ok = ok and known
        except Exception as exc:
            print(f"  [FAIL] indexeur joignable — {type(exc).__name__}")
            for line in await _diagnose_egress("api.geckoterminal.com"):
                print(line)
            return False

        report = await discover_new_tokens(client, settings.robinhood)
        if report.errors:
            print(f"  [FAIL] recherche de nouveaux pools — {'; '.join(report.errors[:2])}")
            return False
        print(f"  [PASS] recherche de nouveaux pools — {report.pools_seen} pools lus, "
              f"{len(report.candidates)} tokens retenus, {report.requests} requête(s)")

        # Zero tokens is not a failure: it is what a genuinely quiet window
        # looks like, and calling it one would be inventing an outage.
        if report.candidates:
            top = report.candidates[0]
            age_min = (top.pool_age_seconds or 0) / 60
            print(f"         plus récent : {top.symbol or '?'} "
                  f"({top.address[:10]}…), pool de {age_min:.0f} min")
        else:
            print("         aucun token dans la fenêtre d'âge — la chaîne est peut-être calme")
    finally:
        await client.close()
    return ok


async def cmd_notify(args) -> int:
    """Send a real notification, so the round trip can be confirmed on the phone.

    Everything about this channel is testable from a laptop except the one thing
    that matters — that Android actually shows the notification. This command is
    that check, and it deliberately reports the availability diagnosis first so a
    failure names which of the two Termux:API installs is missing.
    """
    from cryptopulse.alerts.engine import Alert, AlertLevel
    from cryptopulse.alerts.notify import build_notifier

    settings = get_settings()
    notifier = build_notifier(
        enabled=settings.alerts.android_notifications,
        binary=settings.alerts.android_notification_binary,
    )
    availability = notifier.availability()

    print(f"\nChannel   : {notifier.name}")
    print(f"Available : {'yes' if availability.available else 'NO'}")
    print(f"Reason    : {availability.reason}")
    if availability.fix:
        print(f"Fix       : {availability.fix}")

    if not availability.available:
        print("\nNothing sent. Fix the above and run this again.")
        return 1

    probe = Alert(
        symbol="TESTUSDT",
        level=AlertLevel.HIGH,
        headline="test notification from CRYPTO PULSE AI",
        timestamp_ms=SYSTEM_CLOCK.now_ms(),
        final_score=77.0,
        pump_maturity=0.0,
        data_confidence=0.0,
        safety=0.0,
        liquidity="UNKNOWN",
        state="IGNORE",
        price=0.0,
        score_acceleration=None,
        why=["this is a test, not a signal"],
    )
    # `synthetic=True` on purpose: this alert is invented, and the one rule that
    # must never bend is that an invented number is labelled as one.
    report = await notifier.send([probe], synthetic=True)

    print()
    if report.ok and report.sent:
        print("Sent. Look at your phone — a notification titled 'DÉMO … TESTUSDT'")
        print("should be on screen. If nothing appeared, the Termux:API *app* is")
        print("missing even though the termux-api package is installed.")
        return 0

    print(f"Failed: {report.error}")
    return 1


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

    sub.add_parser(
        "doctor-robinhood",
        help="verify Robinhood Chain (RPC, chain id 4663, block progression) — independent of Binance",
    )

    p_scan = sub.add_parser("scan", help="run one scan and print the ranking")
    p_scan.add_argument("--limit", type=int, default=30)
    p_scan.add_argument("--json", action="store_true")
    _add_provider_flag(p_scan)

    p_bt = sub.add_parser("backtest", help="replay the scorer over history")
    p_bt.add_argument("--symbols", type=str, default=None, help="comma-separated, default: config majors")
    p_bt.add_argument("--bars", type=int, default=1000)
    p_bt.add_argument("--min-score", type=float, default=65.0, dest="min_score")
    p_bt.add_argument("--label", type=str, default="standard_2R")
    _add_provider_flag(p_bt)

    p_res = sub.add_parser("resolve", help="grade emitted signals against what actually happened")
    p_res.add_argument("--limit", type=int, default=500)
    p_res.add_argument("--label", type=str, default="standard_2R")
    _add_provider_flag(p_res)

    p_ver = sub.add_parser("verify", help="check what the price did 15m/1h/4h/24h after each signal")
    p_ver.add_argument("--limit", type=int, default=500)
    _add_provider_flag(p_ver)

    p_not = sub.add_parser("notify", help="send a test Android notification (Termux)")
    p_not.add_argument("--test", action="store_true", help="kept for readability; this command always tests")

    sub.add_parser("db-path", help="print the resolved SQLite file path (for scripts)")

    p_serve = sub.add_parser("serve", help="run the API and dashboard")
    p_serve.add_argument("--host", type=str, default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--reload", action="store_true")

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "db-path":
        return cmd_db_path(args)
    handler = {
        "doctor": cmd_doctor,
        "doctor-robinhood": cmd_doctor_robinhood,
        "scan": cmd_scan,
        "backtest": cmd_backtest,
        "resolve": cmd_resolve,
        "verify": cmd_verify,
        "notify": cmd_notify,
    }[args.command]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main())
