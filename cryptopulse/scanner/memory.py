"""Temporal memory: the scanner remembers what it thought last time.

Without this, every scan is amnesiac and a coin that has sat at 80 for three
hours looks identical to one that went 50 → 80 in twenty minutes. The second is
the interesting one, and only a memory can tell them apart.

Score acceleration is measured over a wall-clock window rather than a fixed
number of scans, so changing the scan interval does not silently rescale it.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass

__all__ = ["ScorePoint", "ScoreMemory"]


@dataclass(frozen=True, slots=True)
class ScorePoint:
    timestamp_ms: int
    final_score: float
    raw_score: float
    price: float
    state: str

    def to_dict(self) -> dict:
        return {
            "timestamp_ms": self.timestamp_ms,
            "final_score": round(self.final_score, 2),
            "raw_score": round(self.raw_score, 2),
            "price": self.price,
            "state": self.state,
        }


class ScoreMemory:
    """Per-symbol ring buffer of recent scores. Thread-safe.

    In-memory by design: the durable record lives in the signals table. This is
    the hot path the ranker reads on every scan.
    """

    def __init__(self, maxlen: int = 288, acceleration_window_seconds: int = 1200) -> None:
        self.maxlen = maxlen
        self.window_ms = acceleration_window_seconds * 1000
        self._data: dict[str, deque[ScorePoint]] = defaultdict(lambda: deque(maxlen=maxlen))
        self._lock = threading.Lock()

    def record(self, symbol: str, point: ScorePoint) -> None:
        with self._lock:
            history = self._data[symbol]
            # Guard against double-recording the same closed candle.
            if history and history[-1].timestamp_ms == point.timestamp_ms:
                history[-1] = point
            else:
                history.append(point)

    def history(self, symbol: str, limit: int | None = None) -> list[ScorePoint]:
        with self._lock:
            pts = list(self._data.get(symbol, ()))
        return pts[-limit:] if limit else pts

    def previous(self, symbol: str) -> ScorePoint | None:
        with self._lock:
            history = self._data.get(symbol)
            if not history or len(history) < 2:
                return None
            return history[-2]

    def acceleration(self, symbol: str, now_ms: int) -> tuple[float | None, float | None]:
        """(score change over the window, reference score at the window's start).

        Returns (None, None) until there is a reading old enough to compare
        against — an invented baseline would manufacture acceleration out of
        nothing on the first scan after startup.
        """
        with self._lock:
            history = list(self._data.get(symbol, ()))
        if len(history) < 2:
            return None, None

        current = history[-1]
        cutoff = now_ms - self.window_ms
        older = [p for p in history[:-1] if p.timestamp_ms <= cutoff]
        reference = older[-1] if older else history[0]

        if reference.timestamp_ms == current.timestamp_ms:
            return None, None
        return current.final_score - reference.final_score, reference.final_score

    def symbols(self) -> list[str]:
        with self._lock:
            return list(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
