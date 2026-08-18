# Adversarial test suite report

## Result summary

| Safety Invariant | Test(s) | Result |
|---|---|---|
| Authorization boundary: LIVE provenance, explicit operator approval, direct pipeline calls, grant-only execution, environment binding, disabled live adapter, and private-seal containment | `tests/test_invariant_authorization.py` | PASS |
| Authorization evidence defaults fail closed | `test_each_unset_authorization_evidence_field_fails_closed` | PASS |
| Market data: stale/missing/untyped timestamps, malformed bars, journaled refusal, and quote/bar coherence | `tests/test_invariant_market_data.py::test_unusable_market_data_refuses_submission_and_is_journaled`, `::test_quote_bar_price_scale_disagreement_refuses_and_journals` | PASS |
| Market data: provider exceptions/timeouts are refused and journaled rather than escaping | `test_provider_failure_or_timeout_fails_closed_and_is_journaled` | XFAIL-not-enforced (2 parameter cases) |
| Broker semantics: rejection, partial fill, unknown status, idempotent duplicate events, submission receipt is not a fill, timeout has no retry, disconnect/reconciliation block entries | `tests/test_invariant_broker.py` | PASS |
| Risk limits: daily/weekly loss, trade and position limits, loss streak, spread, liquidity, buying power, stop plausibility, sizing formula, no martingale | `tests/test_invariant_risk.py` | PASS |
| Recovery: partial-fill and held-position restart preserve exposure and reject duplicate entry | `tests/test_invariant_recovery.py::test_crash_after_partial_fill_preserves_exposure_and_blocks_duplicate_entry`, `::test_restart_while_holding_position_keeps_duplicate_entry_blocked` | PASS |
| Recovery: crash before submission, crash after submission, or restart with unresolved open order blocks entries | `tests/test_invariant_recovery.py::test_crash_before_submission_blocks_new_entries_after_restart`, `::test_crash_after_submission_blocks_new_entries_after_restart`, `::test_restart_with_open_order_blocks_new_entries` | XFAIL-not-enforced (3 cases) |
| Persistent breaker: restart persistence, entries blocked/protective exits allowed, ordinary code cannot reset, reset needs explicit operator authorization | `tests/test_invariant_circuit_breaker.py` | PASS |

## Genuine defects discovered

1. **Provider bar failures escape before the fail-closed freshness/refusal path and are not journaled.** `app/orchestration/pipeline.py:448` calls `provider.get_bars(...)` without a failure boundary. A timeout or provider exception therefore reaches the broad candidate exception handler rather than producing the required market-data refusal record. The two strict XFAIL cases in `tests/test_invariant_market_data.py` demonstrate this.

2. **A local order that exists before a broker ID is durably recorded is ignored by reconciliation.** `app/execution/lifecycle.py:393-396` intentionally omits local open orders without `broker_order_id`; no discrepancy is created for a `RISK_APPROVED` order after a crash before submission. The strict XFAIL in `tests/test_invariant_recovery.py::test_crash_before_submission_blocks_new_entries_after_restart` demonstrates that a restart can proceed rather than halt for resolution.

3. **Missing broker orders are classified but do not block entry.** `app/execution/lifecycle.py:133-140` omits `MISSING_BROKER_ORDER` from `BLOCKING_DISCREPANCIES`, while `app/execution/lifecycle.py:412-420` creates that discrepancy for a locally open order absent at the broker. This permits a restart to continue following a crash after submission or while an order is acknowledged but unresolved. The two strict XFAIL recovery tests demonstrate this.

No production code was changed by this task; the defects are preserved as strict XFAIL tests rather than hidden by weaker assertions.

## Final full-suite output

`166 passed, 5 xfailed, 1 warning in 20.65s`

## Not tested

- No real Alpaca LIVE connection or order submission was attempted: `AlpacaLiveBroker` is intentionally operationally disabled and its construction refusal is tested instead.
- Process termination was not used for recovery tests by design. The requested recovery simulation is performed using fresh journal and breaker objects against the same SQLite paths; it validates durable-state behavior deterministically without creating an uncontrolled process-level failure.
