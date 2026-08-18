"""
SAFETY INVARIANT: stale market data must not silently become recorded evidence.

SHADOW places nothing at a broker, so stale data in SHADOW risks no money. It
does, however, risk something the milestone explicitly cares about: SHADOW
output is the evidence base used to decide whether a strategy deserves promotion
to PAPER (PART 21). A hypothetical fill priced off a stale quote is
indistinguishable in the journal from one priced off a good quote unless the
pipeline refuses it, so poisoned evidence would survive into a promotion
decision.

REGRESSION HISTORY: the freshness gate was originally written as

    if not freshness_report.all_required_fresh and mode.allows_order_submission:

`Mode.SHADOW.allows_order_submission` is False, so the entire gate was skipped in
SHADOW and stale-data trades were journaled without comment. The first attempted
fix then referenced `scored["score"]` in a branch that runs *before* scoring
exists -- a NameError that the whole 166-test suite did not catch, because no
test drove a stale quote through SHADOW. This file is that test.
"""
import datetime as dt

import pandas as pd

from app.broker.shadow_adapter import ShadowBroker
from app.catalyst.engine import CatalystEngine, NullNewsProvider
from app.common.db import Candidate, init_db
from app.common.modes import Mode
from app.execution.authorization import ExecutionAuthorizer
from app.journal.store import TradeJournal
from app.orchestration.pipeline import run_pipeline
from app.risk.engine import RiskEngine
from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker
from app.scanner.base import ScanCriteria
from app.scanner.mock_provider import MockProvider
from app.strategy.opening_range_breakout import OpeningRangeBreakout
from app.strategy.scoring import OpportunityScorer
from app.strategy.vwap_reclaim import VwapReclaim
from sqlalchemy.orm import Session

from tests.test_pipeline_e2e import RISK_CFG, WEIGHTS, quote_source_factory


class StaleBarsProvider(MockProvider):
    """MockProvider whose bars carry timestamps from two days ago.

    Only the timestamps are altered. Prices remain internally coherent, so this
    isolates staleness from the quote/bar coherence gate -- otherwise a failure
    here would be ambiguous between the two gates.
    """

    STALE_BY = dt.timedelta(days=2)

    def get_bars(self, ticker, *args, **kwargs):
        bars = super().get_bars(ticker, *args, **kwargs)
        if "timestamp" in bars.columns:
            bars = bars.copy()
            bars["timestamp"] = pd.to_datetime(bars["timestamp"]) - self.STALE_BY
        return bars


def _run(tmp_path, provider, mode):
    engine = init_db(str(tmp_path / "journal.db"))
    journal = TradeJournal(Session(engine))
    broker = ShadowBroker(starting_equity=100_000,
                          quote_source=quote_source_factory(provider))
    result = run_pipeline(
        mode=mode,
        provider=provider,
        criteria=ScanCriteria(),
        strategies=[OpeningRangeBreakout({}), VwapReclaim({})],
        scorer=OpportunityScorer(WEIGHTS),
        catalyst_engine=CatalystEngine([NullNewsProvider()]),
        risk_engine=RiskEngine(RISK_CFG),
        risk_cfg=RISK_CFG,
        broker=broker,
        journal=journal,
        authorizer=ExecutionAuthorizer(target_mode=mode),
        circuit_breaker=PersistentCircuitBreaker(tmp_path / "breaker.db"),
        config_mode=mode,
        config_mode_source="default.yaml",
        min_score_to_consider=0.0,
    )
    return result, journal, Session(engine)


def test_shadow_refuses_stale_data_instead_of_journaling_a_hypothetical_trade(tmp_path):
    """Stale bars in SHADOW produce a freshness rejection, never a recorded trade."""
    provider = StaleBarsProvider(seed=7, drift_per_bar=0.0004, bars=90)
    result, _journal, _session = _run(tmp_path, provider, Mode.SHADOW)

    assert result.orders_submitted == 0, (
        "SHADOW submitted a hypothetical trade built from two-day-old bars; "
        "that trade would have entered the promotion evidence base"
    )
    stale = [o for o in result.outcomes if o.gate == "freshness"]
    assert stale, (
        f"no candidate was rejected by the freshness gate; stages seen: "
        f"{sorted({o.stage_reached for o in result.outcomes})}"
    )
    for outcome in stale:
        assert outcome.stage_reached == "stale_data"
        assert outcome.rejection_reason == "stale_required_data"
        # The branch must not invent a score it never computed.
        assert outcome.score is None


def test_shadow_stale_data_is_journaled_as_a_data_fault_not_silently_dropped(tmp_path):
    """A stale-data refusal is written to the journal, because rejections are evidence."""
    provider = StaleBarsProvider(seed=7, drift_per_bar=0.0004, bars=90)
    _result, _journal, session = _run(tmp_path, provider, Mode.SHADOW)

    rows = session.query(Candidate).all()
    faults = [r for r in rows if r.setup_json and "data_fault" in r.setup_json]
    assert faults, (
        "the stale-data refusal left no journal record. A silent skip is "
        "indistinguishable from 'no candidate found' when reviewing a run later."
    )
    assert all(r.decision == "REJECTED" for r in faults)
    # Deliberately not attributed to a strategy: a feed fault must not be
    # charged against a strategy's measured hit rate.
    assert all(r.strategy == "n/a" for r in faults)


def test_fresh_data_still_reaches_submission_in_shadow(tmp_path):
    """Control: the same setup with fresh bars still trades, so the gate is not a blanket block."""
    provider = MockProvider(seed=7, drift_per_bar=0.0004, bars=90)
    result, _journal, _session = _run(tmp_path, provider, Mode.SHADOW)

    assert not [o for o in result.outcomes if o.gate == "freshness"]
    assert result.orders_submitted > 0, (
        "the control case submitted nothing, so the stale-data test above proves "
        "nothing -- it could be passing for an unrelated reason"
    )
