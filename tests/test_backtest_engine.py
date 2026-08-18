import pandas as pd
import pytest

from app.backtest.engine import SimulatedTrade, compute_metrics, is_likely_overfit, simulate_fill, train_validation_oos_split


def make_trade(pnl_r: float, direction="long"):
    entry, stop = 100.0, 99.0
    risk = entry - stop
    exit_price = entry + pnl_r * risk if direction == "long" else entry - pnl_r * risk
    return SimulatedTrade(
        ticker="TEST", strategy="test", entry_time=pd.Timestamp("2026-08-18 09:35"),
        exit_time=pd.Timestamp("2026-08-18 09:50"), entry_price=entry, exit_price=exit_price,
        stop=stop, qty=100, direction=direction, exit_reason="target",
    )


def test_r_multiple_matches_expected():
    t = make_trade(2.0)
    assert t.r_multiple == pytest.approx(2.0)


def test_simulate_fill_buy_costs_more_than_intended():
    filled = simulate_fill(100.0, "BUY", spread_pct=0.5, commission_per_share=0.01)
    assert filled > 100.0


def test_simulate_fill_sell_yields_less_than_intended():
    filled = simulate_fill(100.0, "SELL", spread_pct=0.5, commission_per_share=0.01)
    assert filled < 100.0


def test_zero_cost_mode_disables_cost_model():
    filled = simulate_fill(100.0, "BUY", spread_pct=5.0, zero_cost=True)
    assert filled == 100.0


def test_metrics_empty_sample():
    m = compute_metrics([])
    assert m["trades"] == 0


def test_metrics_basic_win_rate_and_expectancy():
    trades = [make_trade(2.0), make_trade(2.0), make_trade(-1.0)]
    m = compute_metrics(trades)
    assert m["trades"] == 3
    assert m["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["expectancy_r"] == pytest.approx((2 + 2 - 1) / 3)


def test_sharpe_none_below_min_sample():
    trades = [make_trade(1.0)] * 5
    m = compute_metrics(trades)
    assert m["sharpe_ratio"] is None
    assert "Sample too small" in m["sharpe_sortino_note"]


def test_chronological_split_no_shuffling():
    dates = list(range(100))
    split = train_validation_oos_split(dates, train_pct=0.6, val_pct=0.2)
    assert split["train"][1] < split["validation"][0]
    assert split["validation"][1] < split["out_of_sample"][0]


def test_split_requires_minimum_sessions():
    with pytest.raises(ValueError):
        train_validation_oos_split(list(range(5)))


def test_overfit_detection_sign_flip():
    assert is_likely_overfit(in_sample_expectancy=0.3, oos_expectancy=-0.1) is True


def test_overfit_detection_large_divergence():
    assert is_likely_overfit(in_sample_expectancy=0.8, oos_expectancy=0.1) is True


def test_not_overfit_when_consistent():
    assert is_likely_overfit(in_sample_expectancy=0.3, oos_expectancy=0.25) is False


def test_unknown_treated_as_overfit_conservatively():
    assert is_likely_overfit(None, 0.3) is True
