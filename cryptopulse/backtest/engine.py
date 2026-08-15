"""Historical replay of the scoring engine.

The replay walks bar by bar. At each step the scorer sees `series.upto(t)` — only
candles that had *finished* by that timestamp. It is structurally impossible for
the engine to read a bar it would not have had in production, because the slice
is taken before the scorer is called and the scorer has no other data source.

Costs are applied to every trade and the assumptions are reported alongside the
results, because a backtest that ignores fees and slippage is a description of a
market that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.backtest.labels import DEFAULT_LABEL_CONFIGS, LabelConfig, Outcome, label_signal
from cryptopulse.backtest.metrics import BacktestMetrics, TradeRecord, compute_metrics
from cryptopulse.backtest.splits import chronological_split
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import OHLCVSeries, Timeframe
from cryptopulse.features.pipeline import AssetFeatures, TimeframeFeatures
from cryptopulse.features.regime import classify_regime
from cryptopulse.scoring.engine import ScoreEngine
from cryptopulse.scoring.states import SetupState

log = get_logger("backtest")

__all__ = ["CostModel", "BacktestConfig", "BacktestResult", "BacktestEngine"]


@dataclass(frozen=True, slots=True)
class CostModel:
    """Execution assumptions. Displayed with every result."""

    taker_fee_bps: float = 10.0  # 0.10% — Binance spot base taker rate
    slippage_bps: float = 8.0
    half_spread_bps: float = 2.0

    @property
    def round_trip_bps(self) -> float:
        # Fee and slippage are paid on both entry and exit; the half-spread is
        # crossed twice as well.
        return 2 * (self.taker_fee_bps + self.slippage_bps + self.half_spread_bps)

    def apply(self, gross_return_pct: float) -> float:
        return gross_return_pct - self.round_trip_bps / 100.0

    def describe(self) -> dict:
        return {
            "taker_fee_bps": self.taker_fee_bps,
            "slippage_bps": self.slippage_bps,
            "half_spread_bps": self.half_spread_bps,
            "round_trip_cost_pct": round(self.round_trip_bps / 100.0, 4),
            "note": (
                "Costs are a flat per-trade assumption, not a depth-aware simulation. "
                "Real slippage on a thin book will be worse than this."
            ),
        }


@dataclass(slots=True)
class BacktestConfig:
    min_score: float = 65.0
    max_pump_maturity: float = 65.0
    min_confidence: float = 0.0  # confidence is age-based and meaningless in replay
    allowed_states: tuple[SetupState, ...] = (SetupState.ARMED, SetupState.BREAKOUT, SetupState.RETEST)
    warmup_bars: int = 220
    step_bars: int = 1
    cooldown_bars: int = 12  # do not re-enter the same symbol every single bar
    label_configs: tuple[LabelConfig, ...] = tuple(DEFAULT_LABEL_CONFIGS)
    costs: CostModel = field(default_factory=CostModel)


@dataclass(slots=True)
class BacktestResult:
    label_name: str
    metrics: BacktestMetrics
    trades: list[TradeRecord]
    signals_generated: int
    signals_unresolved: int
    config: dict
    splits: dict[str, dict] = field(default_factory=dict)
    data_source: str = "unknown"
    synthetic: bool = False

    def to_dict(self) -> dict:
        return {
            "label": self.label_name,
            "data_source": self.data_source,
            "synthetic_data": self.synthetic,
            "signals_generated": self.signals_generated,
            "signals_unresolved": self.signals_unresolved,
            "metrics": self.metrics.to_dict(),
            "splits": self.splits,
            "config": self.config,
            "warning": (
                "RESULTS ARE FROM SYNTHETIC DATA — they measure the pipeline, not the strategy."
                if self.synthetic
                else None
            ),
        }


class BacktestEngine:
    """Replays the live scorer over history. Same code path, historical slices."""

    def __init__(self, settings: CryptoPulseSettings, config: BacktestConfig | None = None) -> None:
        self.settings = settings
        self.config = config or BacktestConfig()
        self.engine = ScoreEngine(settings)

    def _build_features_at(
        self, series_by_tf: dict[Timeframe, OHLCVSeries], primary_tf: Timeframe, ts_ms: int, quote_volume_24h: float | None
    ) -> AssetFeatures | None:
        """Assemble features using only bars closed at or before `ts_ms`."""
        per_tf: dict[Timeframe, TimeframeFeatures] = {}
        for tf, full in series_by_tf.items():
            sliced = full.upto(ts_ms)
            if len(sliced) < 60:
                continue
            try:
                per_tf[tf] = TimeframeFeatures.build(sliced, min_bars=60)
            except Exception:
                continue
        if primary_tf not in per_tf:
            return None
        return AssetFeatures(
            symbol=series_by_tf[primary_tf].symbol,
            primary_timeframe=primary_tf,
            timeframes=per_tf,
            quote_volume_24h=quote_volume_24h,
        )

    def run(
        self,
        series_by_symbol: dict[str, dict[Timeframe, OHLCVSeries]],
        *,
        primary_tf: Timeframe,
        label_config: LabelConfig | None = None,
        quote_volumes: dict[str, float] | None = None,
        data_source: str = "unknown",
        synthetic: bool = False,
    ) -> BacktestResult:
        cfg = self.config
        label_cfg = label_config or cfg.label_configs[0]
        quote_volumes = quote_volumes or {}

        trades: list[TradeRecord] = []
        signals = 0
        unresolved = 0

        for symbol, by_tf in series_by_symbol.items():
            primary = by_tf.get(primary_tf)
            if primary is None or len(primary) < cfg.warmup_bars + label_cfg.horizon_bars + 2:
                log.info("backtest_skip_symbol", symbol=symbol, reason="insufficient history")
                continue

            regime_label = None
            h4 = by_tf.get(Timeframe.H4)
            if h4 is not None and len(h4) >= 60:
                regime_label = classify_regime(h4.high, h4.low, h4.close, reference_symbol=symbol).trend.value

            n = len(primary)
            last_entry_index = -10_000

            for i in range(cfg.warmup_bars, n - 1, cfg.step_bars):
                if i - last_entry_index < cfg.cooldown_bars:
                    continue

                decision_ts = int(primary.close_time_ms[i])
                af = self._build_features_at(by_tf, primary_tf, decision_ts, quote_volumes.get(symbol))
                if af is None:
                    continue

                # `now_ms` is the decision time, so confidence's freshness term is
                # measured as it would have been live rather than against today.
                result = self.engine.score(af, decision_ts)
                if result.final_score < cfg.min_score:
                    continue
                if result.maturity.score > cfg.max_pump_maturity:
                    continue
                if result.state.state not in cfg.allowed_states:
                    continue

                atr_at_signal = af.primary.atr14
                if not atr_at_signal:
                    continue

                signals += 1
                label = label_signal(
                    primary.high, primary.low, primary.close, primary.open, i, atr_at_signal, label_cfg
                )
                if label.outcome is Outcome.UNRESOLVED:
                    unresolved += 1
                    continue

                net = cfg.costs.apply(label.return_pct)
                trades.append(
                    TradeRecord(
                        symbol=symbol,
                        entry_time_ms=int(primary.open_time_ms[i + 1]),
                        entry_price=label.entry_price,
                        exit_price=label.exit_price,
                        return_pct=net,
                        gross_return_pct=label.return_pct,
                        outcome=label.outcome.value,
                        bars_held=label.bars_held,
                        mfe_atr=label.mfe_atr,
                        mae_atr=label.mae_atr,
                        score=result.final_score,
                        regime=regime_label,
                    )
                )
                last_entry_index = i

        trades.sort(key=lambda t: t.entry_time_ms)
        metrics = compute_metrics(trades)

        # Chronological split report: the out-of-sample block is what matters.
        splits: dict[str, dict] = {}
        if trades:
            for split in chronological_split(len(trades)):
                subset = trades[split.start : split.end]
                if subset:
                    splits[split.name] = compute_metrics(subset).to_dict()

        return BacktestResult(
            label_name=label_cfg.name,
            metrics=metrics,
            trades=trades,
            signals_generated=signals,
            signals_unresolved=unresolved,
            config={
                "label": label_cfg.describe(),
                "min_score": cfg.min_score,
                "max_pump_maturity": cfg.max_pump_maturity,
                "allowed_states": [s.value for s in cfg.allowed_states],
                "warmup_bars": cfg.warmup_bars,
                "cooldown_bars": cfg.cooldown_bars,
                "costs": cfg.costs.describe(),
                "engine_version": self.engine.scoring.engine_version,
                "weights_fingerprint": self.engine.weights_fingerprint,
            },
            splits=splits,
            data_source=data_source,
            synthetic=synthetic,
        )
