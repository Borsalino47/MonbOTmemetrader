"""Application service: owns the scanner, the loop and the shared state.

One instance per process. The FastAPI routes read from it; the background loop
writes to it. `asyncio` gives us a single-threaded event loop, so the only
concurrency hazard is the database work, which is pushed to a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC

from cryptopulse.alerts.delivery import AlertDelivery, send_alerts
from cryptopulse.alerts.engine import Alert, AlertEngine
from cryptopulse.config.settings import CryptoPulseSettings, get_settings
from cryptopulse.core.clock import SYSTEM_CLOCK
from cryptopulse.core.logging import get_logger
from cryptopulse.database import repo
from cryptopulse.database.session import init_engine
from cryptopulse.outcomes.horizons import HORIZONS, HorizonReport, HorizonTracker
from cryptopulse.outcomes.tracker import OutcomeTracker, ResolutionReport
from cryptopulse.providers.registry import is_synthetic
from cryptopulse.scanner.base import ScanReport
from cryptopulse.scanner.cex import CexScanner
from cryptopulse.scanner.memory import ScoreMemory
from cryptopulse.scoring.engine import ScoreResult

log = get_logger("api.service")

# Retention is bookkeeping. Running it every scan would spend the disk budget on
# delete churn rather than on the journal it is meant to protect.
PRUNE_INTERVAL_MS = 6 * 3600 * 1000

__all__ = ["ScannerService", "get_service", "set_service"]


class ScannerService:
    def __init__(self, settings: CryptoPulseSettings | None = None) -> None:
        self.settings = settings or get_settings()
        self.memory = ScoreMemory()
        self.scanner = CexScanner(self.settings, memory=self.memory, clock=SYSTEM_CLOCK)
        self.alerts = AlertEngine(self.settings.alerts, self.settings.scoring)
        # Delivery is a no-op until CP_ALERT_WEBHOOK_URL is set. The synthetic
        # flag travels with it so a DEMO alert can never read as a live one.
        self.delivery = AlertDelivery(
            webhook_url=self.settings.alerts.webhook_url,
            timeout=self.settings.providers.request_timeout_seconds,
            synthetic=is_synthetic(self.scanner.provider),
        )
        self.last_report: ScanReport | None = None
        self.last_alerts: list[Alert] = []
        self.scan_count = 0
        self.consecutive_failures = 0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._db_ready = False

        # The outcome tracker shares the scanner's provider: same feed, same
        # rate-limit budget, same circuit breaker.
        self.tracker = OutcomeTracker(self.settings, self.scanner.provider, clock=SYSTEM_CLOCK)
        self.last_resolution: ResolutionReport | None = None
        self._resolve_lock = asyncio.Lock()

        # The barrier verdict and the horizon path answer different questions and
        # are tracked separately. Same provider again: one feed, one budget.
        self.horizons = HorizonTracker(self.settings, self.scanner.provider, clock=SYSTEM_CLOCK)
        self.last_horizon_run: HorizonReport | None = None
        self._horizon_lock = asyncio.Lock()

        # Retention runs on a slow clock, not every scan: it is bookkeeping, and
        # deleting on a one-minute loop would spend the disk budget on churn.
        self.last_prune: dict | None = None
        self._last_prune_ms = 0

    # -- lifecycle ----------------------------------------------------------- #

    def ensure_db(self) -> None:
        if not self._db_ready:
            init_engine(self.settings.database)
            self._db_ready = True

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

            now_ms = SYSTEM_CLOCK.now_ms()
            alerts = self.alerts.evaluate(report.results, now_ms)
            self.last_alerts = alerts

            # Database work off the event loop.
            regime = self.scanner.regime.trend.value
            provider = self.scanner.provider.name
            try:
                written = await asyncio.to_thread(repo.persist_scan, report, provider=provider, regime=regime)
                if alerts:
                    await asyncio.to_thread(repo.persist_alerts, alerts)
                log.info("scan_persisted", signals=written, alerts=len(alerts))
            except Exception as exc:
                log.error("persist_failed", error=str(exc)[:300])

            # Notification is the last thing in the cycle and cannot fail it: an
            # unreachable webhook must not cost a scan.
            if alerts and self.delivery.configured:
                await send_alerts(self.delivery, alerts)

        # Outside the scan lock: resolution is independent of scanning and must
        # not delay the next pass if the provider is slow. Each follow-up job is
        # guarded separately — a failure in one must not skip the other.
        try:
            await self.resolve_outcomes()
        except Exception as exc:
            log.error("resolution_failed", error=str(exc)[:300])

        try:
            await self.track_horizons()
        except Exception as exc:
            log.error("horizon_tracking_failed", error=str(exc)[:300])

        try:
            await self.maybe_prune()
        except Exception as exc:
            log.error("retention_failed", error=str(exc)[:300])

        return report

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

    async def track_horizons(self, limit: int = 300) -> HorizonReport:
        """Record what the price did 15m / 1h / 4h / 24h after each signal.

        A signal is revisited until all four windows are stored, so the 24h row
        lands a day after the 15m one without a second schedule.
        """
        async with self._horizon_lock:
            self.ensure_db()
            pending = await asyncio.to_thread(
                repo.signals_needing_horizons, self.horizons.ready_before_ms(), limit
            )
            if not pending:
                report = HorizonReport()
                self.last_horizon_run = report
                return report

            report = await self.horizons.track(pending)
            if report.results:
                written = await asyncio.to_thread(repo.save_horizons, report.results)
                log.info("horizons_persisted", written=written)
            self.last_horizon_run = report
            return report

    async def maybe_prune(self, *, force: bool = False) -> dict | None:
        """Apply the retention window, at most once every six hours."""
        now_ms = SYSTEM_CLOCK.now_ms()
        if not force and now_ms - self._last_prune_ms < PRUNE_INTERVAL_MS:
            return None
        self._last_prune_ms = now_ms
        self.ensure_db()
        removed = await asyncio.to_thread(repo.prune, self.settings.database.retention_days)
        self.last_prune = {**removed, "at_ms": now_ms}
        return self.last_prune

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

        # Before this process has scanned, the dashboard is showing journal rows.
        # Their provenance governs the banner, not the provider configured now:
        # a snapshot written by the synthetic provider stays DEMO on screen even
        # if a real feed is what will answer the next scan.
        snapshot = None
        if report is None:
            try:
                snap = repo.last_scan_snapshot(limit=1)
            except Exception:
                snap = None
            if snap is not None:
                snapshot = {
                    "age_seconds": snap["age_seconds"],
                    "provider": snap["provider"],
                    "synthetic": snap["synthetic"],
                    "data_mode": "DEMO" if snap["synthetic"] else "LIVE",
                    "scanned": snap["scanned"],
                    "succeeded": snap["succeeded"],
                }
                if snap["synthetic"]:
                    synthetic = True

        return {
            "app": self.settings.app_name,
            "environment": self.settings.environment,
            "paper_mode": self.settings.paper_mode,
            "engine_version": self.settings.scoring.engine_version,
            "provider": self.scanner.provider.name,
            "synthetic_data": synthetic,
            # One field the dashboard can key its banner off, so LIVE vs DEMO is
            # never inferred from a provider name the front end has to recognise.
            "data_mode": "DEMO" if synthetic else "LIVE",
            "data_mode_detail": (
                "DEMO — every candle on screen is generated by the SYNTHETIC fixture provider. "
                "No number here comes from a market."
                if synthetic
                else f"LIVE — candles come from {self.scanner.provider.name}, a real exchange feed."
            ),
            "synthetic_warning": (
                "The active data source generates synthetic candles. Nothing displayed is market data."
                if synthetic
                else None
            ),
            "displaying": "journal" if snapshot else ("live" if report else "nothing"),
            "journal_snapshot": snapshot,
            "scan_count": self.scan_count,
            "consecutive_failures": self.consecutive_failures,
            "scan_interval_seconds": self.settings.scanner.scan_interval_seconds,
            "market_regime": self.scanner.regime.to_dict(),
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
            "retention": {
                "retention_days": self.settings.database.retention_days,
                "last_run": self.last_prune,
                "note": (
                    "A signal is never pruned while it still owes a verdict or a horizon window, "
                    "however old it is."
                ),
            },
            "alert_delivery": {
                "configured": self.delivery.configured,
                # Only ever the redacted host: a webhook URL is a credential.
                "last": self.delivery.last.to_dict(),
                "note": (
                    None
                    if self.delivery.configured
                    else "No webhook configured. Alerts appear in the dashboard only. "
                         "Set CP_ALERT_WEBHOOK_URL to receive them on your phone."
                ),
            },
            "outcome_tracker": {
                "label_config": self.tracker.label.name,
                "definition": self.tracker.label.describe(),
                "horizon_bars": self.tracker.label.horizon_bars,
                "resolves_signals_older_than": _iso_or_none(self.tracker.ready_before_ms()),
                "last_run": self.last_resolution.to_dict() if self.last_resolution else None,
            },
            "horizon_tracker": {
                "horizons": [h.name for h in HORIZONS],
                "success_criterion": (
                    "net change from entry above zero, after the modelled round-trip cost"
                ),
                "entry_rule": "the open of the bar after the signal, never the signal's own close",
                "checks_signals_older_than": _iso_or_none(self.horizons.ready_before_ms()),
                "last_run": self.last_horizon_run.to_dict() if self.last_horizon_run else None,
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
