"""
Opportunity Scoring (spec §8). Produces a transparent 0-100 score with a
fully itemized breakdown — "Do NOT allow the AI to produce an unexplained
number" is enforced structurally: `score()` returns both the total AND the
per-component contributions, and nothing downstream is allowed to discard
the breakdown (the DB column score_breakdown_json is NOT NULL, see
app/common/db.py Candidate model).

Every component is a deterministic function of inputs already computed
elsewhere (catalyst engine, technical engine, risk engine, strategy
backtests) — no component here re-derives facts via an LLM call, keeping
arithmetic out of the hands of a language model (spec §23).
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class ScoreInputs:
    catalyst_quality: float          # 0-1, from CatalystEngine
    catalyst_freshness: float        # 0-1 (1.0 = brand new, 0 = stale/recirculated)
    relative_volume: float           # raw RVOL, e.g. 3.2
    liquidity_usd: float             # avg $ volume
    spread_pct: float
    technical_alignment: float       # 0-1, fraction of configured indicators agreeing with direction
    market_trend_alignment: float    # 0-1, does regime support this direction
    reward_risk: float               # e.g. 2.5
    historical_strategy_expectancy_r: float | None  # None if strategy has < min sample size
    data_confidence: float           # 0-1, fraction of required fields that were available & fresh


class OpportunityScorer:
    def __init__(self, weights: dict, min_liquidity_usd: float = 5_000_000, max_spread_pct: float = 0.5):
        self.weights = weights
        self.min_liquidity_usd = min_liquidity_usd
        self.max_spread_pct = max_spread_pct

    def _liquidity_score(self, liquidity_usd: float) -> float:
        if liquidity_usd < self.min_liquidity_usd:
            return 0.0
        # saturates at 5x the floor
        return min(1.0, (liquidity_usd - self.min_liquidity_usd) / (4 * self.min_liquidity_usd))

    def _spread_score(self, spread_pct: float) -> float:
        if spread_pct >= self.max_spread_pct:
            return 0.0
        return 1.0 - (spread_pct / self.max_spread_pct)

    def _rvol_score(self, rvol: float) -> float:
        return min(1.0, rvol / 5.0)  # 5x RVOL = full marks

    def _rr_score(self, rr: float) -> float:
        return min(1.0, rr / 3.0)  # 3:1 R:R = full marks

    def _history_score(self, expectancy_r: float | None) -> float:
        if expectancy_r is None:
            return 0.5  # unknown/insufficient sample -> neutral, not punished, not rewarded
        return max(0.0, min(1.0, 0.5 + expectancy_r))  # expectancy_r=0 -> 0.5, +0.5R -> 1.0, -0.5R -> 0.0

    def score(self, i: ScoreInputs) -> dict:
        components = {
            "catalyst_quality": i.catalyst_quality * self.weights.get("catalyst_quality", 0),
            "catalyst_freshness": i.catalyst_freshness * self.weights.get("catalyst_freshness", 0),
            "relative_volume": self._rvol_score(i.relative_volume) * self.weights.get("relative_volume", 0),
            "liquidity": self._liquidity_score(i.liquidity_usd) * self.weights.get("liquidity", 0),
            "spread_quality": self._spread_score(i.spread_pct) * self.weights.get("spread_quality", 0),
            "technical_alignment": i.technical_alignment * self.weights.get("technical_alignment", 0),
            "market_trend": i.market_trend_alignment * self.weights.get("market_trend", 0),
            "reward_risk": self._rr_score(i.reward_risk) * self.weights.get("reward_risk", 0),
            "historical_strategy_performance": self._history_score(i.historical_strategy_expectancy_r)
            * self.weights.get("historical_strategy_performance", 0),
            "data_confidence": i.data_confidence * self.weights.get("data_confidence", 0),
        }
        max_possible = sum(self.weights.values()) or 1.0
        total = sum(components.values())
        normalized = round(100.0 * total / max_possible, 1)
        return {
            "score": normalized,
            "breakdown": {k: round(v, 2) for k, v in components.items()},
            "max_possible_weight": max_possible,
        }
