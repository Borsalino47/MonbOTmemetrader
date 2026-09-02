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

TWO FAMILIES, BECAUSE THERE ARE TWO THESES
------------------------------------------
The ATR family above grades the opportunity score: a move of a few ATR over
hours. It cannot grade the ×10 layer, whose claim is about *weeks* and is stated
as a multiple of price, not as a multiple of ATR. Grading a moonshot signal with
`standard_2R` would answer a question nobody asked.

So a second family exists whose barriers are **price multiples** and whose
horizon is counted in **daily** bars (`timeframe`). One deliberate compromise in
it: grading directly at ×10 would produce approximately zero settled rows for
years, which is not a feedback loop. The ladder therefore settles at ×2 and ×3
as well — "did the thesis start working" — and **every result records
`max_multiple`**, the highest multiple actually reached inside the horizon. That
single field means "how many of these ever reached ×10" is answerable later by
reading the journal, without re-grading anything and without waiting for a label
that would take three years to settle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from cryptopulse.core.types import Timeframe

__all__ = [
    "Outcome", "LabelConfig", "LabelResult", "label_signal",
    "DEFAULT_LABEL_CONFIGS", "MOONSHOT_LABEL_CONFIGS", "label_config_by_name", "ALL_LABEL_CONFIGS",
]


