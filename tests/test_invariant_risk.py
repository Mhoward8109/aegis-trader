"""Position-sizing and risk-limit safety invariants."""
from __future__ import annotations

import pytest

from app.risk.engine import AccountState, CandidateRiskInput, RiskEngine


CFG = {
    "max_risk_per_trade_pct": 1.0, "max_risk_per_trade_usd": None,
    "max_daily_loss_pct": 2.0, "max_daily_loss_usd": None, "max_weekly_loss_pct": 5.0,
    "max_trades_per_day": 3, "max_concurrent_positions": 2,
    "max_position_pct_of_account": 80.0, "max_sector_exposure_pct": 40.0,
    "max_spread_pct": 0.5, "min_liquidity_avg_dollar_vol": 5_000_000,
    "max_slippage_pct": 1.0, "max_consecutive_losses": 3,
    "max_stop_distance_pct": 10.0, "min_stop_distance_pct": 0.2,
}


def account(**overrides):
    values = dict(equity=100_000, buying_power=100_000, open_positions=0,
                  open_position_symbols=set(), sector_exposure_pct={}, trades_today=0,
                  realized_pnl_today=0, realized_pnl_week=0, consecutive_losses=0)
    values.update(overrides)
    return AccountState(**values)


def candidate(**overrides):
    values = dict(ticker="SAFE", sector="Tech", entry=100.0, stop=99.0,
                  spread_pct=0.1, avg_dollar_volume=10_000_000,
                  estimated_slippage_pct=0.1, direction="long")
    values.update(overrides)
    return CandidateRiskInput(**values)


@pytest.mark.parametrize(
    ("candidate_overrides", "account_overrides", "rule"),
    [
        ({}, {"realized_pnl_today": -2_000}, "max_daily_loss"),
        ({}, {"realized_pnl_week": -5_000}, "max_weekly_loss"),
        ({}, {"trades_today": 3}, "max_trades_per_day"),
        ({}, {"open_positions": 2}, "max_concurrent_positions"),
        ({}, {"consecutive_losses": 3}, "max_consecutive_losses"),
        ({"spread_pct": 0.51}, {}, "max_spread_pct"),
        ({"avg_dollar_volume": 4_999_999}, {}, "min_liquidity"),
        ({}, {"buying_power": 0}, "minimum_whole_share_size"),
        ({"stop": 100}, {}, "zero_stop_distance"),
        ({"stop": 101}, {}, "stop_wrong_side"),
        ({"stop": 80}, {}, "max_stop_distance_pct"),
        ({"stop": 99.9}, {}, "min_stop_distance_pct"),
    ],
)
def test_each_load_bearing_risk_limit_refuses_unsafe_entry(candidate_overrides, account_overrides, rule):
    """Each configured risk limit must veto its unsafe input, or removing that limit would permit the prohibited trade."""
    decision = RiskEngine(CFG).evaluate(candidate(**candidate_overrides), account(**account_overrides))
    assert not decision.approved
    assert decision.rule_triggered == rule


def test_sizing_is_floor_of_risk_budget_divided_by_stop_then_capped():
    """Sizing must floor risk-budget divided by stop distance before caps, or fractional or excess risk could reach the broker."""
    engine = RiskEngine(CFG)
    shares, budget = engine.compute_position_size(100_000, 100, 97)
    assert budget == 1_000
    assert shares == 333
    decision = engine.evaluate(candidate(stop=99.7), account(buying_power=250))
    assert decision.approved
    assert decision.inputs["sizing"]["raw_risk_budget_shares"] == 3333
    assert decision.position_size_shares == 2


def test_losses_never_increase_size_no_martingale():
    """Loss history must never increase position size, or a martingale path could amplify losses after a losing streak."""
    engine = RiskEngine(CFG)
    fresh = engine.evaluate(candidate(), account())
    after_losses = engine.evaluate(candidate(), account(consecutive_losses=2, realized_pnl_today=-100))
    assert fresh.approved and after_losses.approved
    assert after_losses.position_size_shares <= fresh.position_size_shares
