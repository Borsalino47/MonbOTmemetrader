"""Application service: owns the scanner, the loop and the shared state.

One instance per process. The FastAPI routes read from it; the background loop
writes to it. `asyncio` gives us a single-threaded event loop, so the only
concurrency hazard is the database work, which is pushed to a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC

from cryptopulse.alerts.engine import Alert, AlertEngine, AlertKind, AlertLevel
from cryptopulse.alerts.notifiers import NotifierHub
from cryptopulse.backtest.labels import label_config_by_name
from cryptopulse.config.settings import CryptoPulseSettings, get_settings
from cryptopulse.core.clock import SYSTEM_CLOCK
from cryptopulse.core.logging import get_logger
from cryptopulse.database import repo
from cryptopulse.database.session import init_engine
from cryptopulse.outcomes.tracker import OutcomeTracker, ResolutionReport
from cryptopulse.providers.registry import is_synthetic
from cryptopulse.scanner.base import ScanReport
from cryptopulse.scanner.cex import CexScanner
from cryptopulse.scanner.memory import ScoreMemory
from cryptopulse.scoring.engine import ScoreResult
from cryptopulse.scoring.moonshot import MOONSHOT_ENGINE_VERSION

log = get_logger("api.service")

__all__ = ["ScannerService", "get_service", "set_service"]


class ScannerService:
    def __init__(self, settings: CryptoPulseSettings | None = None) -> None:
        self.settings = settings or get_settings()
        self.memory = ScoreMemory()
        self.scanner = CexScanner(self.settings, memory=self.memory, clock=SYSTEM_CLOCK)
        self.alerts = AlertEngine(self.settings.alerts, self.settings.scoring)
        # Delivery is part of the service, not of the CLI, so the API's own scan
        # loop notifies exactly as `cryptopulse radar` does. One code path.
        self.notifiers = NotifierHub.from_settings(self.settings.alerts)
        self.last_report: ScanReport | None = None
        self.last_alerts: list[Alert] = []
        self.scan_count = 0
        self.consecutive_failures = 0
        self.last_success_ms: int | None = None
        self.started_at_ms = SYSTEM_CLOCK.now_ms()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._db_ready = False
        self._memory_rehydrated = False
        self._last_maintenance_ms = 0
        self._watchdog_fired = False

        # The outcome trackers share the scanner's provider: same feed, same
        # rate-limit budget, same circuit breaker. Two of them, because the two
        # axes are graded against different labels on different timeframes.
        self.tracker = OutcomeTracker(self.settings, self.scanner.provider, clock=SYSTEM_CLOCK)
        self.moon_tracker = OutcomeTracker(
            self.settings,
            self.scanner.provider,
            label_config=label_config_by_name(self.settings.moonshot.label_config),
            clock=SYSTEM_CLOCK,
        )
        self.last_resolution: ResolutionReport | None = None
        self.last_moon_resolution: ResolutionReport | None = None
        self._resolve_lock = asyncio.Lock()
        self._moon_resolve_lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------- #

    def ensure_db(self) -> None:
        if self._db_ready:
            return
        init_engine(self.settings.database)
        self._db_ready = True
        self._rehydrate_memory()

    def _rehydrate_memory(self) -> None:
        """Reload recent score points so a restart does not blind the ranker.

        Score acceleration is a difference between two passes. A freshly started
        process has one pass, so every asset looks flat — and "the score is
        rising" is exactly the signal this product is built around. Reading the
        last window back from disk costs one query at startup.
        """
        if self._memory_rehydrated:
            return
        self._memory_rehydrated = True
        window_ms = max(self.memory.window_ms * 4, 6 * 3_600_000)
        try:
            points = repo.recent_score_points(SYSTEM_CLOCK.now_ms() - window_ms)
            loaded = self.memory.rehydrate(points)
            if loaded:
                log.info("score_memory_rehydrated", points=loaded, symbols=len({p["symbol"] for p in points}))
        except Exception as exc:
            # A cold memory is a degraded start, not a failed one.
            log.warning("score_memory_rehydrate_failed", error=str(exc)[:200])

    async def start(self) -> None:
        self.ensure_db()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="scan-loop")
            log.info("scan_loop_started", interval_s=self.settings.scanner.scan_interval_seconds)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.scanner.close()
        await self.notifiers.close()
        log.info("service_stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The loop must survive anything a scan can throw.
                self.consecutive_failures += 1
                log.error("scan_loop_error", error=str(exc)[:300], consecutive=self.consecutive_failures)

            # Outside the try: a scan that *raised* is exactly the case the
            # watchdog exists for, so checking it only on the success path would
            # leave it silent during the worst failure.
            try:
                await self.check_watchdog()
            except Exception as exc:
                log.error("watchdog_failed", error=str(exc)[:300])

            await asyncio.sleep(self.settings.scanner.scan_interval_seconds)

    # -- scanning ------------------------------------------------------------ #

    async def run_once(self) -> ScanReport:
        """One full pass: scan, alert, persist. Safe to call concurrently."""
        async with self._lock:
            self.ensure_db()
            report = await self.scanner.scan()
            self.last_report = report
            self.scan_count += 1

            if report.succeeded == 0 and report.errors:
                self.consecutive_failures += 1
            else:
                self.consecutive_failures = 0
                self.last_success_ms = SYSTEM_CLOCK.now_ms()

            now_ms = SYSTEM_CLOCK.now_ms()
            alerts = self.alerts.evaluate(report.results, now_ms)
            self.last_alerts = alerts

            # Database work off the event loop.
            regime = self.scanner.regime.trend.value
            provider = self.scanner.provider.name
            try:
                written = await asyncio.to_thread(
                    repo.persist_scan,
                    report,
                    provider=provider,
                    regime=regime,
                    moonshot_journal_min_score=self.settings.moonshot.journal_min_score,
                )
                if alerts:
                    await asyncio.to_thread(repo.persist_alerts, alerts)
                log.info("scan_persisted", signals=written, alerts=len(alerts))
            except Exception as exc:
                log.error("persist_failed", error=str(exc)[:300])

            # Delivery runs inside the lock but after persistence: an alert is on
            # disk before anyone is told about it, so a delivery crash can never
            # lose the record. `dispatch` is documented never to raise.
            if alerts:
                await self.notifiers.dispatch(alerts)

        # Outside the scan lock: resolution is independent of scanning and must
        # not delay the next pass if the provider is slow.
        try:
            await self.resolve_outcomes()
        except Exception as exc:
            log.error("resolution_failed", error=str(exc)[:300])

        if self.settings.moonshot.enabled:
            try:
                await self.resolve_moonshot_outcomes()
            except Exception as exc:
                log.error("moonshot_resolution_failed", error=str(exc)[:300])

        try:
            await self.run_maintenance()
        except Exception as exc:
            log.error("maintenance_failed", error=str(exc)[:300])

        return report

    # -- keeping the process alive over weeks -------------------------------- #

    async def run_maintenance(self, force: bool = False) -> int:
        """Housekeeping that must happen periodically, not every scan.

        Only score points are purged. Signals are the evidence the project
        exists to accumulate and a ×10 label can take 180 days to settle, so
        deleting them on a retention timer would throw away rows before they
        could ever be graded — see `repo.purge_older_than`.
        """
        now_ms = SYSTEM_CLOCK.now_ms()
        if not force and (now_ms - self._last_maintenance_ms) < 6 * 3_600_000:
            return 0
        self._last_maintenance_ms = now_ms
        cutoff = now_ms - self.settings.database.retention_days * 86_400_000
        purged = await asyncio.to_thread(repo.purge_older_than, cutoff)
        if purged:
            log.info("score_points_purged", rows=purged, retention_days=self.settings.database.retention_days)
        return purged

    def watchdog_deadline_seconds(self) -> int:
        """How long without a successful scan counts as "the radar has stopped"."""
        configured = self.settings.alerts.watchdog_after_seconds
        return configured if configured > 0 else 5 * self.settings.scanner.scan_interval_seconds

    async def check_watchdog(self) -> Alert | None:
        """Say out loud when the radar has stopped working — and when it recovers.

        Fires once per outage rather than every cycle: an alert that repeats every
        minute is one you turn off, and then you have no watchdog at all.
        """
        if not self.settings.alerts.watchdog_enabled:
            return None

        health = self.health_status()
        if health["status"] != "DOWN":
            if self._watchdog_fired:
                self._watchdog_fired = False
                recovery = self._system_alert(
                    "RADAR RECOVERED", ["scanning again after an outage"], AlertLevel.INFO
                )
                await self.notifiers.dispatch([recovery])
                return recovery
            return None

        if self._watchdog_fired:
            return None  # already told you; saying it again teaches you to ignore it
        self._watchdog_fired = True
        alert = self._system_alert("RADAR IS NOT SCANNING", health["reasons"], AlertLevel.CRITICAL_SETUP)
        log.error("watchdog_tripped", reasons=health["reasons"])
        await self.notifiers.dispatch([alert])
        return alert

    def _system_alert(self, headline: str, reasons: list[str], level: AlertLevel) -> Alert:
        now_ms = SYSTEM_CLOCK.now_ms()
        return Alert(
            symbol="SYSTEM",
            kind=AlertKind.SYSTEM,
            level=level,
            headline=headline,
            timestamp_ms=now_ms,
            final_score=0.0,
            pump_maturity=0.0,
            data_confidence=0.0,
            safety=0.0,
            liquidity="UNKNOWN",
            state="SYSTEM",
            price=0.0,
            score_acceleration=None,
            why=reasons,
            risks=[],
            dedup_key=f"system-{headline}",
        )

    def health_status(self) -> dict:
        """Is the radar actually working? Three states, each with its reason.

        Used by `/api/health` for its HTTP status, by container health checks,
        and by the watchdog. Deliberately blunt: a scanner that has not completed
        a pass is DOWN even if the process is alive and the dashboard renders.
        """
        now_ms = SYSTEM_CLOCK.now_ms()
        reasons: list[str] = []
        report = self.last_report

        if self.scan_count == 0:
            age = (now_ms - self.started_at_ms) / 1000
            if age < max(120, self.settings.scanner.scan_interval_seconds * 2):
                return {"status": "STARTING", "reasons": ["no scan has completed yet"], "since_success_seconds": None}
            return {
                "status": "DOWN",
                "reasons": [f"no scan has completed in the {age:.0f}s since startup"],
                "since_success_seconds": None,
            }

        since = None if self.last_success_ms is None else (now_ms - self.last_success_ms) / 1000
        deadline = self.watchdog_deadline_seconds()

        if since is None or since > deadline:
            reasons.append(
                f"no successful scan in {since:.0f}s" if since is not None else "no scan has ever succeeded"
            )
        if self.consecutive_failures >= 3:
            reasons.append(f"{self.consecutive_failures} consecutive failed scans")
        if reasons:
            return {"status": "DOWN", "reasons": reasons, "since_success_seconds": since}

        if self.consecutive_failures:
            reasons.append(f"{self.consecutive_failures} consecutive failed scan(s)")
        if report and report.failed:
            reasons.append(f"{report.failed} asset(s) failed in the last scan")
        if report and not all(h.available for h in report.provider_health):
            reasons.append("a data source reported itself unavailable")
        status = self.status()
        last_scan = status.get("last_scan") or {}
        if last_scan.get("data_stale"):
            reasons.append(f"market data is {last_scan.get('market_data_age_seconds')}s old")

        return {
            "status": "DEGRADED" if reasons else "OK",
            "reasons": reasons or ["scanning normally"],
            "since_success_seconds": since,
        }

    async def resolve_outcomes(self, limit: int = 300) -> ResolutionReport:
        """Grade signals whose horizon has elapsed. Safe to call concurrently."""
        async with self._resolve_lock:
            self.ensure_db()
            pending = await asyncio.to_thread(repo.pending_signals, self.tracker.ready_before_ms(), limit)
            if not pending:
                report = ResolutionReport(label_config=self.tracker.label.name)
                self.last_resolution = report
                return report

            report = await self.tracker.resolve(pending)
            if report.resolutions:
                written = await asyncio.to_thread(repo.save_resolutions, report.resolutions)
                log.info("outcomes_persisted", written=written)
            self.last_resolution = report
            return report

    async def resolve_moonshot_outcomes(self, limit: int = 300) -> ResolutionReport:
        """Grade ×10 readings whose horizon has elapsed.

        Separate from `resolve_outcomes` in every respect: its own label, its own
        timeframe, its own columns and its own lock. A signal graded on the
        intraday axis is still pending on this one, which is the point.
        """
        async with self._moon_resolve_lock:
            self.ensure_db()
            pending = await asyncio.to_thread(
                repo.pending_moonshot_signals, self.moon_tracker.ready_before_ms(), limit
            )
            if not pending:
                report = ResolutionReport(label_config=self.moon_tracker.label.name)
                self.last_moon_resolution = report
                return report

            report = await self.moon_tracker.resolve(pending)
            if report.resolutions:
                written = await asyncio.to_thread(repo.save_moonshot_resolutions, report.resolutions)
                log.info("moonshot_outcomes_persisted", written=written)
            self.last_moon_resolution = report
            return report

    # -- reads --------------------------------------------------------------- #

    def results(self) -> list[ScoreResult]:
        return self.last_report.results if self.last_report else []

    def find(self, symbol: str) -> ScoreResult | None:
        return next((r for r in self.results() if r.symbol == symbol.upper()), None)

    def status(self) -> dict:
        report = self.last_report
        now_ms = SYSTEM_CLOCK.now_ms()
        synthetic = is_synthetic(self.scanner.provider)

        # Age of the newest closed candle on the primary timeframe, taken as the
        # median across assets so one lagging symbol does not misrepresent the feed.
        data_age = None
        data_stale = False
        if report and report.results:
            ages = sorted(
                r.confidence.max_age_seconds for r in report.results if r.confidence.max_age_seconds is not None
            )
            if ages:
                data_age = round(ages[len(ages) // 2], 1)
                # One timeframe-length of lag is normal; beyond the configured
                # tolerance on top of that, the feed is genuinely behind.
                budget = self.settings.scanner.stale_after_seconds + self.settings.scanner.primary_timeframe.seconds
                data_stale = data_age > budget

        return {
            "app": self.settings.app_name,
            "environment": self.settings.environment,
            "paper_mode": self.settings.paper_mode,
            "engine_version": self.settings.scoring.engine_version,
            "provider": self.scanner.provider.name,
            "synthetic_data": synthetic,
            "synthetic_warning": (
                "The active data source generates synthetic candles. Nothing displayed is market data."
                if synthetic
                else None
            ),
            "scan_count": self.scan_count,
            "consecutive_failures": self.consecutive_failures,
            "scan_interval_seconds": self.settings.scanner.scan_interval_seconds,
            "market_regime": self.scanner.regime.to_dict(),
            "universe": {
                "mode": self.settings.scanner.universe,
                "benchmark": self.scanner.benchmark_symbol,
                "rank_mode": self.settings.scanner.rank_mode,
                **(
                    self.scanner.universe_resolution.to_dict()
                    if self.scanner.universe_resolution is not None
                    else {}
                ),
            },
            "moonshot": {
                "enabled": self.settings.moonshot.enabled,
                "engine_version": MOONSHOT_ENGINE_VERSION,
                "timeframe": self.settings.moonshot.timeframe.value,
                "target_multiple": self.settings.moonshot.target_multiple,
                "valuation_source": self.settings.providers.valuation,
                "candidates_last_scan": sum(
                    1 for r in (report.results if report else []) if r.moonshot and r.moonshot.is_candidate
                ),
                "grading": {
                    "label_config": self.moon_tracker.label.name,
                    "definition": self.moon_tracker.label.describe(),
                    "resolves_signals_older_than": _iso_or_none(self.moon_tracker.ready_before_ms()),
                    "last_run": (
                        self.last_moon_resolution.to_dict() if self.last_moon_resolution else None
                    ),
                },
            },
            "alert_delivery": {
                "channels": self.notifiers.describe(),
                "last_results": [d.to_dict() for d in self.notifiers.last_results],
            },
            "last_scan": (
                {
                    "started_at_ms": report.started_at_ms,
                    "finished_at_ms": report.finished_at_ms,
                    "age_seconds": round((now_ms - report.finished_at_ms) / 1000, 1),
                    "duration_ms": report.duration_ms,
                    "universe_size": report.universe_size,
                    "scanned": report.scanned,
                    "succeeded": report.succeeded,
                    "failed": report.failed,
                    "market_data_age_seconds": data_age,
                    "data_stale": data_stale,
                    "notes": report.notes,
                }
                if report
                else None
            ),
            "provider_health": [h.to_dict() for h in (report.provider_health if report else [])],
            # How much of the request budget the candle cache is saving. A closed
            # bar cannot change, so a low hit rate here means requests are being
            # spent on data that was already known.
            "candle_cache": self.scanner.cache_stats(),
            "outcome_tracker": {
                "label_config": self.tracker.label.name,
                "definition": self.tracker.label.describe(),
                "horizon_bars": self.tracker.label.horizon_bars,
                "resolves_signals_older_than": _iso_or_none(self.tracker.ready_before_ms()),
                "last_run": self.last_resolution.to_dict() if self.last_resolution else None,
            },
            "server_time_ms": now_ms,
        }


_service: ScannerService | None = None


def get_service() -> ScannerService:
    global _service
    if _service is None:
        _service = ScannerService()
    return _service


def set_service(service: ScannerService | None) -> None:
    """Test hook: inject a service built on a fixture provider."""
    global _service
    _service = service


def _iso_or_none(ms: int | None) -> str | None:
    if ms is None:
        return None
    from datetime import datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
