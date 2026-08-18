# Aegis Trader — Disaster Recovery

## Guiding Principle

The journal database (SQLite, `app/common/db.py`) and the broker's own
confirmed order/position state are the two sources of truth. Nothing in
this system should ever need to trust in-memory state that isn't also
durably recorded in one of those two places — see
`app/execution/order_state_machine.py`, which is explicitly designed to
resync from broker-confirmed state rather than assume a fill.

## Scenario: Process Crashes Mid-Run

- **SHADOW/RESEARCH**: no broker was ever contacted, so there is nothing
  to reconcile. Restart `python -m app.cli run --mode <mode>`; the journal
  DB already has every candidate/outcome recorded up to the crash point.
- **PAPER/LIVE (once wired, Phase 9+)**: on restart, before evaluating any
  new candidate, the orchestrator must call `broker.get_orders()` /
  `broker.get_positions()` and reconcile against the journal's last known
  `OrderState` for any order left in a non-terminal state
  (`SUBMITTED`/`PARTIALLY_FILLED`). This reconciliation step is a
  documented requirement for Phase 10 (order state machine wiring into
  the live pipeline) — **not yet implemented**, because PAPER/LIVE order
  submission itself isn't wired yet. Do not enable PAPER mode without
  this reconciliation step in place.

## Scenario: Circuit Breaker Trips

By design (`app/risk/circuit_breaker.py`), a tripped breaker blocks new
`RiskEngine` approvals for the rest of the session — it does not touch
existing open positions or attempt to auto-flatten them (auto-flattening
on every trip would itself be a risky, opinionated action the spec doesn't
ask for). Operator action: review `circuit_breaker_events` in the journal
DB, understand why it tripped (daily loss / consecutive losses / stale
data / reconciliation failure / repeated rejects / excessive slippage),
manually decide whether to close open positions through the broker's own
UI/API, and only resume the process for a new session after root-causing
the trip. There is no code path to clear a trip from within a strategy or
CLI flag — see `docs/SAFETY.md` §5.

## Scenario: Stale Data Detected Mid-Cycle

`app/marketdata/freshness.py::assert_fresh()` raises `StaleDataError`
immediately (fail-closed, spec §11) rather than letting the pipeline use
the stale value. The current `run_pipeline()` does not yet call
`assert_all_fresh()` explicitly before scoring (`freshness.py` exists as a
utility module used by tests today; wiring it into every pipeline read is
a near-term follow-up before this is production-grade for PAPER). Until
wired: **do not run PAPER/LIVE modes without first adding an explicit
freshness check on every quote/bar/account read in the pipeline.**

## Scenario: Journal Database Corruption or Loss

- The journal DB (`data/journal.db` by default) is the only durable local
  state. Back it up regularly once PAPER/LIVE trading begins (a simple
  `cp data/journal.db backups/journal-$(date +%F).db` cron entry is
  sufficient for a single-operator system — no HA/replication is built or
  required at this scale).
- If lost entirely while PAPER/LIVE positions are open: the broker
  (Alpaca) remains the source of truth for actual positions/orders/P&L —
  `broker.get_positions()` / `broker.get_account()` can always rebuild a
  correct picture of what's real, even with zero local history. Only the
  *research value* (why past trades were taken) is lost, not the ability
  to know current true exposure.

## Scenario: You Need to Stop Everything Immediately

1. Kill the running process (no persistent scheduler exists yet in this
   milestone — see `docs/OPERATOR_MANUAL.md` "Emergency Stop").
2. If PAPER/LIVE orders are outstanding, use the broker's own
   dashboard/API directly to cancel open orders or flatten positions —
   don't wait on this codebase for that in an emergency.
3. Set `mode: SHADOW` (or delete `config/local.yaml` entirely) before
   restarting anything, so a restart cannot accidentally resume LIVE.

## Scenario: Alpaca Deprecates/Changes an API Field (as already happened with PDT fields)

Precedent: Alpaca froze then removed `pattern_day_trader`/`daytrade_count`
in 2026 following FINRA's PDT rule retirement (see
`docs/ARCHITECTURE.md` §5). `app/broker/alpaca_adapter.py` already reads
newer/optional fields defensively via `getattr(...)`, so a future
similar field removal degrades gracefully (missing optional field) rather
than crashing — but any *new hard-required* field change from Alpaca will
still require a code update. Check Alpaca's changelog
([alpaca.markets/blog](https://alpaca.markets/blog/)) periodically once
PAPER/LIVE is in use.
