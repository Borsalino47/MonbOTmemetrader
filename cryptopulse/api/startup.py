"""Startup timing — what happened, in what order, and how long each part took.

WHY MEASURE THIS AT ALL

"The app is slow to start" is not actionable. Before this module the only way to
find out where a launch went was to read timestamps out of a log, and the answer
turned out to be surprising: the server itself was ready in about a second, and
the wait was almost entirely the launcher running `pip`, `npm` and a blocking
`doctor` before it started the server at all.

Numbers stop that kind of guess. Every phase records the milliseconds since the
process began, so a slow start on a phone can be attributed rather than argued
about.

THE ORDER IS THE DESIGN

    SERVER READY        HTTP answers. Nothing else has run yet.
    SQLITE WARM START   The last stored scan is in memory and servable.
    UI READY            The browser has been served the built bundle.
    POSITION WATCHER    Open positions first: a 🔴 VENDRE must never queue
                        behind a scan of a hundred and twenty tokens.
    DOCTOR              The feed check, in the background, never blocking.
    FIRST SCAN          Only now does the expensive pass begin.
    OUTCOMES / STATS    Last, because nothing on screen depends on them.

Each of those is a *phase*, and a phase that never happened is absent rather
than zero — `first_scan_ms` of 0 would read as "instant" when it means "has not
happened yet".
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from cryptopulse.core.logging import get_logger

log = get_logger("api.startup")

__all__ = ["PHASES", "StartupTracker"]

# The phases, in the order they are meant to occur. Declared rather than
# discovered so a missing one is visible as a gap instead of just being absent.
PHASES: tuple[str, ...] = (
    "server_ready_ms",
    "sqlite_restore_ms",
    "ui_ready_ms",
    "watcher_ready_ms",
    "doctor_ms",
    "first_live_data_ms",
    "first_scan_ms",
    "full_scan_ms",
)

_LABELS_FR = {
    "server_ready_ms": "serveur prêt",
    "sqlite_restore_ms": "dernier scan restauré depuis SQLite",
    "ui_ready_ms": "interface servie",
    "watcher_ready_ms": "positions ouvertes analysées",
    "doctor_ms": "flux vérifié",
    "first_live_data_ms": "premières données du flux",
    "first_scan_ms": "premier scan lancé",
    "full_scan_ms": "premier scan terminé",
}


@dataclass(slots=True)
class StartupTracker:
    """Milliseconds since the process began, per phase. Records each once.

    Recording once matters: `ui_ready_ms` is set by the first request for the
    dashboard, and every later reload would otherwise overwrite it with a number
    describing a page load rather than a start.
    """

    started_at: float = field(default_factory=time.perf_counter)
    started_at_ms: int = 0
    phases: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Seconds of interpreter and import time that happened before this object
    # could exist. Reported rather than hidden: on a phone it is the larger half
    # of a launch, and a timeline starting after it would claim the app was
    # ready long before anything was on screen.
    preamble_ms: int = 0

    def __post_init__(self) -> None:
        stamp = os.environ.get("CP_PROCESS_START")
        if not stamp:
            return
        try:
            self.preamble_ms = max(0, int((time.time() - float(stamp)) * 1000))
        except (TypeError, ValueError):
            self.preamble_ms = 0

    def mark(self, phase: str, *, note: str | None = None) -> int | None:
        """Record `phase` if it has not been recorded. Returns the elapsed ms."""
        if phase in self.phases:
            return None
        elapsed = self.preamble_ms + int((time.perf_counter() - self.started_at) * 1000)
        self.phases[phase] = elapsed
        if note:
            self.notes.append(f"{phase}: {note}")
        log.info("startup_phase", phase=phase, ms=elapsed)
        return elapsed

    @property
    def elapsed_ms(self) -> int:
        return self.preamble_ms + int((time.perf_counter() - self.started_at) * 1000)

    def to_dict(self) -> dict:
        # Declared phases first and in order, so the sequence reads as a
        # timeline. A phase that has not happened is None, never 0 — a zero
        # would read as "instant" when it means "not yet".
        ordered = {p: self.phases.get(p) for p in PHASES}
        extra = {k: v for k, v in self.phases.items() if k not in ordered}
        return {
            **ordered,
            **extra,
            "labels_fr": _LABELS_FR,
            "elapsed_ms": self.elapsed_ms,
            # How much of the total was Python starting up and importing. The
            # part no amount of application code can make faster.
            "python_preamble_ms": self.preamble_ms,
            "notes": self.notes[:12],
            "note": (
                "Millisecondes depuis le lancement du processus. Une phase absente "
                "(null) n'a pas encore eu lieu — ce n'est pas zéro."
            ),
        }
