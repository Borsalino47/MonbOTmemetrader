"""Shared scanner contract.

The CEX scanner (V1) and the DEX scanner (Phase 2) implement the same interface,
so the API, dashboard and alert engine work against either without branching.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from cryptopulse.providers.base import ProviderHealth
from cryptopulse.scoring.engine import ScoreResult

__all__ = ["ScanReport", "Scanner"]


@dataclass(slots=True)
class ScanReport:
    """Outcome of one full pass. Includes failures — a scan that half-worked must
    not be presented as a scan that worked."""

    started_at_ms: int
    finished_at_ms: int
    scanned: int
    succeeded: int
    failed: int
    results: list[ScoreResult] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    provider_health: list[ProviderHealth] = field(default_factory=list)
    universe_size: int = 0
    synthetic_data: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return self.finished_at_ms - self.started_at_ms

    def to_dict(self, include_features: bool = False) -> dict:
        return {
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": self.finished_at_ms,
            "duration_ms": self.duration_ms,
            "universe_size": self.universe_size,
            "scanned": self.scanned,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "synthetic_data": self.synthetic_data,
            "notes": self.notes,
            "errors": self.errors,
            "provider_health": [h.to_dict() for h in self.provider_health],
            "results": [r.to_dict(include_features=include_features) for r in self.results],
        }


class Scanner(ABC):
    name: str

    @abstractmethod
    async def scan(self) -> ScanReport: ...

    @abstractmethod
    async def close(self) -> None: ...
