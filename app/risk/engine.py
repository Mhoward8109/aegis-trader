"""
Risk Engine (spec §9) — has veto authority over the strategy engine. Nothing
in this codebase allows a strategy to bypass it (spec §34: order
authorization requires Strategy Signal AND Data Valid AND Risk Approved AND
Account Valid AND Broker Connected AND Market Session Valid AND No Circuit
Breaker AND Execution Mode Allows Trading — this module implements the
"Risk Approved" clause and exposes helpers for the others).

Explicitly implements the prohibitions in spec §9:
  - position size is a pure function of (equity, risk_pct, entry, stop);
    it is NEVER a function of yesterday's or today's P&L (no martingale,
    no revenge sizing, no doubling down).
  - stops are never removed by this module. RiskEngine has no method that
    deletes or widens a stop "because price is approaching it" — that
    capability simply does not exist here.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone


class RiskViolation(Exception):
    pass


@dataclasses.dataclass
class AccountState:
    equity: float
    buying_power: float
    open_positions: int
    open_position_symbols: set[str]
    sector_exposure_pct: dict[str, float]
    trades_today: int
    realized_pnl_today: float
    realized_pnl_week: float
    consecutive_losses: int


@dataclasses.dataclass
class CandidateRiskInput:
    ticker: str
    sector: str | None
    entry: float
    stop: float
    spread_pct: float
    avg_dollar_volume: float
    estimated_slippage_pct: float = 0.0


@dataclasses.dataclass
class RiskDecision:
    approved: bool
    position_size_shares: float
    risk_budget_usd: float
    reason: str
    rule_triggered: str | None = None
    inputs: dict | None = None


class RiskEngine:
    def __init__(self, risk_cfg: dict):
        self.cfg = risk_cfg

    # ---- position sizing (spec §9 formula) ---------------------------
    def compute_position_size(self, equity: float, entry: float, stop: float) -> tuple[float, float]:
        """Risk Budget = Equity * Allowed Risk%; Size = Risk Budget / |Entry-Stop|.
        Returns (shares, risk_budget_usd). Never looks at trade history."""
        risk_pct = self.cfg["max_risk_per_trade_pct"] / 100.0
        risk_budget = equity * risk_pct
        cap = self.cfg.get("max_risk_per_trade_usd")
        if cap is not None:
            risk_budget = min(risk_budget, cap)
        per_share_risk = abs(entry - stop)
        if per_share_risk <= 0:
            raise RiskViolation("Entry and stop are equal or invalid; cannot size a position with zero risk distance.")
        shares = risk_budget / per_share_risk
        return shares, risk_budget

    # ---- the single decision function --------------------------------
    def evaluate(self, candidate: CandidateRiskInput, account: AccountState) -> RiskDecision:
        inputs = dataclasses.asdict(candidate) | {"account": dataclasses.asdict(account)}

        def reject(rule: str, reason: str) -> RiskDecision:
            return RiskDecision(False, 0.0, 0.0, reason, rule, inputs)

        # --- hard gates, independent of sizing ---
        if account.consecutive_losses >= self.cfg["max_consecutive_losses"]:
            return reject(
                "max_consecutive_losses",
                f"{account.consecutive_losses} consecutive losses >= limit "
                f"{self.cfg['max_consecutive_losses']}. No sizing increase permitted; "
                f"trading paused for this strategy (no martingale, spec §9).",
            )

        if account.trades_today >= self.cfg["max_trades_per_day"]:
            return reject("max_trades_per_day", f"Already took {account.trades_today} trades today.")

        if account.open_positions >= self.cfg["max_concurrent_positions"]:
            return reject("max_concurrent_positions", f"{account.open_positions} positions already open.")

        if candidate.ticker in account.open_position_symbols:
            return reject("duplicate_position", f"Already have an open position in {candidate.ticker}.")

        daily_loss_limit = self._daily_loss_limit_usd(account.equity)
        if account.realized_pnl_today <= -abs(daily_loss_limit):
            return reject(
                "max_daily_loss",
                f"Realized P&L today {account.realized_pnl_today:.2f} breaches daily loss limit "
                f"-{daily_loss_limit:.2f}.",
            )

        weekly_loss_limit = self.cfg["max_weekly_loss_pct"] / 100.0 * account.equity
        if account.realized_pnl_week <= -abs(weekly_loss_limit):
            return reject("max_weekly_loss", f"Weekly P&L {account.realized_pnl_week:.2f} breaches limit.")

        if candidate.spread_pct > self.cfg["max_spread_pct"]:
            return reject("max_spread_pct", f"Spread {candidate.spread_pct:.3f}% > limit {self.cfg['max_spread_pct']}%.")

        if candidate.avg_dollar_volume < self.cfg["min_liquidity_avg_dollar_vol"]:
            return reject(
                "min_liquidity",
                f"Avg $ volume {candidate.avg_dollar_volume:,.0f} < floor {self.cfg['min_liquidity_avg_dollar_vol']:,.0f}.",
            )

        if candidate.estimated_slippage_pct > self.cfg["max_slippage_pct"]:
            return reject("max_slippage_pct", "Estimated slippage exceeds policy limit.")

        if candidate.sector and candidate.sector in account.sector_exposure_pct:
            projected_sector_pct = account.sector_exposure_pct[candidate.sector]
            if projected_sector_pct >= self.cfg["max_sector_exposure_pct"]:
                return reject("max_sector_exposure", f"Sector {candidate.sector} exposure already {projected_sector_pct}%.")

        # --- sizing (pure function of equity/entry/stop only) ---
        try:
            shares, risk_budget = self.compute_position_size(account.equity, candidate.entry, candidate.stop)
        except RiskViolation as e:
            return reject("invalid_risk_distance", str(e))

        # cap by max position % of account
        max_position_usd = self.cfg["max_position_pct_of_account"] / 100.0 * account.equity
        notional = shares * candidate.entry
        if notional > max_position_usd:
            shares = max_position_usd / candidate.entry
            notional = shares * candidate.entry

        # cap by buying power (liquidity/buying-power constraint applied AFTER
        # risk-based sizing, per spec §9)
        if notional > account.buying_power:
            shares = account.buying_power / candidate.entry
            notional = shares * candidate.entry

        if shares <= 0:
            return reject("non_positive_size", "Computed position size is zero or negative after constraints.")

        return RiskDecision(
            approved=True,
            position_size_shares=round(shares, 4),
            risk_budget_usd=round(risk_budget, 2),
            reason="All risk checks passed.",
            inputs=inputs,
        )

    def _daily_loss_limit_usd(self, equity: float) -> float:
        pct_limit = self.cfg["max_daily_loss_pct"] / 100.0 * equity
        usd_limit = self.cfg.get("max_daily_loss_usd")
        if usd_limit is not None:
            return min(pct_limit, usd_limit)
        return pct_limit
