"""Injectable clock.

Everything that needs "now" takes a Clock. Tests and backtests inject a frozen
clock so that data-age and staleness logic is deterministic and so that no code
path can accidentally read the wall clock during a historical replay.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def now_ms(self) -> int: ...


class SystemClock:
    """Wall-clock time, always timezone-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def now_ms(self) -> int:
        return int(self.now().timestamp() * 1000)


class FrozenClock:
    """Deterministic clock for tests and historical replay."""

    def __init__(self, at: datetime | int) -> None:
        self.set(at)

    def set(self, at: datetime | int) -> None:
        if isinstance(at, int):
            self._at = datetime.fromtimestamp(at / 1000, tz=UTC)
        else:
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            self._at = at.astimezone(UTC)

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._at = self._at + timedelta(seconds=seconds)

    def now(self) -> datetime:
        return self._at

    def now_ms(self) -> int:
        return int(self._at.timestamp() * 1000)


SYSTEM_CLOCK = SystemClock()
