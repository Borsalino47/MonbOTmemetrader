"""Alert engine with deduplication and cooldown.

An alerting system that fires every scan trains you to ignore it, which is worse
than having no alerts. Three mechanisms keep the volume honest:

* **Gates**: an alert requires the score threshold *and* a clean liquidity gate
  *and* a clean safety gate *and* acceptable pump maturity *and* enough data
  confidence. Any one failing means no alert, not a downgraded one.
* **Cooldown**: the same symbol at the same level cannot re-fire inside the
  cooldown window.
* **Dedup key**: symbol + kind + level + setup state. A genuine state change
  (ARMED → BREAKOUT) is news and bypasses the cooldown; a re-scan that produces
  the same state is not.

TWO KINDS OF ALERT, ON PURPOSE

`SETUP` alerts are about the next few hours: a level about to give way, a
breakout confirming. `MOONSHOT` alerts are about the next few weeks: a base that
has been building for months starting to take volume. They are deliberately not
merged, because they demand different reactions and run on different clocks — a
daily base does not change between two one-minute scans, so moonshot alerts use
their own threshold and a cooldown measured in hours.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.config.settings import AlertSettings, ScoringSettings
from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import Timeframe
from cryptopulse.scoring.engine import ScoreResult
from cryptopulse.scoring.moonshot import MoonshotStage
from cryptopulse.scoring.states import SetupState

log = get_logger("alerts")

__all__ = ["AlertKind", "AlertLevel", "Alert", "AlertEngine"]


class AlertKind(str, Enum):
    SETUP = "SETUP"  # tradable setup on the primary timeframe
    MOONSHOT = "MOONSHOT"  # candidate for a large multiple, on the daily


class AlertLevel(str, Enum):
    INFO = "INFO"
    WATCH = "WATCH"
    HIGH = "HIGH"
    CRITICAL_SETUP = "CRITICAL_SETUP"

    @property
    def rank(self) -> int:
        return {"INFO": 0, "WATCH": 1, "HIGH": 2, "CRITICAL_SETUP": 3}[self.value]


@dataclass(slots=True)
class Alert:
    symbol: str
    level: AlertLevel
    headline: str
    timestamp_ms: int
    final_score: float
    pump_maturity: float
    data_confidence: float
    safety: float
    liquidity: str
    state: str
    price: float
    score_acceleration: float | None
    why: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    trigger: str | None = None
    invalidation: str | None = None
    dedup_key: str = ""
    kind: AlertKind = AlertKind.SETUP
    moonshot_score: float | None = None
    moonshot_stage: str | None = None
    moonshot_multiple: float | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "kind": self.kind.value,
            "level": self.level.value,
            "headline": self.headline,
            "timestamp_ms": self.timestamp_ms,
            "price": self.price,
            "opportunity_score": f"{self.final_score:.0f}/100",
            "final_score": round(self.final_score, 1),
            "pump_maturity": round(self.pump_maturity, 1),
            "data_confidence": round(self.data_confidence, 1),
            "safety": round(self.safety, 1),
            "liquidity": self.liquidity,
            "state": self.state,
            "score_acceleration": None if self.score_acceleration is None else round(self.score_acceleration, 1),
            "why": self.why,
            "risks": self.risks,
            "trigger": self.trigger,
            "invalidation": self.invalidation,
            "dedup_key": self.dedup_key,
            "moonshot_score": None if self.moonshot_score is None else round(self.moonshot_score, 1),
            "moonshot_stage": self.moonshot_stage,
            "moonshot_multiple_to_window_high": (
                None if self.moonshot_multiple is None else round(self.moonshot_multiple, 2)
            ),
        }

    def format_text(self) -> str:
        lines = [
            f"{self.symbol}",
            f"{self.headline} — {self.level.value}",
            "",
        ]
        if self.moonshot_score is not None:
            lines.append(f"Moonshot Score: {self.moonshot_score:.0f}/100  (stage: {self.moonshot_stage})")
            if self.moonshot_multiple is not None and self.moonshot_multiple > 1.05:
                lines.append(f"Traded {self.moonshot_multiple:.1f}x higher inside its available history")
        lines += [
            f"Opportunity Score: {self.final_score:.0f}/100",
            f"Data Confidence: {self.data_confidence:.0f}/100",
            f"Pump Maturity: {self.pump_maturity:.0f}/100",
            f"Safety: {self.safety:.0f}/100",
            f"Liquidity: {self.liquidity}",
            f"Price: {self.price:.8g}",
            f"Status: {self.state}",
        ]
        if self.score_acceleration is not None:
            lines.append(f"Score change: {self.score_acceleration:+.1f}")
        if self.why:
            lines += ["", "Why this signal:"] + [f"  - {w}" for w in self.why[:6]]
        if self.risks:
            lines += ["", "Risks:"] + [f"  - {r}" for r in self.risks[:6]]
        if self.trigger:
            lines += ["", f"Trigger: {self.trigger}"]
        if self.invalidation:
            lines.append(f"Invalidation: {self.invalidation}")
        return "\n".join(lines)


_MOONSHOT_HEADLINES = {
    MoonshotStage.IGNITION: "MOONSHOT — IGNITION ON A LONG BASE",
    MoonshotStage.ACCUMULATION: "MOONSHOT — QUIET ACCUMULATION",
}

_STATE_HEADLINES = {
    SetupState.ARMED: "ARMED — COILED BELOW RESISTANCE",
    SetupState.BREAKOUT: "BREAKOUT CONFIRMED",
    SetupState.RETEST: "RETEST OF BROKEN LEVEL",
    SetupState.CONTINUATION: "MOMENTUM CONTINUATION",
    SetupState.WATCH: "SETUP FORMING",
    SetupState.OBSERVE: "EARLY BEHAVIOUR CHANGE",
}


class AlertEngine:
    def __init__(self, alert_cfg: AlertSettings, scoring_cfg: ScoringSettings) -> None:
        self.cfg = alert_cfg
        self.scoring = scoring_cfg
        self._last_fired: dict[str, int] = {}  # dedup_key -> timestamp_ms
        self._last_state: dict[str, str] = {}  # symbol -> state
        self._history: list[Alert] = []

    # -- level selection ------------------------------------------------------ #

    def _level_for(self, r: ScoreResult) -> AlertLevel | None:
        s = r.final_score
        if s >= self.cfg.min_score_critical:
            return AlertLevel.CRITICAL_SETUP
        if s >= self.cfg.min_score_high:
            return AlertLevel.HIGH
        if s >= self.cfg.min_score_watch:
            return AlertLevel.WATCH
        if s >= self.cfg.min_score_info:
            return AlertLevel.INFO
        return None

    def _passes_gates(self, r: ScoreResult) -> tuple[bool, str]:
        if r.liquidity.veto:
            return False, "liquidity veto"
        if r.safety.hard_veto:
            return False, "safety veto"
        if r.maturity.score > self.scoring.pump_maturity_late:
            return False, f"pump maturity {r.maturity.score:.0f} too high"
        if r.confidence.score < 55.0:
            return False, f"data confidence {r.confidence.score:.0f} too low"
        if r.state.state in (SetupState.IGNORE, SetupState.INVALIDATED):
            return False, f"state {r.state.state.value}"
        return True, ""

    @staticmethod
    def _dedup_key(r: ScoreResult, level: AlertLevel, kind: AlertKind = AlertKind.SETUP) -> str:
        state = r.state.state.value
        if kind is AlertKind.MOONSHOT:
            # A moonshot alert is about the daily stage, not the intraday setup
            # state — keying it on the latter would re-fire the same base every
            # time a 5m candle changed the setup label.
            state = r.moonshot.stage.value if r.moonshot else "UNKNOWN"
        payload = f"{r.symbol}|{kind.value}|{level.value}|{state}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    # -- evaluation ----------------------------------------------------------- #

    def evaluate(self, results: list[ScoreResult], now_ms: int) -> list[Alert]:
        if not self.cfg.enabled:
            return []

        fired: list[Alert] = []
        for r in results:
            level = self._level_for(r)
            if level is None:
                continue

            ok, why_not = self._passes_gates(r)
            if not ok:
                log.debug("alert_suppressed", symbol=r.symbol, reason=why_not)
                continue

            # Acceleration requirement applies to the informational tiers only.
            # A CRITICAL_SETUP that is simply very good should not be withheld
            # because it did not improve since the last scan.
            if level.rank <= AlertLevel.HIGH.rank:
                accel = r.score_acceleration
                if accel is not None and accel < self.cfg.min_score_acceleration:
                    if r.state.state not in (SetupState.BREAKOUT, SetupState.ARMED):
                        continue

            key = self._dedup_key(r, level)
            state_changed = self._last_state.get(r.symbol) != r.state.state.value
            last = self._last_fired.get(key)
            if last is not None and not state_changed:
                if now_ms - last < self.cfg.cooldown_seconds * 1000:
                    continue

            alert = Alert(
                symbol=r.symbol,
                level=level,
                headline=_STATE_HEADLINES.get(r.state.state, r.state.state.value),
                timestamp_ms=now_ms,
                final_score=r.final_score,
                pump_maturity=r.maturity.score,
                data_confidence=r.confidence.score,
                safety=r.safety.score,
                liquidity=r.liquidity.status.value,
                state=r.state.state.value,
                price=r.price,
                score_acceleration=r.score_acceleration,
                why=r.why()[:8],
                risks=r.risks()[:8],
                trigger=r.state.trigger,
                invalidation=r.state.invalidation,
                dedup_key=key,
            )
            fired.append(alert)
            self._last_fired[key] = now_ms
            self._last_state[r.symbol] = r.state.state.value

        fired.sort(key=lambda a: (a.level.rank, a.final_score), reverse=True)
        fired = fired[: self.cfg.max_alerts_per_scan]

        # Moonshot alerts get their own budget rather than competing for the same
        # slots: they are rare by construction, and a busy hour of setup alerts
        # must not be able to bury the one signal the radar exists to find.
        moonshots = self.evaluate_moonshots(results, now_ms)
        fired = fired + moonshots

        self._history.extend(fired)
        del self._history[:-500]

        if fired:
            log.info("alerts_fired", count=len(fired), symbols=[a.symbol for a in fired])
        return fired

    # -- moonshot ------------------------------------------------------------- #

    def evaluate_moonshots(self, results: list[ScoreResult], now_ms: int) -> list[Alert]:
        """Alerts for candidates on a multi-week horizon.

        Same gates as a setup alert — liquidity, safety and data confidence are
        not negotiable on either horizon — but the score, the cooldown and the
        dedup key all come from the daily reading rather than the intraday one.
        """
        if not self.cfg.enabled:
            return []

        fired: list[Alert] = []
        for r in results:
            m = r.moonshot
            if m is None or m.stage not in (MoonshotStage.IGNITION, MoonshotStage.ACCUMULATION):
                continue
            if m.score < self.cfg.min_score_moonshot:
                continue
            if r.liquidity.veto or r.safety.hard_veto:
                log.debug("moonshot_suppressed", symbol=r.symbol, reason="gate veto")
                continue
            if r.confidence.score < 55.0:
                log.debug("moonshot_suppressed", symbol=r.symbol, reason="data confidence")
                continue

            level = AlertLevel.HIGH if m.score >= 80 else AlertLevel.WATCH
            key = self._dedup_key(r, level, AlertKind.MOONSHOT)
            last = self._last_fired.get(key)
            if last is not None and now_ms - last < self.cfg.moonshot_cooldown_seconds * 1000:
                continue

            # The trigger level comes from the timeframe the moonshot reading was
            # actually taken on, not the intraday primary.
            trigger = None
            if r.features is not None and m.timeframe:
                htf = r.features.get(Timeframe.parse(m.timeframe))
                exp = htf.expansion if htf else None
                if exp and exp.base_high:
                    trigger = f"{m.timeframe} close above the base high {exp.base_high:.8g}"

            fired.append(
                Alert(
                    symbol=r.symbol,
                    level=level,
                    kind=AlertKind.MOONSHOT,
                    headline=_MOONSHOT_HEADLINES.get(m.stage, "MOONSHOT CANDIDATE"),
                    timestamp_ms=now_ms,
                    final_score=r.final_score,
                    pump_maturity=r.maturity.score,
                    data_confidence=r.confidence.score,
                    safety=r.safety.score,
                    liquidity=r.liquidity.status.value,
                    state=r.state.state.value,
                    price=r.price,
                    score_acceleration=r.score_acceleration,
                    why=m.reasons[:8],
                    # Caveats and unknowns together: what argues against, and what
                    # was never measured. Both belong in front of a human here.
                    risks=(m.caveats + m.unknowns)[:8],
                    trigger=trigger,
                    invalidation=r.state.invalidation,
                    dedup_key=key,
                    moonshot_score=m.score,
                    moonshot_stage=m.stage.value,
                    moonshot_multiple=m.multiple_to_window_high,
                )
            )
            self._last_fired[key] = now_ms

        fired.sort(key=lambda a: a.moonshot_score or 0.0, reverse=True)
        return fired[: self.cfg.max_alerts_per_scan]

    def recent(self, limit: int = 50) -> list[Alert]:
        return self._history[-limit:][::-1]
