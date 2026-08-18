from app.strategy.scoring import OpportunityScorer, ScoreInputs

WEIGHTS = {
    "catalyst_quality": 15, "catalyst_freshness": 10, "relative_volume": 15, "liquidity": 10,
    "spread_quality": 5, "technical_alignment": 15, "market_trend": 10, "reward_risk": 10,
    "data_confidence": 5, "historical_strategy_performance": 5,
}


def test_score_is_bounded_0_100():
    scorer = OpportunityScorer(WEIGHTS)
    best = ScoreInputs(1, 1, 10, 50_000_000, 0.0, 1, 1, 5, 1.0, 1)
    worst = ScoreInputs(0, 0, 0, 0, 1.0, 0, 0, 0, -1.0, 0)
    s_best = scorer.score(best)["score"]
    s_worst = scorer.score(worst)["score"]
    assert 0 <= s_worst <= s_best <= 100


def test_breakdown_always_present_and_sums_correctly():
    scorer = OpportunityScorer(WEIGHTS)
    inputs = ScoreInputs(0.8, 0.9, 3.0, 10_000_000, 0.1, 0.6, 0.5, 2.0, None, 0.9)
    result = scorer.score(inputs)
    assert "breakdown" in result
    assert set(result["breakdown"].keys()) == set(WEIGHTS.keys())
    # total weighted contribution normalized to 0-100 should equal the score
    raw_sum = sum(result["breakdown"].values())
    expected = round(100.0 * raw_sum / sum(WEIGHTS.values()), 1)
    assert abs(result["score"] - expected) < 0.2  # rounding tolerance


def test_illiquid_candidate_gets_zero_liquidity_component():
    scorer = OpportunityScorer(WEIGHTS, min_liquidity_usd=5_000_000)
    inputs = ScoreInputs(0.5, 0.5, 2.0, 1_000_000, 0.1, 0.5, 0.5, 2.0, None, 0.8)
    result = scorer.score(inputs)
    assert result["breakdown"]["liquidity"] == 0.0


def test_unknown_strategy_history_is_neutral_not_punished():
    scorer = OpportunityScorer(WEIGHTS)
    inputs = ScoreInputs(0.5, 0.5, 2.0, 10_000_000, 0.1, 0.5, 0.5, 2.0, None, 0.8)
    result = scorer.score(inputs)
    # neutral (0.5 factor) * weight 5 = 2.5, not 0 and not 5
    assert result["breakdown"]["historical_strategy_performance"] == 2.5
