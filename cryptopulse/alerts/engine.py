"""Alert engine with deduplication and cooldown.

An alerting system that fires every scan trains you to ignore it, which is worse
than having no alerts. Three mechanisms keep the volume honest:

* **Gates**: an alert requires the score threshold *and* a clean liquidity gate
  *and* a clean safety gate *and* acceptable pump maturity *and* enough data
  confidence. Any one failing means no alert, not a downgraded one.
* **Cooldown**: the same symbol at the same level cannot re-fire inside the
  cooldown window.
* **Dedup key**: symbol + level + setup state. A genuine state change (ARMED →
  BREAKOUT) is news and bypasses the cooldown; a re-scan that produces the same
  state is not.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.config.settings import AlertSettings, ScoringSettings
from cryptopulse.core.logging import get_logger
from cryptopulse.i18n.labels import (
    ALERT_LEVEL_FR,
    LIQUIDITY_FR,
    SETUP_STATE_FR,
    SETUP_STATE_LONG_FR,
)
from cryptopulse.scoring.engine import ScoreResult
from cryptopulse.scoring.states import SetupState

log = get_logger("alerts")

__all__ = ["AlertLevel", "Alert", "AlertEngine"]


class AlertLevel(str, Enum):
    INFO = "INFO"
    WATCH = "WATCH"
    HIGH = "HIGH"
    CRITICAL_SETUP = "CRITICAL_SETUP"

    @property
    def rank(self) -> int:
        return {"INFO": 0, "WATCH": 1, "HIGH": 2, "CRITICAL_SETUP": 3}[self.value]


def _alert_label(level) -> str:
    return ALERT_LEVEL_FR[level.value]


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

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "level": self.level.value,
            "level_label_fr": _alert_label(self.level),
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
        }

    def format_text(self) -> str:
        """The body of the notification and of the webhook payload.

        This is the one alert surface the user reads away from the screen, so
        it is French end to end (spec §17). The reasons arrive already
        rendered — they were assembled in `cryptopulse/i18n/reasons.py` where
        the numbers were.
        """
        lines = [
            f"{self.symbol}",
            f"{self.headline} — {ALERT_LEVEL_FR.get(self.level.value, self.level.value)}",
            "",
            f"Score d'opportunité : {self.final_score:.0f}/100",
            f"Confiance dans les données : {self.data_confidence:.0f}/100",
            f"Maturité du mouvement : {self.pump_maturity:.0f}/100",
            f"Sécurité : {self.safety:.0f}/100",
            f"Liquidité : {LIQUIDITY_FR.get(self.liquidity, self.liquidity)}",
            f"Prix : {self.price:.8g}",
            f"État : {SETUP_STATE_LONG_FR.get(self.state, self.state)}",
        ]
        if self.score_acceleration is not None:
            lines.append(f"Variation du score : {self.score_acceleration:+.1f}")
        if self.why:
            lines += ["", "POURQUOI :"] + [f"  - {w}" for w in self.why[:6]]
        if self.risks:
            lines += ["", "RISQUES :"] + [f"  - {r}" for r in self.risks[:6]]
        if self.trigger:
            lines += ["", f"Déclencheur : {self.trigger}"]
        if self.invalidation:
            lines.append(f"Invalidation : {self.invalidation}")
        return "\n".join(lines)


_STATE_HEADLINES = {
    SetupState.ARMED: "PRÊT — COMPRIMÉ SOUS LA RÉSISTANCE",
    SetupState.BREAKOUT: "CASSURE CONFIRMÉE",
    SetupState.RETEST: "RETEST DU NIVEAU CASSÉ",
    SetupState.CONTINUATION: "CONTINUATION DE LA HAUSSE",
    SetupState.WATCH: "SETUP EN FORMATION",
    SetupState.OBSERVE: "CHANGEMENT DE COMPORTEMENT PRÉCOCE",
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
        """The reason travels to `/api/health`, so it is French like the alert."""
        if r.liquidity.veto:
            return False, "veto de liquidité"
        if r.safety.hard_veto:
            return False, "veto de sécurité"
        if r.maturity.score > self.scoring.pump_maturity_late:
            return False, f"maturité du mouvement trop élevée ({r.maturity.score:.0f})"
        if r.confidence.score < 55.0:
            return False, f"confiance dans les données trop faible ({r.confidence.score:.0f})"
        if r.state.state in (SetupState.IGNORE, SetupState.INVALIDATED):
            return False, f"état {SETUP_STATE_FR[r.state.state.value]}"
        return True, ""

    @staticmethod
    def _dedup_key(r: ScoreResult, level: AlertLevel) -> str:
        payload = f"{r.symbol}|{level.value}|{r.state.state.value}"
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
                headline=_STATE_HEADLINES.get(
                    r.state.state, SETUP_STATE_LONG_FR[r.state.state.value]
                ),
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
        self._history.extend(fired)
        del self._history[:-500]

        if fired:
            log.info("alerts_fired", count=len(fired), symbols=[a.symbol for a in fired])
        return fired

    def recent(self, limit: int = 50) -> list[Alert]:
        return self._history[-limit:][::-1]
