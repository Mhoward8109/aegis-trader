"""Focused proofs for each configured risk safety property and performance fact."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.analytics.performance import (
    calculate_holding_duration,
    calculate_mfe_mae,
    calculate_r_multiple,
    calculate_slippage,
)
from app.common.db import TradeMode, init_db
from app.journal.store import TradeJournal
from app.risk.engine import AccountState, CandidateRiskInput, RiskEngine


RISK_CFG = {
    "max_risk_per_trade_pct": 1.0,
    "max_risk_per_trade_usd": None,
    "max_daily_loss_pct": 2.0,
    "max_daily_loss_usd": None,
    "max_weekly_loss_pct": 5.0,
    "max_trades_per_day": 10,
    "max_concurrent_positions": 3,
    "max_position_pct_of_account": 20.0,
    "max_concentration_per_symbol_pct": 5.0,
    "max_sector_exposure_pct": 40.0,
    "max_spread_pct": 0.5,
    "min_liquidity_avg_dollar_vol": 5_000_000,
    "max_slippage_pct": 0.3,
    "max_consecutive_losses": 3,
}


def make_engine(**overrides) -> RiskEngine:
    cfg = RISK_CFG | overrides
    return RiskEngine(cfg)


def make_account(**overrides) -> AccountState:
    values = {
        "equity": 100_000.0,
        "buying_power": 100_000.0,
        "open_positions": 0,
        "open_position_symbols": set(),
        "sector_exposure_pct": {},
        "trades_today": 0,
        "realized_pnl_today": 0.0,
        "realized_pnl_week": 0.0,
        "consecutive_losses": 0,
    }
    values.update(overrides)
    return AccountState(**values)


def make_candidate(**overrides) -> CandidateRiskInput:
    values = {
        "ticker": "TEST",
        "sector": "Technology",
        "entry": 100.0,
        "stop": 99.0,
        "spread_pct": 0.1,
        "avg_dollar_volume": 10_000_000.0,
        "estimated_slippage_pct": 0.05,
        "direction": "long",
    }
    values.update(overrides)
    return CandidateRiskInput(**values)


def assert_rejected_for(decision, rule: str) -> None:
    assert not decision.approved
    assert decision.rule_triggered == rule
    assert decision.inputs["trigger"]["rule"] == rule
    assert decision.inputs["trigger"]["limit"] is not None


def test_max_risk_per_trade_pct_sets_risk_budget_before_sizing():
    decision = make_engine().evaluate(make_candidate(), make_account())
    assert decision.approved
    assert decision.risk_budget_usd == 1_000.0
    assert decision.inputs["sizing"]["raw_risk_budget_shares"] == 1_000


def test_max_dollar_risk_per_trade_caps_risk_budget():
    decision = make_engine(max_risk_per_trade_usd=250.0).evaluate(make_candidate(), make_account())
    assert decision.approved
    assert decision.risk_budget_usd == 250.0
    assert decision.inputs["sizing"]["raw_risk_budget_shares"] == 250


def test_max_position_size_pct_clamps_whole_share_size():
    decision = make_engine(max_concentration_per_symbol_pct=80.0).evaluate(
        make_candidate(), make_account()
    )
    assert decision.approved
    assert decision.position_size_shares == 200  # 20% * $100k / $100
    assert decision.inputs["sizing"]["caps_shares"]["max_position_size_shares"] == 200


def test_max_concentration_per_symbol_clamps_whole_share_size():
    decision = make_engine().evaluate(make_candidate(), make_account())
    assert decision.approved
    assert decision.position_size_shares == 50  # 5% * $100k / $100
    assert decision.inputs["sizing"]["caps_shares"]["max_concentration_per_symbol_shares"] == 50


def test_max_concurrent_positions_blocks_new_trade():
    decision = make_engine().evaluate(make_candidate(), make_account(open_positions=3))
    assert_rejected_for(decision, "max_concurrent_positions")


def test_max_daily_loss_blocks_new_trade():
    decision = make_engine().evaluate(make_candidate(), make_account(realized_pnl_today=-2_000.0))
    assert_rejected_for(decision, "max_daily_loss")
    assert decision.inputs["trigger"]["limit"] == -2_000.0


def test_max_weekly_loss_blocks_new_trade():
    decision = make_engine().evaluate(make_candidate(), make_account(realized_pnl_week=-5_000.0))
    assert_rejected_for(decision, "max_weekly_loss")


def test_max_trades_per_day_blocks_new_trade():
    decision = make_engine().evaluate(make_candidate(), make_account(trades_today=10))
    assert_rejected_for(decision, "max_trades_per_day")


def test_max_consecutive_losses_blocks_new_trade_without_resizing():
    decision = make_engine().evaluate(make_candidate(), make_account(consecutive_losses=3))
    assert_rejected_for(decision, "max_consecutive_losses")


def test_min_liquidity_blocks_new_trade():
    decision = make_engine().evaluate(make_candidate(avg_dollar_volume=4_999_999.0), make_account())
    assert_rejected_for(decision, "min_liquidity")


def test_max_spread_blocks_new_trade():
    decision = make_engine().evaluate(make_candidate(spread_pct=0.51), make_account())
    assert_rejected_for(decision, "max_spread_pct")


def test_max_estimated_slippage_blocks_new_trade():
    decision = make_engine().evaluate(make_candidate(estimated_slippage_pct=0.31), make_account())
    assert_rejected_for(decision, "max_slippage_pct")


def test_buying_power_limit_clamps_whole_share_size():
    decision = make_engine(max_concentration_per_symbol_pct=80.0).evaluate(
        make_candidate(), make_account(buying_power=550.0)
    )
    assert decision.approved
    assert decision.position_size_shares == 5
    assert decision.inputs["sizing"]["caps_shares"]["buying_power_shares"] == 5


def test_per_sector_exposure_limit_clamps_projected_exposure():
    decision = make_engine(max_concentration_per_symbol_pct=80.0).evaluate(
        make_candidate(), make_account(sector_exposure_pct={"Technology": 39.0})
    )
    assert decision.approved
    assert decision.position_size_shares == 10
    assert decision.inputs["sizing"]["projected_sector_exposure_pct"] == pytest.approx(40.0)


def test_missing_trades_today_fails_closed():
    decision = make_engine().evaluate(make_candidate(), make_account(trades_today=None))
    assert_rejected_for(decision, "missing_trades_today")
    assert "not supplied" in decision.reason


def test_zero_stop_distance_rejected():
    decision = make_engine().evaluate(make_candidate(stop=100.0), make_account())
    assert_rejected_for(decision, "zero_stop_distance")
    assert decision.inputs["trigger"]["calculation"]["signed_stop_distance"] == 0.0


def test_stop_on_wrong_side_for_direction_rejected_with_negative_distance():
    decision = make_engine().evaluate(make_candidate(stop=101.0), make_account())
    assert_rejected_for(decision, "stop_wrong_side")
    assert decision.inputs["trigger"]["calculation"]["signed_stop_distance"] == -1.0


def test_sizing_is_invariant_to_consecutive_losses_and_has_no_martingale_path():
    engine = make_engine()
    fresh = engine.evaluate(make_candidate(), make_account(consecutive_losses=0))
    after_losses = engine.evaluate(make_candidate(), make_account(consecutive_losses=2))
    assert fresh.approved and after_losses.approved
    assert fresh.position_size_shares == after_losses.position_size_shares
    assert fresh.risk_budget_usd == after_losses.risk_budget_usd

    sizing_source = inspect.getsource(RiskEngine.compute_position_size)
    assert "consecutive_losses" not in sizing_source
    public_methods = {
        name for name, value in vars(RiskEngine).items() if callable(value) and not name.startswith("_")
    }
    assert not {
        name for name in public_methods if any(word in name.lower() for word in ("martingale", "average", "double", "increase"))
    }


def test_r_multiple_is_deterministic():
    values = {
        "entry_price": 100.0,
        "stop_price": 98.0,
        "exit_price": 104.0,
        "direction": "long",
        "quantity": 10,
    }
    assert calculate_r_multiple(**values) == 2.0
    assert calculate_r_multiple(**values) == 2.0


def test_slippage_is_deterministic_from_recorded_prices():
    assert calculate_slippage(100.0, 100.25) == (0.25, 0.25)


def test_mfe_mae_are_deterministic_from_recorded_extremes():
    assert calculate_mfe_mae(
        entry_price=100.0, highs=[101.0, 103.5, 102.0], lows=[99.5, 98.0], direction="long"
    ) == (3.5, 2.0)


def test_holding_duration_is_deterministic_from_recorded_timestamps():
    entry = datetime(2026, 8, 18, 14, 30, tzinfo=timezone.utc)
    assert calculate_holding_duration(entry, entry + timedelta(minutes=17, seconds=5)) == 1025.0


def test_paper_trade_performance_records_complete_deterministic_facts(tmp_path):
    engine = init_db(str(tmp_path / "journal.db"))
    journal = TradeJournal(Session(engine))
    order = journal.open_order(
        candidate_id=None,
        mode=TradeMode.PAPER,
        ticker="TEST",
        side="BUY",
        order_type="market",
        qty=10,
        intended_entry=100.0,
        stop=98.0,
        targets=[104.0],
        strategy="breakout",
    )
    entry_at = datetime(2026, 8, 18, 14, 30, tzinfo=timezone.utc)
    exit_at = entry_at + timedelta(minutes=10)
    record = journal.record_paper_trade_performance(
        order,
        strategy_version="1.2.0",
        actual_fill_price=100.25,
        exit_price=104.25,
        realized_pnl=40.0,
        highs=[101.0, 105.0],
        lows=[99.0, 98.5],
        catalyst_summary="Verified earnings release",
        market_regime={"trend": "risk_on"},
        rejection_reason=None,
        exit_reason="target",
        decision_data_timestamp=entry_at - timedelta(seconds=30),
        fill_data_timestamp=entry_at,
        exit_data_timestamp=exit_at,
        performance_data_timestamp=exit_at,
    )
    assert record.strategy_name == "breakout"
    assert record.strategy_version == "1.2.0"
    assert record.slippage_absolute == pytest.approx(0.25)
    assert record.slippage_pct == pytest.approx(0.25)
    # Actual fill is the recorded entry for R: $40 / ((100.25 - 98) * 10).
    assert record.r_multiple == pytest.approx(40.0 / 22.5)
    assert (record.mfe, record.mae) == pytest.approx((4.75, 1.75))
    assert record.holding_duration_seconds == 600.0
    assert record.time_of_day == entry_at.timetz().isoformat()
