from app.risk.engine import AccountState, CandidateRiskInput, RiskEngine

BASE_CFG = {
    "max_risk_per_trade_pct": 0.5,
    "max_risk_per_trade_usd": None,
    "max_daily_loss_pct": 2.0,
    "max_daily_loss_usd": None,
    "max_weekly_loss_pct": 5.0,
    "max_trades_per_day": 10,
    "max_concurrent_positions": 3,
    "max_position_pct_of_account": 20.0,
    "max_sector_exposure_pct": 40.0,
    "max_spread_pct": 0.5,
    "min_liquidity_avg_dollar_vol": 5_000_000,
    "max_slippage_pct": 0.3,
    "max_consecutive_losses": 3,
}


def make_account(**overrides):
    base = dict(equity=100_000, buying_power=100_000, open_positions=0, open_position_symbols=set(),
                sector_exposure_pct={}, trades_today=0, realized_pnl_today=0.0, realized_pnl_week=0.0,
                consecutive_losses=0)
    base.update(overrides)
    return AccountState(**base)


def make_candidate(**overrides):
    base = dict(ticker="TEST", sector="Tech", entry=10.0, stop=9.5, spread_pct=0.1,
                avg_dollar_volume=10_000_000, estimated_slippage_pct=0.05)
    base.update(overrides)
    return CandidateRiskInput(**base)


def test_position_sizing_formula_matches_spec():
    engine = RiskEngine(BASE_CFG)
    shares, risk_budget = engine.compute_position_size(equity=100_000, entry=10.0, stop=9.5)
    assert risk_budget == 500.0          # 100000 * 0.5%
    assert shares == 1000.0              # 500 / 0.5


def test_approved_trade_within_all_limits():
    engine = RiskEngine(BASE_CFG)
    decision = engine.evaluate(make_candidate(), make_account())
    assert decision.approved
    assert decision.position_size_shares == 1000.0


def test_rejects_when_consecutive_losses_hit_no_martingale():
    engine = RiskEngine(BASE_CFG)
    decision = engine.evaluate(make_candidate(), make_account(consecutive_losses=3))
    assert not decision.approved
    assert decision.rule_triggered == "max_consecutive_losses"


def test_rejects_daily_loss_breach():
    engine = RiskEngine(BASE_CFG)
    decision = engine.evaluate(make_candidate(), make_account(realized_pnl_today=-2100.0))
    assert not decision.approved
    assert decision.rule_triggered == "max_daily_loss"


def test_rejects_too_many_concurrent_positions():
    engine = RiskEngine(BASE_CFG)
    decision = engine.evaluate(make_candidate(), make_account(open_positions=3))
    assert not decision.approved
    assert decision.rule_triggered == "max_concurrent_positions"


def test_rejects_duplicate_ticker_position():
    engine = RiskEngine(BASE_CFG)
    decision = engine.evaluate(make_candidate(ticker="AAPL"),
                                 make_account(open_position_symbols={"AAPL"}))
    assert not decision.approved
    assert decision.rule_triggered == "duplicate_position"


def test_rejects_wide_spread():
    engine = RiskEngine(BASE_CFG)
    decision = engine.evaluate(make_candidate(spread_pct=1.0), make_account())
    assert not decision.approved
    assert decision.rule_triggered == "max_spread_pct"


def test_rejects_illiquid_candidate():
    engine = RiskEngine(BASE_CFG)
    decision = engine.evaluate(make_candidate(avg_dollar_volume=100_000), make_account())
    assert not decision.approved
    assert decision.rule_triggered == "min_liquidity"


def test_position_size_capped_by_buying_power():
    engine = RiskEngine(BASE_CFG)
    # huge risk budget relative to tiny buying power
    decision = engine.evaluate(make_candidate(entry=10.0, stop=9.99),
                                 make_account(equity=1_000_000, buying_power=1000.0))
    assert decision.approved
    assert decision.position_size_shares * 10.0 <= 1000.0 + 1e-6


def test_never_increases_size_after_losses_same_inputs_same_size():
    """Sizing is purely a function of equity/entry/stop, never of P&L
    history — this is the explicit no-martingale guarantee from spec §9."""
    engine = RiskEngine(BASE_CFG)
    acct_fresh = make_account(realized_pnl_today=0.0, consecutive_losses=0)
    acct_after_losses = make_account(realized_pnl_today=-100.0, consecutive_losses=2)
    d1 = engine.evaluate(make_candidate(), acct_fresh)
    d2 = engine.evaluate(make_candidate(), acct_after_losses)
    assert d1.approved and d2.approved
    assert d1.position_size_shares == d2.position_size_shares
