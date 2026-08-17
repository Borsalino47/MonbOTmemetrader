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
from cryptopulse.alerts.notify import (
    LEVEL_ORDER,
    DecisionNotice,
    NotificationGate,
    NotificationReport,
    build_notifier,
    notify_alerts,
    notify_decision,
)
from cryptopulse.config.settings import CryptoPulseSettings, get_settings
from cryptopulse.core.clock import SYSTEM_CLOCK
from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import Timeframe
from cryptopulse.database import repo
from cryptopulse.database.session import init_engine
from cryptopulse.hunter.deep import DeepScanner, DeepScanReport
from cryptopulse.hunter.discovery import PrescanReport, SnapshotMemory, prescan
from cryptopulse.outcomes.horizons import HORIZONS, HorizonReport, HorizonTracker
from cryptopulse.outcomes.tracker import OutcomeTracker, ResolutionReport
from cryptopulse.providers.registry import is_synthetic
from cryptopulse.scanner.base import ScanReport
from cryptopulse.scanner.cex import CexScanner
from cryptopulse.scanner.memory import ScoreMemory
from cryptopulse.scoring.engine import ScoreResult
from cryptopulse.trading.decision import TradeAction, TradeDecisionEngine
from cryptopulse.trading.hysteresis import DecisionGate
from cryptopulse.trading.watcher import PositionWatcher

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
        # A second, independent alert channel. Unlike the webhook it needs no
        # account and routes nothing through a third party — but it only exists
        # on a phone running Termux, so the builder returns a channel that
        # states why it is unavailable rather than one that fails silently.
        self.notifier = build_notifier(
            enabled=self.settings.alerts.android_notifications,
            binary=self.settings.alerts.android_notification_binary,
        )
        self.notification_gate = NotificationGate()
        self.last_notification: NotificationReport | None = None

        # The decision layer. One gate shared by the scanner and the watcher,
        # so a symbol cannot show one decision in the list and another in its
        # position card.
        self.trade_engine = TradeDecisionEngine(self.settings.trade)
        self.decision_gate = DecisionGate(
            confirmations_required=self.settings.trade.confirmations_required,
            min_seconds_between_changes=self.settings.trade.min_seconds_between_changes,
        )
        self.watcher = PositionWatcher(
            self.settings, self.scanner,
            engine=self.trade_engine, gate=self.decision_gate, clock=SYSTEM_CLOCK,
            on_decision=self._notify_decision_for_position,
        )
        self.last_decisions: list[dict] = []
        self._watch_task: asyncio.Task | None = None

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

        # The Token Hunter reads the venue-wide ticker the scan already fetched,
        # so a wide search costs no extra requests. Its memory of the previous
        # reading is what turns two snapshots into an acceleration signal.
        self.hunter_memory = SnapshotMemory(clock=SYSTEM_CLOCK)
        self.last_prescan: PrescanReport | None = None

        # The deep scan is on demand, never on the loop: it is the only part of
        # the hunter that spends requests, and spending them every cycle for
        # tokens nobody asked about is exactly what the pre-scan exists to avoid.
        self.deep = DeepScanner(self.settings, self.scanner, clock=SYSTEM_CLOCK)
        self.last_deep_scan: DeepScanReport | None = None
        self._deep_lock = asyncio.Lock()

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
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._watch_loop(), name="position-watch-loop")
            log.info(
                "position_watch_started",
                interval_s=self.settings.trade.position_watch_interval_seconds,
            )

    async def stop(self) -> None:
        for attr in ("_task", "_watch_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, attr, None)
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

    async def _watch_loop(self) -> None:
        """Higher frequency, open positions only.

        Independent of the scan loop on purpose: a slow scan must not delay the
        one thing whose latency actually costs money, and a failing watcher must
        not stop the scanner finding the next opportunity.
        """
        while True:
            try:
                if await asyncio.to_thread(self._has_open_positions):
                    await self.watcher.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("position_watch_loop_error", error=str(exc)[:300])
            await asyncio.sleep(self.settings.trade.position_watch_interval_seconds)

    async def _refresh_entry_decisions(self, report, now_ms: int) -> None:
        """Decide about everything not currently held, and journal new buys.

        The noise gate applies here too. A 🟢 ACHETER that appears and vanishes
        inside a minute is worse than one that arrives a minute late: it teaches
        the reader to distrust the green card, which is the only card that has to
        be believed immediately.
        """
        try:
            held = set(await asyncio.to_thread(repo.open_position_symbols))
        except Exception:
            held = set()

        decisions: list[dict] = []
        for result in report.results:
            if result.symbol in held:
                continue  # the watcher owns this one
            try:
                decision = self.trade_engine.decide_entry(result)
            except Exception as exc:
                log.warning("decision_failed", symbol=result.symbol, error=type(exc).__name__)
                continue

            outcome = self.decision_gate.evaluate(result.symbol, decision.action, now_ms=now_ms)
            payload = decision.to_dict()
            # What is on screen is the gated action, not the raw one, so the
            # journal and the screen can never disagree about what was shown.
            payload["action"] = outcome.action.value
            payload["proposed"] = outcome.proposed.value
            payload["gate"] = outcome.to_dict()
            decisions.append(payload)

            if outcome.changed and outcome.action is TradeAction.BUY:
                await self._journal_buy(result, decision)
                await self._notify_decision(decision, result)

        decisions.sort(key=lambda d: (d["action"] != "BUY", -(d.get("price") or 0)))
        self.last_decisions = decisions

    async def _journal_buy(self, result, decision) -> None:
        """Record the recommendation so it can be answered and then measured.

        Written whether or not the user ever answers: a signal nobody responded
        to is still a signal the engine made, and excluding it would make every
        later statistic flattering.
        """
        try:
            await asyncio.to_thread(
                repo.save_trade_signal,
                {
                    "symbol": result.symbol,
                    "action": "BUY",
                    "strength": decision.strength.value if decision.strength else None,
                    "timestamp_ms": decision.timestamp_ms,
                    "price": decision.price,
                    "opportunity_score": result.final_score,
                    "explosion_score": result.explosion.score if result.explosion else None,
                    "safety_score": result.safety.score,
                    "data_confidence": result.confidence.score,
                    "pump_maturity": result.maturity.score,
                    "setup_state": result.state.state.value,
                    "market_regime": self.scanner.regime.trend.value,
                    "trigger_price": decision.trigger_price,
                    "invalidation_price": decision.invalidation_price,
                    "reasons": decision.reasons,
                    "risks": decision.risks,
                    "engine_version": decision.engine_version,
                    "weights_fingerprint": decision.weights_fingerprint,
                    "data_source": self.scanner.provider.name,
                    "synthetic": is_synthetic(self.scanner.provider),
                },
            )
            log.info(
                "buy_signal",
                symbol=result.symbol,
                strength=decision.strength.value if decision.strength else None,
            )
        except Exception as exc:
            # A journalling failure costs a record, never the scan.
            log.error("buy_signal_journal_failed", symbol=result.symbol, error=type(exc).__name__)

    async def _notify_decision_for_position(self, decision, result, position: dict) -> None:
        """Adapter for the watcher's callback, which passes the position row."""
        await self._notify_decision(decision, result, position=position)

    async def _notify_decision(self, decision, result, *, position: dict | None = None) -> None:
        """Put a BUY or SELL on the lock screen. Never costs the scan.

        Not gated by the notification anti-spam rule: that gate stops a symbol
        repeating at the same alert level, while a decision only reaches here
        once the hysteresis has confirmed it *changed*. Applying both would let a
        genuine change be suppressed for sharing a name with the previous one.
        """
        notice = DecisionNotice(
            action=decision.action.value,
            symbol=decision.symbol,
            price=f"{decision.price:.6g}",
            strength=decision.strength.value if decision.strength else None,
            lines=(decision.blocking or decision.reasons)[:3],
            health=(
                None if position is None or position.get("health_score") is None
                else f"{position['health_score']:.0f}/100"
            ),
            pnl=(
                None if position is None or position.get("pnl_pct") is None
                else f"{position['pnl_pct']:+.2f}%"
            ),
        )
        try:
            self.last_notification = await notify_decision(
                self.notifier, notice, synthetic=is_synthetic(self.scanner.provider)
            )
        except Exception as exc:
            log.error("decision_notification_failed", error=type(exc).__name__)

    def _has_open_positions(self) -> bool:
        """One cheap query. With nothing open the watcher costs nothing at all."""
        try:
            self.ensure_db()
            return bool(repo.open_position_symbols())
        except Exception:
            return False

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

            # Entry decisions for everything scanned. Held positions are skipped
            # here: what to do about something you own is the watcher's job, and
            # answering it from a once-a-minute loop would be the wrong latency.
            await self._refresh_entry_decisions(report, now_ms)

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

            # Independently of the webhook: either channel may be off, and
            # neither may break the other or the scan.
            if alerts:
                loud = [a for a in alerts if self._loud_enough(a)]
                if loud:
                    self.last_notification = await notify_alerts(
                        self.notifier, self.notification_gate, loud,
                        synthetic=is_synthetic(self.scanner.provider),
                    )

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

        # Free: reads the ticker dictionary the scan already holds. Running it
        # every cycle is what builds the previous-reading memory the deltas need.
        try:
            self.hunt()
        except Exception as exc:
            log.error("prescan_failed", error=str(exc)[:300])

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

    def hunt(self, limit: int = 40, *, record: bool = True) -> PrescanReport:
        """Rank the whole venue on the ticker data the last scan already paid for.

        Synchronous and instant: no request is made. It reads the snapshot the
        scanner kept, which is why a wide search over hundreds of tokens costs
        nothing and can be run on demand from a phone.

        Only the scan cycle passes `record=True`. An on-demand search must not
        advance the snapshot memory: doing so overwrote the previous reading and
        wiped out the acceleration signal, which was the point of searching.
        """
        cfg = self.settings.scanner
        report = prescan(
            self.scanner.last_tickers,
            self.hunter_memory,
            quote_asset=cfg.quote_asset,
            exclude_bases=tuple(cfg.exclude_stable_bases),
            exclude_patterns=tuple(cfg.exclude_patterns),
            limit=limit,
            record=record,
        )
        if record:
            self.last_prescan = report
        return report

    async def deep_scan(self, *, max_symbols: int = 40) -> DeepScanReport:
        """Analyse the pre-scan's candidates properly. Costs kline requests.

        Serialised: two concurrent deep scans would double the request bill for
        an identical answer.
        """
        async with self._deep_lock:
            report = self.last_prescan
            candidates = report.candidates if report else []
            result = await self.deep.run(candidates, max_symbols=max_symbols)
            self.last_deep_scan = result
            return result

    async def pump_history(
        self, symbol: str, *, timeframe: Timeframe | None = None, bars: int = 1000
    ) -> dict:
        """Past accelerations for one symbol, plus how the present compares.

        1h by default: it is the shortest timeframe whose 1000-bar window (41
        days) yields a sample large enough to report a rate at all, and 300 of
        those bars are already in the candle cache for any scanned symbol.
        """
        from cryptopulse.pumps import (
            build_history,
            compare_to_past,
            summarise,
        )

        tf = timeframe or Timeframe.H1
        series = await self.scanner.provider.get_ohlcv(symbol.upper(), tf, bars)
        history = build_history(series)
        stats = summarise(history.episodes)

        # The present setup, described exactly as past ones were: from bars at
        # or before now. `find_pumps` computes the same fields the same way.
        current = _current_fingerprint(series)
        similarity = compare_to_past(current, history.episodes)

        return {
            "history": history.to_dict(),
            "stats": stats.to_dict(),
            "similarity": similarity.to_dict(),
            "current_setup": {
                "rvol": current.rvol,
                "volume_change_pct": current.volume_change_pct,
                "range_position": current.range_position,
                "atr_pct": current.atr_pct,
            },
            "data_mode": "DEMO" if is_synthetic(self.scanner.provider) else "LIVE",
        }

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

    def _loud_enough(self, alert) -> bool:
        """Is this worth interrupting someone holding a phone?

        The webhook gets everything the alert engine emitted; a notification is
        a physical interruption and gets a higher bar. Defaults to HIGH, which
        means a WATCH never buzzes — that is what keeps the CRITICAL one from
        being muted along with everything else.
        """
        floor = self.settings.alerts.android_min_level
        level = getattr(alert.level, "value", alert.level)
        try:
            return LEVEL_ORDER.index(level) >= LEVEL_ORDER.index(floor)
        except ValueError:
            # An unrecognised floor must not silently mute everything.
            log.warning("unknown_android_min_level", configured=floor)
            return True

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
            "candle_cache": _cache_stats(self.scanner.provider),
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
            "android_notifications": {
                **self.notifier.availability().to_dict(),
                "channel": self.notifier.name,
                "min_level": self.settings.alerts.android_min_level,
                # A channel that stopped working must never look like a quiet
                # market, so the last outcome is reported the same way the
                # webhook's is.
                "last": self.last_notification.to_dict() if self.last_notification else None,
            },
            "trading": {
                "engine_version": self.trade_engine.__class__.__name__,
                "weights_fingerprint": self.trade_engine.weights_fingerprint,
                "decisions_now": len(self.last_decisions),
                "buy_now": sum(1 for d in self.last_decisions if d["action"] == "BUY"),
                "position_watcher": self.watcher.status(),
                "note": (
                    "Aucun ordre n'est jamais passé automatiquement. L'application "
                    "recommande ; vous exécutez vous-même et vous confirmez ensuite."
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


def _current_fingerprint(series):
    """Describe the present setup with the same measures, computed the same way.

    Reuses the episode builder rather than duplicating the arithmetic, so the
    current fingerprint and the historical ones can never drift apart — a
    comparison between two differently-computed descriptions would be worthless.
    """
    from cryptopulse.pumps.detect import PumpEpisode, _attach_start_context
    from cryptopulse.pumps.stats import SetupFingerprint

    closed = series.closed()
    if len(closed) < 51:
        return SetupFingerprint()

    probe = PumpEpisode(
        symbol=closed.symbol, timeframe=str(closed.timeframe), resolution_minutes=0,
        start_ms=0, start_price=float(closed.close[-1]), peak_ms=0,
        peak_price=float(closed.close[-1]), gain_pct=0.0, bars_to_peak=0, minutes_to_peak=0,
    )
    _attach_start_context(probe, closed, len(closed) - 1, 50)
    return SetupFingerprint.from_episode(probe)


def _cache_stats(provider) -> dict:
    """Cache figures if the provider is wrapped, otherwise a stated absence.

    Reported so the saving is visible rather than assumed — and so a cache that
    silently stopped working looks different from one that is switched off.
    """
    stats = getattr(provider, "cache_stats", None)
    if stats is None:
        return {"enabled": False, "note": "candle cache disabled (CP_PROVIDER_CANDLE_CACHE)"}
    return {"enabled": True, **stats()}


def _iso_or_none(ms: int | None) -> str | None:
    if ms is None:
        return None
    from datetime import datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()
