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
from cryptopulse.core.logging import configure_logging, get_logger
from cryptopulse.database import repo

log = get_logger("api")

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


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
            return JSONResponse(
                status_code=503,
                content={
                    "reason": "NO_SCAN_YET",
                    "message": "No scan has completed. Call POST /api/scan/run or wait for the loop.",
                },
            )

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

    # ------------------------------------------------------------- frontend #

    if FRONTEND_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

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
