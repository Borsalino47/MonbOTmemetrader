"""FastAPI application.

Every response that carries market-derived numbers also carries their age and
their source. `/api/health` is the contract the dashboard uses to decide whether
to show a "data is stale" banner — the front end never has to guess.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from cryptopulse.api.service import get_service
from cryptopulse.config.settings import get_settings
from cryptopulse.core.clock import SYSTEM_CLOCK
from cryptopulse.core.logging import configure_logging, get_logger
from cryptopulse.database import repo
from cryptopulse.scoring.discovery import DISCOVERY_ENGINE_VERSION
from cryptopulse.scoring.discovery import WEIGHTS as DISCOVERY_WEIGHTS

log = get_logger("api")

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


class ValidationRequest(BaseModel):
    """One decision, plus whatever context the client was showing.

    Every context field is optional and defaults to None rather than to a
    number. A decision taken from a compact list view legitimately does not
    carry a discovery score, and recording a zero there would later read as
    "the user validated a token the discovery engine rated at zero" — a
    statement nobody made.
    """

    symbol: str
    decision: str = Field(description="VALIDATED / REJECTED / WATCHLIST / ANALYSE")
    signal_timestamp_ms: int | None = None
    signal_id: int | None = None
    price: float | None = None
    final_score: float | None = None
    explosion_score: float | None = None
    discovery_score: float | None = None
    pump_maturity: float | None = None
    data_confidence: float | None = None
    setup_state: str | None = None
    verdict_level: str | None = None
    engine_version: str | None = None
    why: list[str] | None = None
    risks: list[str] | None = None
    trigger: str | None = None
    invalidation: str | None = None
    note: str | None = None
    data_source: str | None = None
    synthetic: bool | None = None


class OpenPositionRequest(BaseModel):
    """"Oui, j'ai acheté." Everything except the symbol is optional.

    Someone who does not want to record what they paid should still get position
    tracking, so the fill details are optional and the observed price is the
    fallback. Which one is in use is reported back as `pnl_basis`.
    """

    symbol: str
    signal_id: int | None = None
    chain: str | None = None
    contract_address: str | None = None
    entry_price: float | None = None
    actual_entry_price: float | None = None
    amount_invested: float | None = None
    quantity: float | None = None
    trigger_price: float | None = None
    invalidation_price: float | None = None


class ClosePositionRequest(BaseModel):
    """"Oui, j'ai vendu." """

    exit_price: float | None = None
    actual_exit_price: float | None = None
    reason: str | None = None


class AnswerRequest(BaseModel):
    taken: bool


def _decision_counts(entries: list[dict], held: list[dict]) -> dict:
    """One count per decision, including the zeroes.

    A missing key renders as a blank rather than a zero, and "no buy signals"
    is a real answer that the screen has to be able to state.
    """
    counts = {a: 0 for a in ("BUY", "HOLD", "WATCH", "REDUCE", "SELL", "AVOID")}
    for row in entries:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    for row in held:
        decision = row.get("current_decision")
        if decision:
            counts[decision] = counts.get(decision, 0) + 1
    return counts


def _merge_position_context(payload: dict, result, signal: dict | None, service) -> dict:
    """Fill an opening from the signal it answers, then from the live scan.

    Order matters. The signal is what the user was actually looking at when they
    decided; the live scan is where the price has drifted to since. Preferring
    the scan would silently reprice the entry to a moment the user never saw.
    """
    if signal is not None:
        for key, source in (
            ("entry_price", "price"),
            ("trigger_price", "trigger_price"),
            ("invalidation_price", "invalidation_price"),
        ):
            if payload.get(key) is None:
                payload[key] = signal.get(source)
        payload.setdefault("entry_opportunity", signal.get("opportunity_score"))
        payload.setdefault("entry_explosion", signal.get("explosion_score"))
        payload.setdefault("entry_discovery", signal.get("discovery_score"))
        payload.setdefault("entry_safety", signal.get("safety_score"))
        payload.setdefault("entry_confidence", signal.get("data_confidence"))
        payload.setdefault("entry_maturity", signal.get("pump_maturity"))
        payload.setdefault("entry_state", signal.get("setup_state"))
        payload.setdefault("entry_regime", signal.get("market_regime"))
        payload.setdefault("entry_reasons", signal.get("reasons"))

    if result is not None:
        d = result.to_dict()
        defaults = {
            "entry_price": d["price"],
            "entry_opportunity": d["final_score"],
            "entry_explosion": (d["explosion"] or {}).get("explosion_score"),
            "entry_safety": d["safety"]["score"],
            "entry_confidence": d["data_confidence"]["score"],
            "entry_maturity": d["pump_maturity"]["score"],
            "entry_rvol": (d.get("metrics") or {}).get("rvol"),
            "entry_state": d["setup"]["state"],
            "entry_reasons": d["why"][:8],
            "trigger_price": (d.get("metrics") or {}).get("resistance"),
            "invalidation_price": (d.get("metrics") or {}).get("support"),
        }
        for key, value in defaults.items():
            if payload.get(key) is None:
                payload[key] = value

    payload["opened_ms"] = SYSTEM_CLOCK.now_ms()
    payload["chain"] = payload.get("chain") or "CEX"
    if payload.get("entry_regime") is None:
        payload["entry_regime"] = service.scanner.regime.trend.value
    payload["data_source"] = service.scanner.provider.name
    payload["synthetic"] = service.status()["data_mode"] == "DEMO"
    return payload


def _merge_validation_context(payload: dict, result) -> dict:
    """Fill blanks from the live scan; never overwrite what the client sent.

    The client's values are what was actually on the user's screen. The server's
    are what the scanner holds now — close, but a scan may have landed between
    the render and the tap. Where both exist the screen wins, because the
    decision was made against the screen.
    """
    d = result.to_dict()
    defaults = {
        "signal_timestamp_ms": d["timestamp_ms"],
        "price": d["price"],
        "final_score": d["final_score"],
        "explosion_score": (d["explosion"] or {}).get("explosion_score"),
        "pump_maturity": d["pump_maturity"]["score"],
        "data_confidence": d["data_confidence"]["score"],
        "setup_state": d["setup"]["state"],
        "verdict_level": (d.get("verdict") or {}).get("level"),
        "engine_version": d["engine_version"],
        "why": d["why"][:12],
        "risks": d["risks"][:12],
        "trigger": d["setup"].get("trigger"),
        "invalidation": next(iter(d["risks"]), None),
    }
    for key, value in defaults.items():
        if payload.get(key) is None:
            payload[key] = value
    return payload


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
        else:
            # Loops off (tests, one-shot runs) still constitutes a ready server,
            # and the timeline must say so — otherwise the first phase of the
            # sequence looks like it never happened.
            service.startup.mark("server_ready_ms")
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

    @app.get("/api/startup", tags=["status"])
    async def startup():
        """How long each phase of the launch took, and where the feed check is.

        Separate from `/api/health` so the dashboard can poll it during the first
        seconds without pulling the whole status payload, and so "why was that
        slow?" has an answer that is a measurement rather than an opinion.
        """
        service = get_service()
        return {
            "startup": service.startup.to_dict(),
            "feed_verification": service.feed.to_dict(),
            "live_verified": service.live_verified,
        }

    @app.post("/api/feed/verify", tags=["status"])
    async def verify_feed_endpoint():
        """Re-run the feed check on demand. Four requests; never blocks a scan."""
        return (await get_service().verify_feed_now()).to_dict()

    # ------------------------------------------------------------- providers #
    # The multi-market surface (spec §47). One namespace, never a second copy
    # of the API: the market is a parameter, not a fork.

    @app.get("/api/providers", tags=["providers"])
    async def providers():
        """Both market universes and where each one stands. The UI switch reads
        this; it never infers a state from a provider name it recognises."""
        service = get_service()
        status = service.status()
        return {
            "providers": [
                {
                    "id": "BINANCE_SPOT",
                    "label": "BINANCE",
                    "emoji": "🟡",
                    "kind": "cex",
                    "state": service.feed.state.value,
                    "state_emoji": service.feed.emoji,
                    "state_label_fr": service.feed.label_fr,
                    "live_verified": service.live_verified,
                    "data_mode": status["data_mode"],
                    "market_data_available": True,
                },
                {
                    "id": "ROBINHOOD_CHAIN",
                    "label": "ROBINHOOD",
                    "emoji": "🟣",
                    "kind": "onchain",
                    "chain_id": service.robinhood_status()["chain_id"],
                    "state": service.robinhood_chain.state.value,
                    "state_emoji": service.robinhood_chain.emoji,
                    "state_label_fr": service.robinhood_chain.label_fr,
                    "live_verified": service.robinhood_chain.verified,
                    "market_data_available": False,
                },
            ],
        }

    @app.get("/api/provider/binance/status", tags=["providers"])
    async def binance_status():
        service = get_service()
        return {
            "provider": "BINANCE_SPOT",
            "verification": service.feed.to_dict(),
            "live_verified": service.live_verified,
        }

    @app.get("/api/provider/robinhood/status", tags=["providers"])
    async def robinhood_status():
        """Chain status, PENDING-first. The first call after a switch answers
        instantly and kicks the doctor in the background — the ROBINHOOD tab
        must appear as fast as the BINANCE one (spec §41)."""
        service = get_service()
        service.ensure_robinhood_verification()
        return service.robinhood_status()

    @app.post("/api/provider/robinhood/verify", tags=["providers"])
    async def robinhood_verify():
        """Run the chain doctor now and wait for its verdict (the retry button)."""
        service = get_service()
        await service.verify_robinhood_now()
        return service.robinhood_status()

    @app.get("/api/robinhood/tokens", tags=["providers"])
    async def robinhood_tokens(bucket: str | None = None, sort: str = "age"):
        """New tokens on Robinhood Chain, newest first.

        Serves the last search and kicks a first one in the background if none
        has run — the same never-block contract as everything else here. The
        response says how many pools it looked at, so the list is never read as
        a census of the chain.
        """
        service = get_service()
        service.ensure_robinhood_discovery()
        report = service.robinhood_discovery
        if report is None:
            return {
                "state": "PENDING",
                "tokens": [],
                "note": "Première recherche en cours.",
            }
        payload = report.to_dict()
        if bucket:
            payload["tokens"] = [t for t in payload["tokens"] if t["age_bucket"] == bucket]
            payload["filtered_to_bucket"] = bucket
        if sort == "early":
            # Spec §33: "top opportunities" ranks by setup quality, not by past
            # gain. Vetoed tokens sink to the bottom rather than being hidden —
            # seeing *why* something was rejected is part of the point.
            payload["tokens"].sort(
                key=lambda t: (
                    not t["hard_veto"],
                    (t["early"] or {}).get("score", 0.0),
                ),
                reverse=True,
            )
            payload["sorted_by"] = "early"
        else:
            payload["sorted_by"] = "age"
        payload["state"] = "OK" if report.candidates else ("FAILED" if report.errors else "EMPTY")
        return payload

    @app.get("/api/robinhood/token/{address}", tags=["providers"])
    async def robinhood_token(address: str):
        """One token, seen by both indexers, with their disagreements named.

        404 rather than an empty shell when the token is not in the current
        search: inventing a blank token page would be a fabrication.
        """
        detail = await get_service().robinhood_token_detail(address)
        if detail is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "Ce contrat n'est pas dans la dernière recherche. "
                    "Relancez une recherche, ou il est hors de la fenêtre d'âge."
                ),
            )
        return detail.to_dict()

    @app.post("/api/robinhood/tokens/search", tags=["providers"])
    async def robinhood_search():
        """Run a search now and wait for it (the explicit refresh button)."""
        report = await get_service().discover_robinhood_now()
        payload = report.to_dict()
        payload["state"] = "OK" if report.candidates else ("FAILED" if report.errors else "EMPTY")
        return payload

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
                "Le score d'opportunité est un classement transparent de 0 à 100 produit par "
                "des pondérations fixes. Il n'a PAS été calibré statistiquement et ne doit pas "
                "être lu comme une probabilité."
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
        # `not report.results` matters as much as `report is None`, and was found
        # by running the app against an unreachable Binance: the scan completed
        # with zero successes, `last_report` stopped being None, and the screen
        # went from a populated journal to blank. A scan that produced nothing
        # has not replaced the last one that produced something.
        if report is None or not report.results:
            # No usable scan from THIS process yet. Rather than an empty screen
            # for the length of a full scan, serve the last one from the journal
            # — labelled with its real age and its own provenance, never as live.
            # Prefer the snapshot the boot sequence already read. On a phone
            # this is the difference between the first screen costing a SQLite
            # query and costing nothing at all — and it is the same rows either
            # way, so there is no honesty cost to the shortcut.
            snap = service.warm_snapshot
            if snap is None or len(snap.get("rows", [])) < limit:
                snap = repo.last_scan_snapshot(limit=limit)
            if snap is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "reason": "NO_SCAN_YET",
                        "message": "Aucun scan n'a jamais abouti et le journal est vide. "
                                   "Lancez un scan ou attendez le cycle automatique.",
                    },
                )
            rows = _filter_snapshot(
                snap["rows"], min_score, max_pump_maturity, state, min_liquidity, premium_only
            )
            return {
                "meta": {
                    "source": "journal",
                    "live": False,
                    # Distinguishes "not scanned yet" from "scanned and the feed
                    # returned nothing", which need different fixes.
                    "reason": (
                        "aucun scan dans ce processus pour l'instant"
                        if report is None
                        else "le dernier scan n'a produit aucun résultat exploitable"
                    ),
                    "stale": True,
                    "age_seconds": snap["age_seconds"],
                    "message": (
                        "Dernier scan enregistré, restauré depuis le journal pendant qu'un "
                        "nouveau tourne. Les colonnes du carnet d'ordres n'ont jamais été "
                        "journalisées et sont donc inconnues."
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
            raise HTTPException(404, f"{symbol.upper()} absent du dernier scan")
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
                    "Aucune fenêtre de vérification n'est encore close pour ce signal. Une "
                    "fenêtre en cours est signalée comme absente plutôt que tranchée au prix "
                    "actuel."
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
                    "message": "La recherche lit l'instantané de marché qu'un scan produit. "
                               "Lancez d'abord un scan, ou attendez le cycle automatique.",
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
                "La priorité classe les tokens qui méritent un examen coûteux. Ce n'est pas "
                "un score, pas une probabilité, et cela ne dit rien sur l'intérêt d'acheter "
                "un token."
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
                    "message": "L'analyse approfondie porte sur les candidats du pré-scan. "
                               "Lancez d'abord un scan pour que la recherche large ait "
                               "quelque chose à classer.",
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
                "La découverte classe l'ampleur du changement de comportement d'un token. "
                "C'est un classement de 0 à 100, pas une probabilité, et ces pondérations "
                "n'ont jamais été validées contre des résultats réels."
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

    # -------------------------------------------------------- validations #

    @app.post("/api/validations", tags=["validations"])
    async def record_validation(body: ValidationRequest):
        """Record what the user decided about a token, and what was on screen.

        The context is copied in rather than joined to the signal table. That is
        deliberate: a token in IGNORE has no signal row to join to, an old scan
        may have been pruned by the time the decision is analysed, and a later
        engine version would re-explain a past decision in words the user never
        read. The row is a photograph of the moment, not a pointer to it.

        Append-only. Changing one's mind writes a second row, because a sequence
        of decisions is the thing worth studying and flattening it to a final
        state would erase it.
        """
        service = get_service()
        payload = body.model_dump()

        # Fill anything the client did not send from the live scan, so a
        # decision taken from a list view still carries the full context. What
        # cannot be found stays None — never zero, never a guess.
        result = service.find(body.symbol)
        if result is not None:
            payload = _merge_validation_context(payload, result)

        # `setdefault` would not fire here: the request model carries these keys
        # with an explicit None, so the test is on the value rather than the key.
        if payload.get("data_source") is None:
            payload["data_source"] = service.scanner.provider.name
        if payload.get("synthetic") is None:
            payload["synthetic"] = service.status()["data_mode"] == "DEMO"

        service.ensure_db()
        try:
            row = await asyncio.to_thread(repo.save_validation, payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            # A decision the user believes was recorded, and was not, is worse
            # than a refused one. Reported as a named failure rather than as a
            # bare 500, so the screen can say what happened.
            log.error("validation_write_failed", symbol=body.symbol, error=type(exc).__name__)
            raise HTTPException(
                503, f"la décision n'a pas pu être enregistrée ({type(exc).__name__})"
            ) from exc
        return row

    @app.get("/api/validations", tags=["validations"])
    async def list_validations(
        limit: int = Query(100, ge=1, le=500),
        symbol: str | None = None,
        decision: str | None = None,
    ):
        """The decision history, newest first, plus the current state per symbol."""
        service = get_service()
        service.ensure_db()
        rows = await asyncio.to_thread(
            repo.recent_validations, limit, symbol=symbol, decision=decision
        )
        summary = await asyncio.to_thread(repo.validation_counts)
        return {
            "validations": rows,
            **summary,
            "decisions": list(repo.VALID_DECISIONS),
            "note": (
                "Ce sont vos propres décisions, enregistrées avec l'état de l'écran qui les "
                "a produites. Les décisions prises en mode DÉMO sont comptées séparément et "
                "ne doivent jamais être mélangées aux réelles."
            ),
        }

    @app.get("/api/validations/current", tags=["validations"])
    async def current_validations(limit: int = Query(300, ge=1, le=1000)):
        """The latest decision per symbol, for marking rows on screen.

        Derived from the append-only history rather than stored, so the badge on
        a row can never disagree with the record behind it.
        """
        service = get_service()
        service.ensure_db()
        return {"current": await asyncio.to_thread(repo.latest_validation_per_symbol, limit)}

    # ------------------------------------------------------------- decisions #

    @app.get("/api/decisions", tags=["trading"])
    async def decisions(limit: int = Query(60, ge=1, le=300)):
        """What to do right now, about everything and about what you hold.

        Two lists rather than one merged ranking. A 🟢 ACHETER and a 🔴 VENDRE
        are instructions to different halves of a portfolio and sorting them
        together would bury whichever happened to score lower — the sell in
        particular, since a position being exited rarely has a high score.
        """
        service = get_service()
        service.ensure_db()

        entries = service.last_decisions[:limit]
        try:
            held = await asyncio.to_thread(repo.positions, "OPEN", 200)
            unanswered = await asyncio.to_thread(repo.unanswered_trade_signals, 20)
        except Exception:
            held, unanswered = [], []

        return {
            "entries": entries,
            "positions": held,
            # Prompts still waiting for OUI / NON. Surfaced here so an
            # unanswered recommendation cannot quietly disappear off the screen.
            "awaiting_answer": unanswered,
            "counts": _decision_counts(entries, held),
            "data_mode": service.status()["data_mode"],
            # Whether a *new* decision here may be presented as verified live
            # data. False while the feed check is pending as well as on failure:
            # "not yet checked" and "checked and wrong" are different states,
            # and neither licenses showing a fresh BUY as a live signal.
            "live_verified": service.live_verified,
            "feed_verification": service.feed.to_dict(),
            "engine": {
                "version": "TRADE_DECISION_V1",
                "weights_fingerprint": service.trade_engine.weights_fingerprint,
            },
            "disclaimer": (
                "Aucun ordre n'est jamais passé automatiquement. L'application analyse "
                "et recommande ; vous exécutez vous-même et vous confirmez ensuite."
            ),
        }

    # ------------------------------------------------------------- positions #

    @app.get("/api/positions", tags=["trading"])
    async def list_positions(status: str = Query("OPEN"), limit: int = Query(100, ge=1, le=500)):
        service = get_service()
        service.ensure_db()
        wanted = None if status.upper() == "ALL" else status.upper()
        rows = await asyncio.to_thread(repo.positions, wanted, limit)
        return {
            "positions": rows,
            "watcher": service.watcher.status(),
            "data_mode": service.status()["data_mode"],
        }

    @app.get("/api/positions/{position_id}/events", tags=["trading"])
    async def position_history(position_id: int):
        """Every decision change, in order — the engine's reasoning made visible."""
        service = get_service()
        service.ensure_db()
        position = await asyncio.to_thread(repo.position_by_id, position_id)
        if position is None:
            raise HTTPException(404, f"aucune position {position_id}")
        events = await asyncio.to_thread(repo.position_events, position_id)
        return {"position": position, "events": events}

    @app.post("/api/positions/open", tags=["trading"])
    async def open_a_position(body: OpenPositionRequest):
        """"Oui, j'ai acheté." Creates the position and starts watching it.

        Opening is only ever a user action. Nothing in this application opens a
        position on its own, and no code path here can place an order.
        """
        service = get_service()
        service.ensure_db()
        payload = body.model_dump()

        result = service.find(body.symbol)
        signal = None
        if body.signal_id is not None:
            try:
                signal = await asyncio.to_thread(
                    repo.answer_trade_signal, body.signal_id, True
                )
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

            # Found by opening a position with the wrong signal id: without this
            # check the merge below happily imported another token's entry price
            # and invalidation level, and the position was then judged for its
            # whole life against a level from a different chart.
            if signal["symbol"] != body.symbol.upper():
                raise HTTPException(
                    400,
                    f"le signal {body.signal_id} concerne {signal['symbol']}, "
                    f"pas {body.symbol.upper()}",
                )

        payload = _merge_position_context(payload, result, signal, service)
        if payload.get("entry_price") is None:
            raise HTTPException(
                400,
                f"aucun prix connu pour {body.symbol.upper()} — indiquez `entry_price`, "
                "ou attendez que le scanner l'ait analysé",
            )

        try:
            position = await asyncio.to_thread(repo.open_position, payload)
        except Exception as exc:
            log.error("open_position_failed", symbol=body.symbol, error=type(exc).__name__)
            raise HTTPException(
                503, f"la position n'a pas pu être enregistrée ({type(exc).__name__})"
            ) from exc

        if signal is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    repo.answer_trade_signal, body.signal_id, True, position_id=position["id"]
                )

        # A fresh position starts with a clean decision memory, so it is not
        # judged against the state a previous position in the same token left.
        service.watcher.forget(position["symbol"])
        return position

    @app.post("/api/positions/{position_id}/close", tags=["trading"])
    async def close_a_position(position_id: int, body: ClosePositionRequest):
        """"Oui, j'ai vendu." Closes the position and stops watching it.

        The exit price falls back to what the scanner currently observes when
        the user does not supply their fill — stated in `pnl_basis`, because a
        return computed from a screen price is a different number from one
        computed from a fill.
        """
        service = get_service()
        service.ensure_db()

        existing = await asyncio.to_thread(repo.position_by_id, position_id)
        if existing is None:
            raise HTTPException(404, f"aucune position {position_id}")

        observed = existing.get("last_price")
        result = service.find(existing["symbol"])
        if result is not None:
            observed = result.price
        exit_price = body.actual_exit_price or body.exit_price or observed
        if exit_price is None:
            raise HTTPException(
                400, "aucun prix de sortie connu — indiquez `exit_price`"
            )

        closed = await asyncio.to_thread(
            repo.close_position,
            position_id,
            exit_price=float(exit_price),
            actual_exit_price=body.actual_exit_price,
            now_ms=SYSTEM_CLOCK.now_ms(),
            reason=body.reason,
        )
        service.watcher.forget(closed["symbol"])
        return closed

    # --------------------------------------------------------- trade signals #

    @app.get("/api/trade-signals", tags=["trading"])
    async def list_trade_signals(
        limit: int = Query(100, ge=1, le=500),
        action: str | None = None,
        taken: bool | None = None,
    ):
        service = get_service()
        service.ensure_db()
        rows = await asyncio.to_thread(repo.recent_trade_signals, limit, action=action, taken=taken)
        return {
            "signals": rows,
            "note": (
                "`taken` vaut null tant que la question n'a pas reçu de réponse. "
                "Ce n'est pas un « non » : un signal sans réponse n'est pas un signal refusé."
            ),
        }

    @app.post("/api/trade-signals/{signal_id}/answer", tags=["trading"])
    async def answer_a_signal(signal_id: int, body: AnswerRequest):
        """"Avez-vous acheté ?" — OUI or NON.

        A NON is recorded and then followed exactly like a OUI. The signals
        someone skipped are the ones they were unsure about, and dropping them
        would make every later statistic flattering.
        """
        service = get_service()
        service.ensure_db()
        try:
            return await asyncio.to_thread(repo.answer_trade_signal, signal_id, body.taken)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/positions/watch", tags=["trading"])
    async def watch_now():
        """Run one position-watch pass on demand, and say what it cost."""
        service = get_service()
        service.ensure_db()
        return (await service.watcher.run_once()).to_dict()

    @app.get("/api/results", tags=["trading"])
    async def results():
        """Mes résultats: what was decided, and what followed.

        Two questions kept apart: did the engine recommend well, and did the
        user do well. They are not the same number — someone can follow good
        signals badly or ignore them profitably — and the comparison between
        them only works because refused signals are kept and followed.
        """
        from cryptopulse.trading.stats import build_trading_performance

        service = get_service()
        service.ensure_db()
        closed = await asyncio.to_thread(repo.positions, "CLOSED", 500)
        opened = await asyncio.to_thread(repo.positions, None, 500)
        signals = await asyncio.to_thread(repo.recent_trade_signals, 500)

        report = build_trading_performance(opened, signals)
        report["closed_positions"] = closed[:50]
        report["data_mode"] = service.status()["data_mode"]
        return report

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
            # The moment the browser is actually handed something to render.
            # Recorded once — a later reload would otherwise overwrite the launch
            # timing with a number describing a page refresh.
            with contextlib.suppress(Exception):
                get_service().startup.mark("ui_ready_ms")
            return FileResponse(FRONTEND_DIST / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(404, "unknown API route")
            with contextlib.suppress(Exception):
                get_service().startup.mark("ui_ready_ms")
            return FileResponse(FRONTEND_DIST / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        async def no_frontend():
            return {
                "message": (
                    f"L'API {get_settings().app_name} fonctionne. L'interface n'est pas "
                    "compilée. Lancez : cd frontend && npm install && npm run build"
                ),
                "docs": "/docs",
                "health": "/api/health",
            }

    return app


app = create_app()
