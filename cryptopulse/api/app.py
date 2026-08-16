"""FastAPI application.

Every response that carries market-derived numbers also carries their age and
their source. `/api/health` is the contract the dashboard uses to decide whether
to show a "data is stale" banner — the front end never has to guess.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cryptopulse.api.service import get_service
from cryptopulse.config.settings import get_settings
from cryptopulse.core.clock import SYSTEM_CLOCK
from cryptopulse.core.logging import configure_logging, get_logger
from cryptopulse.database import repo
from cryptopulse.scoring.discovery import DISCOVERY_ENGINE_VERSION
from cryptopulse.scoring.discovery import WEIGHTS as DISCOVERY_WEIGHTS

log = get_logger("api")

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def service_now_ms() -> int:
    return SYSTEM_CLOCK.now_ms()


def _filter_snapshot(
    rows: list[dict],
    min_score: float,
    max_pump_maturity: float,
    state: str | None,
    min_liquidity: str | None,
    premium_only: bool,
) -> list[dict]:
    """Apply the /api/scan filters to journal rows.

    Kept deliberately parallel to the live path so the same query returns the
    same selection whichever source answered it.
    """
    from cryptopulse.risk.liquidity import LiquidityStatus

    out = [r for r in rows if r["final_score"] >= min_score]
    out = [r for r in out if r["pump_maturity"]["score"] <= max_pump_maturity]
    if state:
        wanted = {s.strip().upper() for s in state.split(",")}
        out = [r for r in out if r["setup"]["state"] in wanted]
    if min_liquidity:
        try:
            floor = LiquidityStatus(min_liquidity.upper()).rank
        except ValueError as exc:
            raise HTTPException(400, f"unknown liquidity status {min_liquidity!r}") from exc
        out = [r for r in out if LiquidityStatus(r["liquidity"]["status"]).rank >= floor]
    if premium_only:
        out = [r for r in out if r["is_premium"]]
    return out


def create_app(*, start_loop: bool = True) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        service = get_service()
        service.ensure_db()
        if start_loop:
            await service.start()
        yield
        await service.stop()

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=(
            "Early-acceleration crypto scanner. Scores are 0-100 rankings, NOT probabilities. "
            "Every response reports the source and age of the data behind it."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ---------------------------------------------------------------- health #

    @app.get("/api/health", tags=["status"])
    async def health():
        return get_service().status()

    @app.get("/api/config", tags=["status"])
    async def config():
        s = get_settings()
        return {
            "engine_version": s.scoring.engine_version,
            "paper_mode": s.paper_mode,
            "weights": {
                "volume": s.scoring.w_volume,
                "momentum": s.scoring.w_momentum,
                "structure": s.scoring.w_structure,
                "breakout": s.scoring.w_breakout,
                "volatility": s.scoring.w_volatility,
                "orderflow": s.scoring.w_orderflow,
                "mtf": s.scoring.w_mtf,
                "liquidity": s.scoring.w_liquidity,
            },
            "thresholds": {
                "observe": s.scoring.threshold_observe,
                "watch": s.scoring.threshold_watch,
                "armed": s.scoring.threshold_armed,
            },
            "timeframes": [tf.value for tf in s.scanner.timeframes],
            "primary_timeframe": s.scanner.primary_timeframe.value,
            "scan_interval_seconds": s.scanner.scan_interval_seconds,
            "disclaimer": (
                "The opportunity score is a transparent 0-100 ranking produced by fixed weights. "
                "It has NOT been statistically calibrated and must not be read as a probability."
            ),
        }

    # ----------------------------------------------------------------- scan #

    @app.get("/api/scan", tags=["scanner"])
    async def scan_results(
        limit: int = Query(200, ge=1, le=1000),
        min_score: float = Query(0.0, ge=0, le=100),
        max_pump_maturity: float = Query(100.0, ge=0, le=100),
        state: str | None = None,
        min_liquidity: str | None = None,
        premium_only: bool = False,
    ):
        service = get_service()
        report = service.last_report
        if report is None:
            # No scan has run in THIS process yet. Rather than an empty screen
            # for the length of a full scan, serve the last one from the journal
            # — labelled with its real age and its own provenance, never as live.
            snap = repo.last_scan_snapshot(limit=limit)
            if snap is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "reason": "NO_SCAN_YET",
                        "message": "No scan has ever completed and the journal is empty. "
                                   "Call POST /api/scan/run or wait for the loop.",
                    },
                )
            rows = _filter_snapshot(
                snap["rows"], min_score, max_pump_maturity, state, min_liquidity, premium_only
            )
            return {
                "meta": {
                    "source": "journal",
                    "live": False,
                    "stale": True,
                    "age_seconds": snap["age_seconds"],
                    "message": (
                        "Last recorded scan, restored from the journal while a fresh one runs. "
                        "Order-book columns were never journalled and read as unknown."
                    ),
                    "started_at_ms": snap["started_at_ms"],
                    "finished_at_ms": snap["signals_at_ms"],
                    "duration_ms": snap["duration_ms"],
                    "universe_size": snap["universe_size"],
                    "scanned": snap["scanned"],
                    "succeeded": snap["succeeded"],
                    "failed": snap["failed"],
                    "provider": snap["provider"],
                    "synthetic_data": snap["synthetic"],
                    "data_mode": "DEMO" if snap["synthetic"] else "LIVE",
                    "notes": [],
                    "errors": {},
                    "returned": min(len(rows), limit),
                    "matched": len(rows),
                },
                "results": rows[:limit],
            }

        from cryptopulse.risk.liquidity import LiquidityStatus

        rows = report.results
        rows = [r for r in rows if r.final_score >= min_score]
        rows = [r for r in rows if r.maturity.score <= max_pump_maturity]
        if state:
            wanted = {s.strip().upper() for s in state.split(",")}
            rows = [r for r in rows if r.state.state.value in wanted]
        if min_liquidity:
            try:
                floor = LiquidityStatus(min_liquidity.upper()).rank
                rows = [r for r in rows if r.liquidity.status.rank >= floor]
            except ValueError as exc:
                raise HTTPException(400, f"unknown liquidity status {min_liquidity!r}") from exc
        if premium_only:
            rows = [r for r in rows if r.is_premium]

        return {
            "meta": {
                **{k: v for k, v in report.to_dict().items() if k != "results"},
                "source": "live",
                "live": True,
                "stale": False,
                "data_mode": "DEMO" if report.synthetic_data else "LIVE",
                "returned": min(len(rows), limit),
                "matched": len(rows),
            },
            "results": [r.to_dict() for r in rows[:limit]],
        }

    @app.get("/api/scan/top", tags=["scanner"])
    async def top_opportunities(limit: int = Query(10, ge=1, le=50)):
        """Best *setups*, not biggest movers.

        Filters out vetoed and low-confidence rows, then keeps the scanner's own
        ranking, which already blends score, acceleration and confidence.
        """
        service = get_service()
        if service.last_report is None:
            return JSONResponse(status_code=503, content={"reason": "NO_SCAN_YET", "results": []})
        rows = [
            r
            for r in service.results()
            if not (r.safety.hard_veto or r.liquidity.veto) and r.confidence.score >= 50
        ]
        return {"results": [r.to_dict() for r in rows[:limit]]}

    @app.post("/api/scan/run", tags=["scanner"])
    async def run_scan():
        report = await get_service().run_once()
        return {k: v for k, v in report.to_dict().items() if k != "results"}

    # ---------------------------------------------------------------- asset #

    @app.get("/api/asset/{symbol}", tags=["asset"])
    async def asset_detail(symbol: str):
        service = get_service()
        result = service.find(symbol)
        if result is None:
            raise HTTPException(404, f"{symbol.upper()} not present in the last scan")
        payload = result.to_dict(include_features=True)
        payload["score_history"] = [p.to_dict() for p in service.memory.history(symbol.upper(), 200)]
        payload["explainability"] = {
            "raw_score": round(result.raw_score, 2),
            "risk_penalty": round(result.risk_penalty, 2),
            "final_score": round(result.final_score, 2),
            "breakdown": [
                {"component": c.name, "points": round(c.points, 2), "max": c.max_points, "reasons": c.reasons}
                for c in result.components
            ],
            "penalties": [p.to_dict() for p in result.penalties.items],
        }
        payload["why_this_asset"] = result.why()
        payload["what_can_invalidate_it"] = (
            [result.state.invalidation] if result.state.invalidation else []
        ) + result.risks()
        return payload

    @app.get("/api/asset/{symbol}/history", tags=["asset"])
    async def asset_history(symbol: str, limit: int = Query(300, ge=1, le=1000), source: str = "memory"):
        service = get_service()
        if source == "db":
            return {"symbol": symbol.upper(), "source": "database", "points": repo.score_history(symbol, limit)}
        return {
            "symbol": symbol.upper(),
            "source": "memory",
            "points": [p.to_dict() for p in service.memory.history(symbol.upper(), limit)],
        }

    # --------------------------------------------------------------- alerts #

    @app.get("/api/alerts", tags=["alerts"])
    async def alerts(limit: int = Query(50, ge=1, le=200), source: str = "memory"):
        if source == "db":
            return {"alerts": repo.recent_alerts(limit)}
        return {"alerts": [a.to_dict() for a in get_service().alerts.recent(limit)]}

    # -------------------------------------------------------------- signals #

    @app.get("/api/signals", tags=["signals"])
    async def signals(
        limit: int = Query(100, ge=1, le=500),
        symbol: str | None = None,
        min_score: float | None = None,
    ):
        return {"stats": repo.signal_stats(), "signals": repo.recent_signals(limit, symbol, min_score)}

    # -------------------------------------------------------------- outcomes #

    @app.get("/api/performance", tags=["outcomes"])
    async def performance(
        include_synthetic: bool = True,
        use_net: bool = True,
        limit: int = Query(5000, ge=1, le=50000),
    ):
        """Realised performance over signals that carry a settled verdict.

        Reports `n` beside every rate and flags buckets below the minimum sample.
        With no settled signals the report is empty rather than zero-filled.
        """
        from cryptopulse.outcomes.stats import build_performance

        rows = repo.resolved_signals(limit=limit, include_synthetic=include_synthetic)
        report = build_performance(rows, use_net=use_net, synthetic_included=include_synthetic)
        return {
            "counts": repo.outcome_counts(),
            "label": {
                "config": get_service().tracker.label.name,
                "definition": get_service().tracker.label.describe(),
            },
            "costs": get_service().tracker.costs.describe(),
            "performance": report.to_dict(),
        }

    @app.post("/api/outcomes/resolve", tags=["outcomes"])
    async def resolve_outcomes(limit: int = Query(300, ge=1, le=2000)):
        """Grade every pending signal whose horizon has elapsed."""
        report = await get_service().resolve_outcomes(limit=limit)
        return {"resolution": report.to_dict(), "counts": repo.outcome_counts()}

    # -------------------------------------------------------------- horizons #

    @app.get("/api/horizons", tags=["horizons"])
    async def horizons(
        include_synthetic: bool = True,
        use_net: bool = True,
        limit: int = Query(20000, ge=1, le=200000),
    ):
        """What the price actually did 15m / 1h / 4h / 24h after each signal.

        Complements `/api/performance`: the barrier verdict says what you would
        have traded, this says what the market did. Pending windows are absent,
        never reported as a zero.
        """
        from cryptopulse.outcomes.stats import build_horizon_performance

        rows = repo.horizon_rows(limit=limit, include_synthetic=include_synthetic)
        return {
            "tracker": get_service().status()["horizon_tracker"],
            "costs": get_service().horizons.costs.describe(),
            "synthetic_included": include_synthetic,
            "performance": build_horizon_performance(rows, use_net=use_net),
        }

    @app.post("/api/horizons/track", tags=["horizons"])
    async def track_horizons(limit: int = Query(300, ge=1, le=2000)):
        """Fill in every horizon window that has fully elapsed."""
        report = await get_service().track_horizons(limit=limit)
        return {"run": report.to_dict()}

    @app.get("/api/signals/{signal_id}/horizons", tags=["horizons"])
    async def signal_horizons(signal_id: int):
        rows = repo.horizons_for_signal(signal_id)
        if not rows:
            return {
                "signal_id": signal_id,
                "horizons": [],
                "message": (
                    "No horizon window has closed for this signal yet. A pending window is "
                    "reported as absent rather than settled at the current price."
                ),
            }
        return {"signal_id": signal_id, "horizons": rows}

    @app.get("/api/outcomes/pending", tags=["outcomes"])
    async def pending_outcomes(limit: int = Query(100, ge=1, le=1000)):
        service = get_service()
        ready = service.tracker.ready_before_ms()
        pending = repo.pending_signals(ready, limit)
        return {
            "ready_before_ms": ready,
            "horizon_bars": service.tracker.label.horizon_bars,
            "count": len(pending),
            "signals": [
                {"id": p.id, "symbol": p.symbol, "timestamp_ms": p.timestamp_ms, "price": p.price, "atr": p.atr}
                for p in pending
            ],
        }

    # ----------------------------------------------------------- hunter #

    @app.get("/api/hunt", tags=["hunter"])
    async def hunt(limit: int = Query(40, ge=1, le=200)):
        """Rank the whole venue on cheap data and return the best candidates.

        Costs no request: it reads the venue-wide ticker the last scan already
        fetched. Candidates are ranked by *anomaly*, not by size — a small token
        whose activity has doubled outranks a large one trading normally, which
        is the opposite of what the volume-sorted scanner universe does.

        A candidate is a suggestion to look closer, never a judgement about the
        asset. That belongs to the deep scan, which sees the price history this
        stage deliberately never fetches.
        """
        service = get_service()
        if not service.scanner.last_tickers:
            return JSONResponse(
                status_code=503,
                content={
                    "reason": "NO_SCAN_YET",
                    "message": "The hunter reads the ticker snapshot a scan produces. "
                               "Run POST /api/scan/run first, or wait for the loop.",
                },
            )
        # Serve the cycle's own report rather than recomputing.
        #
        # Recomputing would read the same ticker dictionary and produce the same
        # ranking, but against a snapshot memory the cycle has just advanced —
        # so every delta would come back null and the acceleration signal would
        # vanish exactly when it is asked for. The stored report already holds
        # it. A recompute happens only when no cycle has produced one yet.
        stored = service.last_prescan
        report = stored.to_dict() if stored else service.hunt(limit=limit, record=False).to_dict()
        report["candidates"] = report["candidates"][:limit]
        report["returned"] = len(report["candidates"])
        age_s = (
            round((service_now_ms() - report["computed_at_ms"]) / 1000, 1)
            if report.get("computed_at_ms")
            else None
        )
        return {
            "prescan": report,
            "age_seconds": age_s,
            "refreshes_with_the_scan": True,
            "data_mode": "DEMO" if service.status()["synthetic_data"] else "LIVE",
            "disclaimer": (
                "Priority ranks which tokens deserve an expensive look. It is not a score, "
                "not a probability, and says nothing about whether a token is worth buying."
            ),
        }

    @app.post("/api/hunt/deep", tags=["hunter"])
    async def deep_scan(max_symbols: int = Query(40, ge=1, le=120)):
        """Analyse the pre-scan's candidates properly, and score their discovery.

        This is the only part of the hunter that spends requests: four klines per
        symbol not already covered by the classic scan. The report says exactly
        what it cost, because a search that quietly spent hundreds of requests
        would be discovered as a rate-limit ban rather than as a number.

        Returns two scores per token, deliberately side by side and never blended:
        TOKEN_DISCOVERY_SCORE ("has its behaviour just changed?") and the ordinary
        Opportunity Score ("is this a good setup?").
        """
        service = get_service()
        if not service.last_prescan:
            return JSONResponse(
                status_code=503,
                content={
                    "reason": "NO_PRESCAN_YET",
                    "message": "The deep scan analyses the pre-scan's candidates. "
                               "Run a scan first so the wide search has something to rank.",
                },
            )
        report = await service.deep_scan(max_symbols=max_symbols)
        return {
            "deep_scan": report.to_dict(),
            "data_mode": "DEMO" if service.status()["synthetic_data"] else "LIVE",
            "engine": {
                "version": service.deep.engine.__class__.__name__,
                "discovery_engine": DISCOVERY_ENGINE_VERSION,
                "weights_fingerprint": service.deep.engine.weights_fingerprint,
                "weights": DISCOVERY_WEIGHTS,
            },
            "disclaimer": (
                "Discovery ranks how much a token's behaviour has changed. It is a 0-100 "
                "ranking, not a probability, and these weights have never been validated "
                "against outcomes."
            ),
        }

    # ------------------------------------------------------------- pumps #

    @app.get("/api/pumps/{symbol}", tags=["pumps"])
    async def pump_history(symbol: str, bars: int = Query(1000, ge=100, le=1000)):
        """This token's past accelerations, and whether the present resembles them.

        Detected on 1h candles: the shortest timeframe whose 1000-bar window
        (41 days) produces a sample large enough to report a rate at all. The
        cost is timing resolution — "time to peak" is known to the hour, and
        every episode carries `resolution_minutes` so nothing renders precision
        it does not have.

        Below the sample floor the similarity block returns no rates at all
        rather than percentages over a handful of observations. On a few weeks
        of history that is the normal outcome, not an error.
        """
        try:
            return await get_service().pump_history(symbol, bars=bars)
        except Exception as exc:
            raise HTTPException(
                502, f"could not read history for {symbol.upper()}: {type(exc).__name__}"
            ) from exc

    # ---------------------------------------------------------- maintenance #

    @app.post("/api/maintenance/prune", tags=["maintenance"])
    async def prune_now():
        """Apply the retention window immediately and report what was removed.

        A signal is never pruned while it still owes a verdict or a horizon
        window, however old it is — deleting those would remove exactly the rows
        about to become evidence.
        """
        removed = await get_service().maybe_prune(force=True)
        return {"removed": removed, "held_back_unsettled": repo.retained_but_unsettled()}

    # ------------------------------------------------------------- frontend #

    if FRONTEND_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")
        if (FRONTEND_DIST / "icons").is_dir():
            app.mount("/icons", StaticFiles(directory=FRONTEND_DIST / "icons"), name="icons")

        # --- PWA -------------------------------------------------------- #
        # These must be served before the SPA catch-all and with their real
        # content types. A service worker delivered as text/html is silently
        # rejected by the browser, and the app simply never becomes installable
        # with no error anywhere to explain why.

        @app.get("/manifest.webmanifest", include_in_schema=False)
        async def manifest():
            path = FRONTEND_DIST / "manifest.webmanifest"
            if not path.is_file():
                raise HTTPException(404, "manifest not built — run npm run build")
            return FileResponse(path, media_type="application/manifest+json")

        @app.get("/sw.js", include_in_schema=False)
        async def service_worker():
            path = FRONTEND_DIST / "sw.js"
            if not path.is_file():
                raise HTTPException(404, "service worker not built — run npm run build")
            return FileResponse(
                path,
                media_type="application/javascript",
                headers={
                    # Lets a worker served from /sw.js control the whole origin.
                    "Service-Worker-Allowed": "/",
                    # The worker is the one file that must never be cached by the
                    # browser: a stale copy would pin an old shell forever.
                    "Cache-Control": "no-cache",
                },
            )

        @app.get("/robots.txt", include_in_schema=False)
        async def robots():
            path = FRONTEND_DIST / "robots.txt"
            if not path.is_file():
                raise HTTPException(404, "not built")
            return FileResponse(path, media_type="text/plain")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(FRONTEND_DIST / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(404, "unknown API route")
            return FileResponse(FRONTEND_DIST / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        async def no_frontend():
            return {
                "message": (
                    f"{get_settings().app_name} API is running. The dashboard bundle is not built. "
                    "Run: cd frontend && npm install && npm run build"
                ),
                "docs": "/docs",
                "health": "/api/health",
            }

    return app


app = create_app()
