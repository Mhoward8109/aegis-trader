"""
Risk controls and deterministic position sizing.

The position-size calculation deliberately has no access to P&L or loss
history.  A losing streak may veto new trades through the consecutive-loss
limit, but it can never increase the size of a later trade (no martingale or
averaging-down path).
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any


class RiskViolation(Exception):
    """Raised only by the pure position-sizing helper for invalid inputs."""


@dataclasses.dataclass
class AccountState:
    """Account inputs required to make a risk decision.

    ``None`` explicitly means the value was not supplied, which is distinct
    from a genuine numeric zero.  :class:`RiskEngine` fails closed for any
    missing field used by a limit.  Callers must therefore pass actual
    journal/account aggregates rather than substituting a placeholder zero.
    """

    equity: float | None
    buying_power: float | None
    open_positions: int | None
    open_position_symbols: set[str] | None
    sector_exposure_pct: dict[str, float] | None
    trades_today: int | None
    realized_pnl_today: float | None
    realized_pnl_week: float | None
    consecutive_losses: int | None


@dataclasses.dataclass
class CandidateRiskInput:
    ticker: str
    sector: str | None
    entry: float
    stop: float
    spread_pct: float
    avg_dollar_volume: float
    estimated_slippage_pct: float = 0.0
    direction: str = "long"


@dataclasses.dataclass
class RiskDecision:
    approved: bool
    position_size_shares: int
    risk_budget_usd: float
    reason: str
    rule_triggered: str | None = None
    inputs: dict[str, Any] | None = None


class RiskEngine:
    """Apply non-bypassable risk limits and return an auditable decision."""

    _REQUIRED_ACCOUNT_FIELDS = (
        "equity",
        "buying_power",
        "open_positions",
        "open_position_symbols",
        "sector_exposure_pct",
        "trades_today",
        "realized_pnl_today",
        "realized_pnl_week",
        "consecutive_losses",
    )

    def __init__(self, risk_cfg: dict):
        self.cfg = risk_cfg

    # ---- position sizing (pure, no account history) -----------------
    def compute_position_size(
        self, equity: float, entry: float, stop: float, direction: str = "long"
    ) -> tuple[int, float]:
        """Return risk-based whole shares and the capped dollar-risk budget.

        The calculation is exactly ``risk_budget / stop_distance``, rounded
        *down* to whole shares.  Position/notional, buying-power, concentration
        and sector caps require account data and are applied by ``evaluate``.
        """
        direction = self._normalise_direction(direction)
        self._validate_entry_stop(entry, stop, direction)
        if not self._is_finite_positive(equity):
            raise RiskViolation(f"equity must be finite and positive; received {equity!r}.")

        risk_budget = equity * (self.cfg["max_risk_per_trade_pct"] / 100.0)
        dollar_cap = self.cfg.get("max_risk_per_trade_usd")
        if dollar_cap is not None:
            if not self._is_finite_positive(dollar_cap):
                raise RiskViolation(
                    "max_risk_per_trade_usd must be finite and positive when configured; "
                    f"received {dollar_cap!r}."
                )
            risk_budget = min(risk_budget, float(dollar_cap))

        stop_distance = abs(entry - stop)
        return math.floor(risk_budget / stop_distance), risk_budget

    # ---- the single decision function --------------------------------
    def evaluate(self, candidate: CandidateRiskInput, account: AccountState) -> RiskDecision:
        inputs = self._decision_inputs(candidate, account)

        def reject(
            rule: str,
            reason: str,
            *,
            observed: Any = None,
            limit: Any = None,
            calculation: dict[str, Any] | None = None,
            risk_budget: float = 0.0,
        ) -> RiskDecision:
            inputs["trigger"] = {
                "rule": rule,
                "observed": observed,
                "limit": limit,
                "calculation": calculation or {},
            }
            return RiskDecision(False, 0, round(risk_budget, 2), reason, rule, inputs)

        # The distinction between an actual zero and absent account data is
        # intentional: zeros pass; None rejects.  Never turn unknown data into
        # an unlimited/zero-loss account state.
        for field in self._REQUIRED_ACCOUNT_FIELDS:
            value = getattr(account, field)
            if value is None:
                return reject(
                    f"missing_{field}",
                    f"Required AccountState.{field} was not supplied (None); refusing to "
                    "treat missing account data as zero or unlimited.",
                    observed=None,
                    limit="required",
                )

        if not self._is_finite_positive(account.equity):
            return reject(
                "invalid_equity",
                f"Equity must be finite and > 0; received {account.equity!r}.",
                observed=account.equity,
                limit="> 0",
            )
        if not self._is_finite_nonnegative(account.buying_power):
            return reject(
                "invalid_buying_power",
                f"Buying power must be finite and >= 0; received {account.buying_power!r}.",
                observed=account.buying_power,
                limit=">= 0",
            )
        if not isinstance(account.open_positions, int) or account.open_positions < 0:
            return reject(
                "invalid_open_positions",
                f"Open-position count must be a non-negative integer; received {account.open_positions!r}.",
                observed=account.open_positions,
                limit=">= 0 integer",
            )
        if not isinstance(account.trades_today, int) or account.trades_today < 0:
            return reject(
                "invalid_trades_today",
                f"Trades today must be a non-negative integer; received {account.trades_today!r}.",
                observed=account.trades_today,
                limit=">= 0 integer",
            )
        if not isinstance(account.consecutive_losses, int) or account.consecutive_losses < 0:
            return reject(
                "invalid_consecutive_losses",
                f"Consecutive losses must be a non-negative integer; received "
                f"{account.consecutive_losses!r}.",
                observed=account.consecutive_losses,
                limit=">= 0 integer",
            )
        if not self._is_finite(account.realized_pnl_today) or not self._is_finite(account.realized_pnl_week):
            return reject(
                "invalid_realized_pnl",
                "Realized daily and weekly P&L must both be finite numeric values; "
                f"received today={account.realized_pnl_today!r}, week={account.realized_pnl_week!r}.",
                observed={
                    "realized_pnl_today": account.realized_pnl_today,
                    "realized_pnl_week": account.realized_pnl_week,
                },
                limit="finite numeric values",
            )
        if not isinstance(account.open_position_symbols, set):
            return reject(
                "invalid_open_position_symbols",
                "Open-position symbols must be a set supplied from the account snapshot.",
                observed=type(account.open_position_symbols).__name__,
                limit="set[str]",
            )
        if not isinstance(account.sector_exposure_pct, dict) or any(
            not self._is_finite_nonnegative(value) for value in account.sector_exposure_pct.values()
        ):
            return reject(
                "invalid_sector_exposure_pct",
                "Sector exposures must be a mapping of sector names to finite non-negative percentages.",
                observed=account.sector_exposure_pct,
                limit="dict[str, finite >= 0]",
            )
        if not candidate.sector:
            return reject(
                "missing_sector",
                "Candidate sector was not supplied; per-sector exposure cannot be verified.",
                observed=candidate.sector,
                limit="non-empty sector",
            )
        if not self._is_finite_positive(candidate.entry):
            return reject(
                "invalid_entry",
                f"Entry must be finite and > 0; received {candidate.entry!r}.",
                observed=candidate.entry,
                limit="> 0",
            )
        if not self._is_finite_positive(candidate.stop):
            return reject(
                "invalid_stop",
                f"Stop must be finite and > 0; received {candidate.stop!r}.",
                observed=candidate.stop,
                limit="> 0",
            )
        try:
            direction = self._normalise_direction(candidate.direction)
            self._validate_entry_stop(candidate.entry, candidate.stop, direction)
        except RiskViolation as error:
            rule = self._stop_rule_for_error(str(error))
            return reject(
                rule,
                str(error),
                observed={"entry": candidate.entry, "stop": candidate.stop, "direction": candidate.direction},
                limit="long: stop < entry; short: stop > entry; non-zero distance",
                calculation={"signed_stop_distance": self._signed_stop_distance(candidate.entry, candidate.stop, direction if "direction" in locals() else "long")},
            )

        # ------------------------------------------------------------------
        # Stop-distance plausibility.
        #
        # A stop can be on the correct side of entry and still be nonsense. This
        # gate was added after integration testing produced setups with stops 84%
        # away from entry: the scanner's quote and the bar series disagreed about
        # the price scale, so `entry` came from one source and `stop` from
        # another. Every earlier gate passed -- the stop WAS below entry, the
        # distance WAS non-zero -- and sizing dutifully returned 1-3 shares
        # instead of refusing.
        #
        # Sizing shrinking to a tiny quantity is not a safety net. It converts a
        # data-integrity fault into a small live position, which is worse than an
        # error, because it looks like a decision. Reject instead.
        # ------------------------------------------------------------------
        max_stop_distance_pct = self.cfg.get("max_stop_distance_pct")
        if max_stop_distance_pct is not None:
            stop_distance_pct = abs(candidate.entry - candidate.stop) / candidate.entry * 100.0
            if stop_distance_pct > max_stop_distance_pct:
                return reject(
                    "max_stop_distance_pct",
                    f"Stop is {stop_distance_pct:.2f}% from entry, above the limit of "
                    f"{max_stop_distance_pct:.2f}%. A stop this far away usually means "
                    f"entry and stop were derived from inconsistent price data rather "
                    f"than a wide-but-intentional stop.",
                    observed=round(stop_distance_pct, 4),
                    limit=max_stop_distance_pct,
                    calculation={
                        "entry": candidate.entry,
                        "stop": candidate.stop,
                        "stop_distance": abs(candidate.entry - candidate.stop),
                    },
                )

        min_stop_distance_pct = self.cfg.get("min_stop_distance_pct")
        if min_stop_distance_pct is not None:
            stop_distance_pct = abs(candidate.entry - candidate.stop) / candidate.entry * 100.0
            if stop_distance_pct < min_stop_distance_pct:
                return reject(
                    "min_stop_distance_pct",
                    f"Stop is only {stop_distance_pct:.4f}% from entry, below the minimum "
                    f"of {min_stop_distance_pct:.4f}%. A stop this tight sizes the position "
                    f"enormously (risk_budget / stop_distance) and will be taken out by "
                    f"normal spread noise.",
                    observed=round(stop_distance_pct, 6),
                    limit=min_stop_distance_pct,
                    calculation={
                        "entry": candidate.entry,
                        "stop": candidate.stop,
                        "stop_distance": abs(candidate.entry - candidate.stop),
                    },
                )

        # Independent gates with numeric observations and thresholds.
        if account.consecutive_losses >= self.cfg["max_consecutive_losses"]:
            return reject(
                "max_consecutive_losses",
                f"Consecutive losses {account.consecutive_losses} >= limit "
                f"{self.cfg['max_consecutive_losses']}; trading is paused and no sizing "
                "increase is permitted.",
                observed=account.consecutive_losses,
                limit=self.cfg["max_consecutive_losses"],
            )
        if account.trades_today >= self.cfg["max_trades_per_day"]:
            return reject(
                "max_trades_per_day",
                f"Trades today {account.trades_today} >= limit {self.cfg['max_trades_per_day']}.",
                observed=account.trades_today,
                limit=self.cfg["max_trades_per_day"],
            )
        if account.open_positions >= self.cfg["max_concurrent_positions"]:
            return reject(
                "max_concurrent_positions",
                f"Open positions {account.open_positions} >= limit "
                f"{self.cfg['max_concurrent_positions']}.",
                observed=account.open_positions,
                limit=self.cfg["max_concurrent_positions"],
            )
        if candidate.ticker in account.open_position_symbols:
            return reject(
                "duplicate_position",
                f"Ticker {candidate.ticker} is already open; one-symbol concentration is limited to one position.",
                observed=candidate.ticker,
                limit="not already open",
            )

        daily_loss_limit = self._daily_loss_limit_usd(account.equity)
        if account.realized_pnl_today <= -daily_loss_limit:
            return reject(
                "max_daily_loss",
                f"Realized P&L today {account.realized_pnl_today:.2f} <= daily loss limit "
                f"-{daily_loss_limit:.2f}.",
                observed=account.realized_pnl_today,
                limit=-daily_loss_limit,
                calculation={"equity": account.equity, "loss_limit_usd": daily_loss_limit},
            )
        weekly_loss_limit = self.cfg["max_weekly_loss_pct"] / 100.0 * account.equity
        if account.realized_pnl_week <= -weekly_loss_limit:
            return reject(
                "max_weekly_loss",
                f"Realized P&L this week {account.realized_pnl_week:.2f} <= weekly loss limit "
                f"-{weekly_loss_limit:.2f}.",
                observed=account.realized_pnl_week,
                limit=-weekly_loss_limit,
                calculation={"equity": account.equity, "loss_limit_usd": weekly_loss_limit},
            )
        if not self._is_finite_nonnegative(candidate.spread_pct):
            return reject(
                "invalid_spread_pct",
                f"Spread percentage must be finite and >= 0; received {candidate.spread_pct!r}.",
                observed=candidate.spread_pct,
                limit=">= 0",
            )
        if candidate.spread_pct > self.cfg["max_spread_pct"]:
            return reject(
                "max_spread_pct",
                f"Spread {candidate.spread_pct:.3f}% > limit {self.cfg['max_spread_pct']:.3f}%.",
                observed=candidate.spread_pct,
                limit=self.cfg["max_spread_pct"],
            )
        if not self._is_finite_nonnegative(candidate.avg_dollar_volume):
            return reject(
                "invalid_avg_dollar_volume",
                f"Average dollar volume must be finite and >= 0; received {candidate.avg_dollar_volume!r}.",
                observed=candidate.avg_dollar_volume,
                limit=">= 0",
            )
        if candidate.avg_dollar_volume < self.cfg["min_liquidity_avg_dollar_vol"]:
            return reject(
                "min_liquidity",
                f"Average dollar volume {candidate.avg_dollar_volume:,.2f} < minimum "
                f"{self.cfg['min_liquidity_avg_dollar_vol']:,.2f}.",
                observed=candidate.avg_dollar_volume,
                limit=self.cfg["min_liquidity_avg_dollar_vol"],
            )
        if not self._is_finite_nonnegative(candidate.estimated_slippage_pct):
            return reject(
                "invalid_estimated_slippage_pct",
                "Estimated slippage percentage must be finite and >= 0; "
                f"received {candidate.estimated_slippage_pct!r}.",
                observed=candidate.estimated_slippage_pct,
                limit=">= 0",
            )
        if candidate.estimated_slippage_pct > self.cfg["max_slippage_pct"]:
            return reject(
                "max_slippage_pct",
                f"Estimated slippage {candidate.estimated_slippage_pct:.3f}% > limit "
                f"{self.cfg['max_slippage_pct']:.3f}%.",
                observed=candidate.estimated_slippage_pct,
                limit=self.cfg["max_slippage_pct"],
            )

        # Risk budget / stop distance is always the starting point.  Every
        # applicable cap is then applied to that result and rounded down once.
        try:
            risk_shares, risk_budget = self.compute_position_size(
                account.equity, candidate.entry, candidate.stop, direction
            )
        except RiskViolation as error:  # defensive; validated above
            return reject("invalid_risk_distance", str(error))

        max_position_usd = self.cfg["max_position_pct_of_account"] / 100.0 * account.equity
        max_position_shares = math.floor(max_position_usd / candidate.entry)

        # A separately configured concentration cap is preferred.  Before
        # configuration adds that key, the existing max-position percentage is
        # the conservative effective symbol concentration cap.
        concentration_pct = self.cfg.get(
            "max_concentration_per_symbol_pct", self.cfg["max_position_pct_of_account"]
        )
        max_concentration_usd = concentration_pct / 100.0 * account.equity
        max_concentration_shares = math.floor(max_concentration_usd / candidate.entry)
        buying_power_shares = math.floor(account.buying_power / candidate.entry)

        current_sector_pct = account.sector_exposure_pct.get(candidate.sector, 0.0)
        sector_limit_pct = self.cfg["max_sector_exposure_pct"]
        remaining_sector_pct = sector_limit_pct - current_sector_pct
        if remaining_sector_pct <= 0:
            return reject(
                "max_sector_exposure",
                f"Sector {candidate.sector} exposure {current_sector_pct:.3f}% >= limit "
                f"{sector_limit_pct:.3f}%.",
                observed=current_sector_pct,
                limit=sector_limit_pct,
                calculation={"sector": candidate.sector, "remaining_sector_pct": remaining_sector_pct},
                risk_budget=risk_budget,
            )
        sector_capacity_usd = account.equity * remaining_sector_pct / 100.0
        sector_capacity_shares = math.floor(sector_capacity_usd / candidate.entry)

        caps = {
            "risk_budget_shares": risk_shares,
            "max_position_size_shares": max_position_shares,
            "max_concentration_per_symbol_shares": max_concentration_shares,
            "buying_power_shares": buying_power_shares,
            "sector_exposure_shares": sector_capacity_shares,
        }
        shares = min(caps.values())
        inputs["sizing"] = {
            "risk_budget_usd": risk_budget,
            "stop_distance": abs(candidate.entry - candidate.stop),
            "raw_risk_budget_shares": risk_shares,
            "caps_shares": caps,
            "selected_whole_shares": shares,
            "max_position_usd": max_position_usd,
            "max_concentration_per_symbol_usd": max_concentration_usd,
            "buying_power_usd": account.buying_power,
            "current_sector_exposure_pct": current_sector_pct,
            "sector_limit_pct": sector_limit_pct,
            "projected_sector_exposure_pct": current_sector_pct + shares * candidate.entry / account.equity * 100.0,
        }
        if shares <= 0:
            binding_cap = min(caps, key=caps.get)
            return reject(
                "minimum_whole_share_size",
                f"Whole-share sizing rounded down to {shares}; the binding cap "
                f"{binding_cap} is {caps[binding_cap]} shares, so no purchasable share remains.",
                observed=shares,
                limit=">= 1 whole share",
                calculation=inputs["sizing"],
                risk_budget=risk_budget,
            )

        return RiskDecision(
            approved=True,
            position_size_shares=shares,
            risk_budget_usd=round(risk_budget, 2),
            reason=(
                f"All risk checks passed; {shares} whole shares selected from risk-budget "
                f"size {risk_shares} after applicable caps."
            ),
            inputs=inputs,
        )

    def _daily_loss_limit_usd(self, equity: float) -> float:
        pct_limit = self.cfg["max_daily_loss_pct"] / 100.0 * equity
        usd_limit = self.cfg.get("max_daily_loss_usd")
        return min(pct_limit, float(usd_limit)) if usd_limit is not None else pct_limit

    def _decision_inputs(self, candidate: CandidateRiskInput, account: AccountState) -> dict[str, Any]:
        """Create JSON-safe audit inputs before evaluating a single gate."""
        account_inputs = dataclasses.asdict(account)
        if isinstance(account_inputs.get("open_position_symbols"), set):
            account_inputs["open_position_symbols"] = sorted(account_inputs["open_position_symbols"])
        return {
            "candidate": dataclasses.asdict(candidate),
            "account": account_inputs,
            "computed_limits": {
                "max_risk_per_trade_pct": self.cfg.get("max_risk_per_trade_pct"),
                "max_risk_per_trade_usd": self.cfg.get("max_risk_per_trade_usd"),
                "max_position_pct_of_account": self.cfg.get("max_position_pct_of_account"),
                "max_concentration_per_symbol_pct": self.cfg.get(
                    "max_concentration_per_symbol_pct", self.cfg.get("max_position_pct_of_account")
                ),
                "max_concurrent_positions": self.cfg.get("max_concurrent_positions"),
                "max_daily_loss_pct": self.cfg.get("max_daily_loss_pct"),
                "max_daily_loss_usd": self.cfg.get("max_daily_loss_usd"),
                "max_weekly_loss_pct": self.cfg.get("max_weekly_loss_pct"),
                "max_trades_per_day": self.cfg.get("max_trades_per_day"),
                "max_consecutive_losses": self.cfg.get("max_consecutive_losses"),
                "min_liquidity_avg_dollar_vol": self.cfg.get("min_liquidity_avg_dollar_vol"),
                "max_spread_pct": self.cfg.get("max_spread_pct"),
                "max_slippage_pct": self.cfg.get("max_slippage_pct"),
                "max_sector_exposure_pct": self.cfg.get("max_sector_exposure_pct"),
            },
        }

    @staticmethod
    def _normalise_direction(direction: str) -> str:
        normalised = direction.lower()
        if normalised in {"long", "buy"}:
            return "long"
        if normalised in {"short", "sell"}:
            return "short"
        raise RiskViolation(f"Invalid direction {direction!r}; expected 'long' or 'short'.")

    @staticmethod
    def _validate_entry_stop(entry: float, stop: float, direction: str) -> None:
        if not RiskEngine._is_finite_positive(entry):
            raise RiskViolation(f"Entry must be finite and > 0; received {entry!r}.")
        if not RiskEngine._is_finite_positive(stop):
            raise RiskViolation(f"Stop must be finite and > 0; received {stop!r}.")
        if entry == stop:
            raise RiskViolation(
                f"Zero stop distance: entry {entry:.6f} equals stop {stop:.6f}; trade rejected."
            )
        signed_distance = RiskEngine._signed_stop_distance(entry, stop, direction)
        if signed_distance < 0:
            raise RiskViolation(
                f"Stop is on the wrong side of entry for {direction}: entry={entry:.6f}, "
                f"stop={stop:.6f}, signed stop distance={signed_distance:.6f} (negative)."
            )

    @staticmethod
    def _signed_stop_distance(entry: float, stop: float, direction: str) -> float:
        return entry - stop if direction == "long" else stop - entry

    @staticmethod
    def _stop_rule_for_error(message: str) -> str:
        if message.startswith("Zero stop distance"):
            return "zero_stop_distance"
        if message.startswith("Stop is on the wrong side"):
            return "stop_wrong_side"
        if message.startswith("Invalid direction"):
            return "invalid_direction"
        if message.startswith("Entry"):
            return "invalid_entry"
        return "invalid_stop"

    @staticmethod
    def _is_finite(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    @classmethod
    def _is_finite_positive(cls, value: Any) -> bool:
        return cls._is_finite(value) and value > 0

    @classmethod
    def _is_finite_nonnegative(cls, value: Any) -> bool:
        return cls._is_finite(value) and value >= 0
