"""Backtest metrics.

Win rate is listed but is deliberately not the headline. A 75% win rate with a
0.3 profit factor is a losing system; expectancy and profit factor say what
actually happened to the equity.

Every metric returns None rather than a placeholder when the sample cannot
support it. A Sharpe ratio computed from four trades is noise wearing a number's
clothes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

__all__ = ["TradeRecord", "BacktestMetrics", "compute_metrics"]

MIN_TRADES_FOR_RATIOS = 20


@dataclass(slots=True)
class TradeRecord:
    symbol: str
    entry_time_ms: int
    entry_price: float
    exit_price: float
    return_pct: float  # net of costs
    gross_return_pct: float
    outcome: str
    bars_held: int
    mfe_atr: float
    mae_atr: float
    score: float
    regime: str | None = None


@dataclass(slots=True)
class BacktestMetrics:
    trades: int
    wins: int
    losses: int
    timeouts: int
    win_rate: float | None
    avg_win_pct: float | None
    avg_loss_pct: float | None
    expectancy_pct: float | None
    profit_factor: float | None
    total_return_pct: float
    max_drawdown_pct: float | None
    sharpe: float | None
    sortino: float | None
    avg_bars_held: float | None
    avg_mfe_atr: float | None
    avg_mae_atr: float | None
    by_regime: dict[str, dict] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trades": self.trades,
            "wins": self.wins,
            # Named explicitly because the outcome tracker uses the *other*
            # definition — its win rate counts rows labelled WIN by the barrier,
            # while a backtest trade that timed out slightly green is a "win"
            # here. Two defensible definitions; one of them has to say which.
            "win_rate_basis": "share of trades with a positive net return (timeouts included)",
            "losses": self.losses,
            "timeouts": self.timeouts,
            "win_rate": _r(self.win_rate),
            "avg_win_pct": _r(self.avg_win_pct),
            "avg_loss_pct": _r(self.avg_loss_pct),
            "expectancy_pct": _r(self.expectancy_pct),
            "profit_factor": _r(self.profit_factor),
            "total_return_pct": _r(self.total_return_pct),
            "max_drawdown_pct": _r(self.max_drawdown_pct),
            "sharpe": _r(self.sharpe),
            "sortino": _r(self.sortino),
            "avg_bars_held": _r(self.avg_bars_held),
            "avg_mfe_atr": _r(self.avg_mfe_atr),
            "avg_mae_atr": _r(self.avg_mae_atr),
            "by_regime": self.by_regime,
            "notes": self.notes,
        }


def _r(x, nd: int = 4):
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def compute_metrics(trades: list[TradeRecord], *, per_trade_bars: int | None = None) -> BacktestMetrics:
    notes: list[str] = []
    n = len(trades)
    if n == 0:
        return BacktestMetrics(
            0, 0, 0, 0, None, None, None, None, None, 0.0, None, None, None, None, None, None,
            notes=["no trades generated — nothing to measure"],
        )

    returns = np.array([t.return_pct for t in trades], dtype=np.float64)
    wins = [t for t in trades if t.return_pct > 0]
    losses = [t for t in trades if t.return_pct < 0]
    timeouts = sum(1 for t in trades if t.outcome == "TIMEOUT")

    win_rate = len(wins) / n
    avg_win = float(np.mean([t.return_pct for t in wins])) if wins else None
    avg_loss = float(np.mean([t.return_pct for t in losses])) if losses else None
    expectancy = float(np.mean(returns))

    gross_profit = float(sum(t.return_pct for t in wins))
    gross_loss = float(abs(sum(t.return_pct for t in losses)))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (None if not wins else float("inf"))
    if profit_factor == float("inf"):
        profit_factor = None
        notes.append("profit factor undefined: no losing trades in the sample")

    # Compounded equity curve, one unit risked per trade sequentially.
    equity = np.cumprod(1.0 + returns / 100.0)
    total_return = float((equity[-1] - 1.0) * 100.0)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    max_dd = float(np.min(drawdown) * 100.0)

    sharpe = sortino = None
    if n >= MIN_TRADES_FOR_RATIOS:
        sd = float(np.std(returns, ddof=1))
        if sd > 0:
            # Per-trade Sharpe. Deliberately NOT annualised: trades are irregularly
            # spaced, and annualising an irregular series invents a time base.
            sharpe = expectancy / sd
        downside = returns[returns < 0]
        if downside.size >= 2:
            dsd = float(np.std(downside, ddof=1))
            if dsd > 0:
                sortino = expectancy / dsd
        notes.append("Sharpe/Sortino are per-trade, not annualised (trades are irregularly spaced).")
    else:
        notes.append(f"Sharpe/Sortino omitted: {n} trades is below the {MIN_TRADES_FOR_RATIOS}-trade minimum.")

    by_regime: dict[str, dict] = {}
    regimes = {t.regime for t in trades if t.regime}
    for regime in sorted(regimes):
        subset = [t for t in trades if t.regime == regime]
        sub_returns = np.array([t.return_pct for t in subset])
        by_regime[regime] = {
            "trades": len(subset),
            "win_rate": _r(sum(1 for t in subset if t.return_pct > 0) / len(subset)),
            "expectancy_pct": _r(float(np.mean(sub_returns))),
        }

    return BacktestMetrics(
        trades=n,
        wins=len(wins),
        losses=len(losses),
        timeouts=timeouts,
        win_rate=win_rate,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        expectancy_pct=expectancy,
        profit_factor=profit_factor,
        total_return_pct=total_return,
        max_drawdown_pct=max_dd,
        sharpe=sharpe,
        sortino=sortino,
        avg_bars_held=float(np.mean([t.bars_held for t in trades])),
        avg_mfe_atr=float(np.mean([t.mfe_atr for t in trades])),
        avg_mae_atr=float(np.mean([t.mae_atr for t in trades])),
        by_regime=by_regime,
        notes=notes,
    )