class Outcome(str, Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    TIMEOUT = "TIMEOUT"
    # Not enough future bars exist *yet*. A transient state: ask again later.
    UNRESOLVED = "UNRESOLVED"
    # Can never be resolved — the bars needed have fallen out of reach, or the
    # signal lacks the ATR its barriers depend on. Terminal, and excluded from
    # every rate rather than counted as a loss.
    UNRESOLVABLE = "UNRESOLVABLE"

    @property
    def is_settled(self) -> bool:
        """True when this outcome represents a real verdict on the trade."""
        return self in (Outcome.WIN, Outcome.LOSS, Outcome.TIMEOUT)


@dataclass(frozen=True, slots=True)
class LabelConfig:
    """How a signal is graded.

    Barriers are ATR multiples by default. Setting `target_multiple` switches the
    label to price multiples instead — the only honest way to state a "×N" thesis,
    since ATR at signal time says nothing about where a ten-fold move ends.

    `timeframe` is the timeframe the *outcome* is measured on, which is not
    necessarily the one the signal was scored on. A base that took four months to
    build resolves on daily bars even though the signal fired on a 5-minute close.
    `None` means "the scanner's primary timeframe", i.e. the V1 behaviour.
    """

    name: str
    target_atr: float = 2.0
    stop_atr: float = 1.0
    horizon_bars: int = 24
    # Price-multiple barriers. When `target_multiple` is set it wins over the ATR
    # pair: target = entry * multiple, stop = entry * (1 - stop_pct/100).
    target_multiple: float | None = None
    stop_pct: float | None = None
    timeframe: Timeframe | None = None

    @property
    def is_multiple_based(self) -> bool:
        return self.target_multiple is not None

    @property
    def needs_atr(self) -> bool:
        """ATR-based barriers cannot be placed without the ATR recorded at signal time."""
        return not self.is_multiple_based

    def describe(self) -> str:
        tf = f" on {self.timeframe.value} bars" if self.timeframe else ""
        if self.is_multiple_based:
            stop = f"-{self.stop_pct:.0f}%" if self.stop_pct else "no stop"
            return (
                f"{self.name}: long entry at the next bar's open{tf}; WIN if price reaches "
                f"x{self.target_multiple:g} before {stop} within {self.horizon_bars} bars; "
                f"LOSS if the stop is hit first; TIMEOUT if neither is reached (settled at the last "
                f"close). Ambiguous bars resolve as LOSS. `max_multiple` records the highest multiple "
                f"actually reached, whatever the verdict."
            )
        return (
            f"{self.name}: long entry at the next bar's open{tf}; WIN if price reaches "
            f"+{self.target_atr} ATR before -{self.stop_atr} ATR within {self.horizon_bars} bars; "
            f"LOSS if the stop is hit first; TIMEOUT if neither is reached (settled at the last close). "
            f"Ambiguous bars (both barriers touched in the same candle) resolve as LOSS."
        )


DEFAULT_LABEL_CONFIGS = [
    LabelConfig("fast_2R", target_atr=2.0, stop_atr=1.0, horizon_bars=12),
    LabelConfig("standard_2R", target_atr=2.0, stop_atr=1.0, horizon_bars=24),
    LabelConfig("patient_3R", target_atr=3.0, stop_atr=1.0, horizon_bars=48),
]

# The ×10 ladder. Horizons are in DAILY bars: 30 bars is a month, 180 is half a
# year. The stops widen with the horizon because an asset that needs six months
# to go ×10 is not one you can hold on a 35% leash.
MOONSHOT_LABEL_CONFIGS = [
    LabelConfig("moon_2x_30d", target_multiple=2.0, stop_pct=35.0, horizon_bars=30, timeframe=Timeframe.D1),
    LabelConfig("moon_3x_90d", target_multiple=3.0, stop_pct=50.0, horizon_bars=90, timeframe=Timeframe.D1),
    LabelConfig("moon_10x_180d", target_multiple=10.0, stop_pct=60.0, horizon_bars=180, timeframe=Timeframe.D1),
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
    # Highest multiple of the entry price touched inside the horizon, whatever
    # the verdict. 1.0 means price never traded above entry. This is what makes
    # "how many of these ever reached x10" answerable from the journal alone.
    max_multiple: float = 1.0

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "bars_held": self.bars_held,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "return_pct": round(self.return_pct, 4),
            "mfe_atr": None if not np.isfinite(self.mfe_atr) else round(self.mfe_atr, 3),
            "mae_atr": None if not np.isfinite(self.mae_atr) else round(self.mae_atr, 3),
            "max_multiple": round(self.max_multiple, 4),
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

    atr_usable = bool(atr_at_signal and atr_at_signal > 0 and np.isfinite(atr_at_signal))
    # ATR barriers cannot be placed without an ATR. Multiple barriers can — they
    # are a function of price alone — so a missing ATR only costs the excursion
    # figures, which are then reported as nan rather than as a fabricated 0.
    if entry_index >= n or (cfg.needs_atr and not atr_usable):
        return LabelResult(Outcome.UNRESOLVED, 0, float("nan"), float("nan"), 0.0, 0.0, 0.0, cfg.name)

    entry = float(open_[entry_index])
    if entry <= 0 or not np.isfinite(entry):
        return LabelResult(Outcome.UNRESOLVED, 0, float("nan"), float("nan"), 0.0, 0.0, 0.0, cfg.name)

    if cfg.is_multiple_based:
        target = entry * cfg.target_multiple
        # No stop configured means the only ways out are the target and the
        # horizon. That is a deliberate option for a thesis measured in months.
        stop = entry * (1.0 - cfg.stop_pct / 100.0) if cfg.stop_pct else -np.inf
    else:
        target = entry + cfg.target_atr * atr_at_signal
        stop = entry - cfg.stop_atr * atr_at_signal

    # Excursions are reported in ATR when an ATR is known, and left nan when it
    # is not. `max_multiple` is always available because it needs only the price.
    scale = atr_at_signal if atr_usable else float("nan")

    last_index = min(entry_index + cfg.horizon_bars - 1, n - 1)
    available = last_index - entry_index + 1
    if available < cfg.horizon_bars and available <= 0:
        return LabelResult(Outcome.UNRESOLVED, 0, entry, float("nan"), 0.0, 0.0, 0.0, cfg.name)

    mfe = 0.0
    mae = 0.0
    max_multiple = 1.0
    for i in range(entry_index, last_index + 1):
        hi, lo = float(high[i]), float(low[i])
        mfe = max(mfe, (hi - entry) / scale) if atr_usable else float("nan")
        mae = min(mae, (lo - entry) / scale) if atr_usable else float("nan")
        max_multiple = max(max_multiple, hi / entry)

        hit_target = hi >= target
        hit_stop = lo <= stop
        if hit_target and hit_stop:
            # Intrabar sequence unknown: assume the adverse fill.
            return LabelResult(
                Outcome.LOSS, i - entry_index + 1, entry, stop, (stop - entry) / entry * 100.0,
                mfe, mae, cfg.name, max_multiple,
            )
        if hit_target:
            return LabelResult(
                Outcome.WIN, i - entry_index + 1, entry, target, (target - entry) / entry * 100.0,
                mfe, mae, cfg.name, max_multiple,
            )
        if hit_stop:
            return LabelResult(
                Outcome.LOSS, i - entry_index + 1, entry, stop, (stop - entry) / entry * 100.0,
                mfe, mae, cfg.name, max_multiple,
            )

    # Horizon expired without touching a barrier.
    if available < cfg.horizon_bars:
        # Not enough future exists yet — this is not a TIMEOUT, it is unknown.
        return LabelResult(Outcome.UNRESOLVED, available, entry, float("nan"), 0.0, mfe, mae, cfg.name, max_multiple)

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
        max_multiple,
    )


ALL_LABEL_CONFIGS = DEFAULT_LABEL_CONFIGS + MOONSHOT_LABEL_CONFIGS


def label_config_by_name(name: str, default: LabelConfig | None = None) -> LabelConfig:
    """Look a label up by name across both families.

    Raises rather than silently falling back when an unknown name is given and no
    default is supplied: grading a journal under the wrong definition is not a
    mistake that shows up until the statistics are already wrong.
    """
    for cfg in ALL_LABEL_CONFIGS:
        if cfg.name == name:
            return cfg
    if default is not None:
        return default
    known = ", ".join(c.name for c in ALL_LABEL_CONFIGS)
    raise ValueError(f"unknown label config {name!r}. Known: {known}")
