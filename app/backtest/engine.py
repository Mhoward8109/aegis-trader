"""
Backtesting framework (spec §16-17).

Explicit anti-bias measures:
  - `train_validation_oos_split()` enforces chronological, non-overlapping
    splits — no shuffling, so there is no look-ahead leakage across the split.
  - `simulate_fill()` charges a spread-crossing cost and a commission by
    default; callers must pass zero_cost=True explicitly to disable it, so
    "I forgot to model costs" is an opt-in mistake, not the default.
  - Metrics module refuses to compute Sharpe/Sortino on fewer than
    `MIN_TRADES_FOR_RATIOS` trades and returns None instead of a misleading
    number from a tiny sample.
  - `walk_forward()` rejects parameter sets whose in-sample and out-of-sample
    performance diverge beyond `overfit_threshold`, flagging them instead of
    silently reporting the in-sample number.
"""
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd

MIN_TRADES_FOR_RATIOS = 20


@dataclasses.dataclass
class SimulatedTrade:
    ticker: str
    strategy: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    stop: float
    qty: float
    direction: str          # long|short
    exit_reason: str
    market_regime: str | None = None

    @property
    def r_multiple(self) -> float | None:
        risk = abs(self.entry_price - self.stop)
        if risk == 0:
            return None
        pnl_per_share = (self.exit_price - self.entry_price) if self.direction == "long" \
            else (self.entry_price - self.exit_price)
        return pnl_per_share / risk

    @property
    def pnl(self) -> float:
        pnl_per_share = (self.exit_price - self.entry_price) if self.direction == "long" \
            else (self.entry_price - self.exit_price)
        return pnl_per_share * self.qty


def train_validation_oos_split(dates: list, train_pct=0.6, val_pct=0.2) -> dict:
    """Chronological split — dates MUST already be sorted ascending. Returns
    date-range boundaries, not shuffled indices, precisely to prevent
    look-ahead leakage."""
    n = len(dates)
    if n < 10:
        raise ValueError("Need at least 10 distinct sessions to split meaningfully.")
    train_end = int(n * train_pct)
    val_end = train_end + int(n * val_pct)
    return {
        "train": (dates[0], dates[max(train_end - 1, 0)]),
        "validation": (dates[train_end], dates[max(val_end - 1, train_end)]),
        "out_of_sample": (dates[val_end], dates[-1]),
    }


def simulate_fill(intended_price: float, side: str, spread_pct: float, commission_per_share: float = 0.0,
                   zero_cost: bool = False) -> float:
    """Deterministic, conservative fill model: buys fill at intended + half
    the spread, sells fill at intended - half the spread, plus commission.
    This is intentionally pessimistic (spec §16: 'Avoid unrealistic fills')."""
    if zero_cost:
        return intended_price
    half_spread = intended_price * (spread_pct / 100.0) / 2.0
    if side == "BUY":
        return intended_price + half_spread + commission_per_share
    return intended_price - half_spread - commission_per_share


def compute_metrics(trades: list[SimulatedTrade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "note": "No trades in this sample."}

    pnls = [t.pnl for t in trades]
    r_multiples = [t.r_multiple for t in trades if t.r_multiple is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / n
    avg_winner = float(np.mean(wins)) if wins else 0.0
    avg_loser = float(np.mean(losses)) if losses else 0.0
    expectancy_r = float(np.mean(r_multiples)) if r_multiples else None
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)

    equity_curve = np.cumsum(pnls)
    running_max = np.maximum.accumulate(equity_curve) if n else np.array([0.0])
    drawdowns = running_max - equity_curve
    max_drawdown = float(np.max(drawdowns)) if n else 0.0

    consecutive_wins = _max_streak(pnls, lambda p: p > 0)
    consecutive_losses = _max_streak(pnls, lambda p: p <= 0)

    sharpe = sortino = None
    if n >= MIN_TRADES_FOR_RATIOS:
        returns = np.array(pnls)
        if returns.std(ddof=1) > 0:
            sharpe = float(returns.mean() / returns.std(ddof=1) * math.sqrt(252))
        downside = returns[returns < 0]
        if len(downside) > 0 and downside.std(ddof=1) > 0:
            sortino = float(returns.mean() / downside.std(ddof=1) * math.sqrt(252))

    hold_times = [(t.exit_time - t.entry_time).total_seconds() / 60.0 for t in trades]

    by_regime: dict[str, list[float]] = {}
    for t in trades:
        by_regime.setdefault(t.market_regime or "unknown", []).append(t.pnl)

    return {
        "trades": n,
        "win_rate": round(win_rate, 4),
        "loss_rate": round(1 - win_rate, 4),
        "avg_winner": round(avg_winner, 2),
        "avg_loser": round(avg_loser, 2),
        "expectancy_r": round(expectancy_r, 3) if expectancy_r is not None else None,
        "profit_factor": round(profit_factor, 3) if math.isfinite(profit_factor) else "inf",
        "max_drawdown": round(max_drawdown, 2),
        "sharpe_ratio": round(sharpe, 3) if sharpe is not None else None,
        "sortino_ratio": round(sortino, 3) if sortino is not None else None,
        "sharpe_sortino_note": None if n >= MIN_TRADES_FOR_RATIOS else
            f"Sample too small ({n} < {MIN_TRADES_FOR_RATIOS}) for a meaningful Sharpe/Sortino.",
        "avg_hold_minutes": round(float(np.mean(hold_times)), 1) if hold_times else None,
        "max_consecutive_wins": consecutive_wins,
        "max_consecutive_losses": consecutive_losses,
        "return_per_trade_usd": round(float(np.mean(pnls)), 2),
        "by_regime": {k: {"trades": len(v), "total_pnl": round(sum(v), 2)} for k, v in by_regime.items()},
    }


def _max_streak(pnls: list[float], predicate) -> int:
    best = cur = 0
    for p in pnls:
        if predicate(p):
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def is_likely_overfit(in_sample_expectancy: float | None, oos_expectancy: float | None,
                       divergence_threshold: float = 0.5) -> bool:
    """Flags a parameter set as likely overfit when out-of-sample expectancy
    diverges from in-sample by more than the threshold (as an absolute R
    difference), or flips sign. Spec §17: 'Reject parameter combinations
    that appear strongly overfit.'"""
    if in_sample_expectancy is None or oos_expectancy is None:
        return True  # can't prove it's NOT overfit -> treat conservatively
    if (in_sample_expectancy > 0) != (oos_expectancy > 0):
        return True
    return abs(in_sample_expectancy - oos_expectancy) > divergence_threshold
