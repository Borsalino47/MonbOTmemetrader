"""Outcome labelling — the definition of "did this signal work?".

Triple-barrier method. From the entry bar, three things can happen first:

* price reaches entry + `target_atr` * ATR      -> WIN
* price reaches entry - `stop_atr` * ATR        -> LOSS
* neither happens within `horizon_bars`          -> TIMEOUT (settled at last close)

Two decisions here matter more than the arithmetic:

**Barriers are ATR multiples, not percentages.** A 2% target is trivial for one
asset and unreachable for another; 2 ATR means the same thing everywhere, and the
ATR used is the one known at entry — never a later one.

**Ambiguous bars resolve pessimistically.** When a single candle's high crosses
the target *and* its low crosses the stop, intrabar order is unknown. This
implementation records LOSS. That understates performance, which is the correct
direction to be wrong in: the alternative flatters every result and the error
compounds silently across a whole backtest.

Multiple horizons are provided because a definition that only works at one lookahead
is usually an artefact of that lookahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

__all__ = ["Outcome", "LabelConfig", "LabelResult", "label_signal", "DEFAULT_LABEL_CONFIGS"]


class Outcome(str, Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    TIMEOUT = "TIMEOUT"
    UNRESOLVED = "UNRESOLVED"  # not enough future bars exist yet


@dataclass(frozen=True, slots=True)
class LabelConfig:
    name: str
    target_atr: float = 2.0
    stop_atr: float = 1.0
    horizon_bars: int = 24

    def describe(self) -> str:
        return (
            f"{self.name}: long entry at the next bar's open; WIN if price reaches "
            f"+{self.target_atr} ATR before -{self.stop_atr} ATR within {self.horizon_bars} bars; "
            f"LOSS if the stop is hit first; TIMEOUT if neither is reached (settled at the last close). "
            f"Ambiguous bars (both barriers touched in the same candle) resolve as LOSS."
        )


DEFAULT_LABEL_CONFIGS = [
    LabelConfig("fast_2R", target_atr=2.0, stop_atr=1.0, horizon_bars=12),
    LabelConfig("standard_2R", target_atr=2.0, stop_atr=1.0, horizon_bars=24),
    LabelConfig("patient_3R", target_atr=3.0, stop_atr=1.0, horizon_bars=48),
]


@dataclass(slots=True)
class LabelResult:
    outcome: Outcome
    bars_held: int
    entry_price: float
    exit_price: float
    return_pct: float
    mfe_atr: float  # maximum favourable excursion
    mae_atr: float  # maximum adverse excursion
    config_name: str

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "bars_held": self.bars_held,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "return_pct": round(self.return_pct, 4),
            "mfe_atr": round(self.mfe_atr, 3),
            "mae_atr": round(self.mae_atr, 3),
            "config": self.config_name,
        }


def label_signal(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_: np.ndarray,
    signal_index: int,
    atr_at_signal: float,
    cfg: LabelConfig,
) -> LabelResult:
    """Resolve one signal fired at the close of `signal_index`.

    Entry is the **open of the following bar**. Entering at the signal bar's own
    close would assume execution at a price that was only knowable at the instant
    the bar finished, which is not a fill anyone gets.
    """
    entry_index = signal_index + 1
    n = close.size

    if entry_index >= n or atr_at_signal <= 0 or not np.isfinite(atr_at_signal):
        return LabelResult(Outcome.UNRESOLVED, 0, float("nan"), float("nan"), 0.0, 0.0, 0.0, cfg.name)

    entry = float(open_[entry_index])
    target = entry + cfg.target_atr * atr_at_signal
    stop = entry - cfg.stop_atr * atr_at_signal

    last_index = min(entry_index + cfg.horizon_bars - 1, n - 1)
    available = last_index - entry_index + 1
    if available < cfg.horizon_bars and available <= 0:
        return LabelResult(Outcome.UNRESOLVED, 0, entry, float("nan"), 0.0, 0.0, 0.0, cfg.name)

    mfe = 0.0
    mae = 0.0
    for i in range(entry_index, last_index + 1):
        hi, lo = float(high[i]), float(low[i])
        mfe = max(mfe, (hi - entry) / atr_at_signal)
        mae = min(mae, (lo - entry) / atr_at_signal)

        hit_target = hi >= target
        hit_stop = lo <= stop
        if hit_target and hit_stop:
            # Intrabar sequence unknown: assume the adverse fill.
            return LabelResult(
                Outcome.LOSS, i - entry_index + 1, entry, stop, (stop - entry) / entry * 100.0, mfe, mae, cfg.name
            )
        if hit_target:
            return LabelResult(
                Outcome.WIN, i - entry_index + 1, entry, target, (target - entry) / entry * 100.0, mfe, mae, cfg.name
            )
        if hit_stop:
            return LabelResult(
                Outcome.LOSS, i - entry_index + 1, entry, stop, (stop - entry) / entry * 100.0, mfe, mae, cfg.name
            )

    # Horizon expired without touching a barrier.
    if available < cfg.horizon_bars:
        # Not enough future exists yet — this is not a TIMEOUT, it is unknown.
        return LabelResult(Outcome.UNRESOLVED, available, entry, float("nan"), 0.0, mfe, mae, cfg.name)

    exit_price = float(close[last_index])
    return LabelResult(
        Outcome.TIMEOUT,
        available,
        entry,
        exit_price,
        (exit_price - entry) / entry * 100.0,
        mfe,
        mae,
        cfg.name,
    )
