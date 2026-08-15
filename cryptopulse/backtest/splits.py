"""Data splitting and walk-forward windows.

The failure mode this exists to prevent: tuning weights on the whole history and
then reporting performance on that same history. Those numbers are a description
of the past, not evidence about the future.

Splits are chronological — never shuffled. Shuffling a time series lets the model
see the future in the training set.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Split", "chronological_split", "walk_forward_windows"]


@dataclass(frozen=True, slots=True)
class Split:
    name: str
    start: int
    end: int  # exclusive

    @property
    def size(self) -> int:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {"name": self.name, "start": self.start, "end": self.end, "size": self.size}


def chronological_split(n: int, train: float = 0.6, validation: float = 0.2) -> list[Split]:
    """60/20/20 by default. The out-of-sample block is the most recent data."""
    if not 0 < train < 1 or not 0 <= validation < 1 or train + validation >= 1:
        raise ValueError("train and validation must be fractions summing to less than 1")
    train_end = int(n * train)
    val_end = train_end + int(n * validation)
    return [
        Split("train", 0, train_end),
        Split("validation", train_end, val_end),
        Split("out_of_sample", val_end, n),
    ]


def walk_forward_windows(
    n: int, train_size: int, test_size: int, step: int | None = None, *, embargo: int = 0
) -> list[tuple[Split, Split]]:
    """Rolling (train, test) pairs marching forward through the series.

    `embargo` drops bars between train and test. With overlapping labels — a
    signal at bar i is resolved using bars i+1..i+horizon — the last training
    labels would otherwise be built from bars that also appear in the test
    window. Set the embargo to at least the label horizon.
    """
    step = step or test_size
    windows: list[tuple[Split, Split]] = []
    start = 0
    idx = 0
    while True:
        train_end = start + train_size
        test_start = train_end + embargo
        test_end = test_start + test_size
        if test_end > n:
            break
        windows.append(
            (Split(f"train_{idx}", start, train_end), Split(f"test_{idx}", test_start, test_end))
        )
        start += step
        idx += 1
    return windows
