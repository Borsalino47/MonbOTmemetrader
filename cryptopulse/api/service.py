"""Application service: owns the scanner, the loop and the shared state.

One instance per process. The FastAPI routes read from it; the background loop
writes to it. `asyncio` gives us a single-threaded event loop, so the only
concurrency hazard is the database work, which is pushed to a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib

from cryptopulse.alerts.engine import Alert, AlertEngine
from cryptopulse.config.settings import CryptoPulseSettings, get_settings
from cryptopulse.core.clock import SYSTEM_CLOCK
from cryptopulse.core.logging import get_logger
from cryptopulse.database import repo
from cryptopulse.database.session import init_engine
from cryptopulse.providers.registry import is_synthetic
from cryptopulse.scanner.base import ScanReport
from cryptopulse.scanner.cex import CexScanner
from cryptopulse.scanner.memory import ScoreMemory
from cryptopulse.scoring.engine import ScoreResult

log = get_logger("api.service")

__all__ = ["ScannerService", "get_service", "set_service"]


class ScannerService:
    def __init__(self, settings: CryptoPulseSettings | None = None) -> None:
        self.settings = settings or get_settings()
        self.memory = ScoreMemory()
        self.scanner = CexScanner(self.settings, memory=self.memory, clock=SYSTEM_CLOCK)
        self.alerts = AlertEngine(self.settings.alerts, self.settings.scoring)
        self.last_report: ScanReport | None = None
        self.last_alerts: list[Alert] = []
        self.scan_count = 0
        self.consecutive_failures = 0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._db_ready = False

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
