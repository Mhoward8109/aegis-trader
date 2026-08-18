# Aegis Trader — Safety Model

This document exists so nobody — including a future version of this
assistant — can accidentally make Aegis Trader place a real order. Every
control below is enforced in code, with a test in `tests/`, not just
described here.

## 1. The Four Modes

| Mode | Meaning | Can generate hypothetical trades | Can submit orders | Real money |
|---|---|---|---|---|
| RESEARCH | Pure analysis | no | no | no |
| SHADOW | Paper-trade the strategy logic without touching a broker | yes (journaled only) | no | no |
| PAPER | Broker's own paper/sandbox account | yes | yes — paper endpoint only | no |
| LIVE | Real account, real money | yes | yes | **yes** |

Defined in `app/common/modes.py::Mode`. The system **defaults to and starts
in SHADOW** (`config/default.yaml: mode: SHADOW`) and cannot start in LIVE
by omission — `default.yaml` is checked into version control and must never
contain `mode: LIVE`.

## 2. How LIVE Is Structurally Blocked

Promotion to LIVE requires **both**, simultaneously, on every single run:

1. `mode: LIVE` set in `config/local.yaml` — a file that is git-ignored
   (never committed, never shipped) and does not exist until an operator
   creates it by hand.
2. The CLI flag `--i-understand-this-is-live-trading` passed to that
   specific invocation of `python -m app.cli run`.

Both are checked by `ModeGovernor.assert_execution_allowed()`
(`app/common/modes.py`), which is the **only** code path in the entire
system permitted to authorize a broker call. No strategy, scorer, or risk
module can flip the mode — they only ever receive the already-resolved
`Mode` as a read-only value. Missing either control raises
`LiveTradingNotAuthorizedError` and refuses to run — it does not warn and
continue, and it does not fall back to PAPER.

`ShadowBroker` (`app/broker/shadow_adapter.py`) additionally has **no HTTP
or WebSocket client of any kind** — not "an HTTP client that isn't called,"
but no import, no attribute, nothing capable of an outbound network call.
This is verified in `tests/test_shadow_broker.py`. In SHADOW mode, it is
not merely policy but structurally impossible for the process to reach any
broker over the network.

`AlpacaBroker` (`app/broker/alpaca_adapter.py`) is constructed against
`base_url_paper` by default; its live base URL is only used if the caller
passes `allow_live=True` explicitly, and the only caller in the codebase
that can do that is `app/cli.py`'s `run` command — and only after
`ModeGovernor` has already approved it for this exact invocation.

## 3. Never Silently Fall Back From Paper to Live (and vice versa)

`ModeGovernor.assert_execution_allowed(target)` compares the requested
target mode against the configured mode and **raises `InvalidModeError`**
on any mismatch — it never auto-corrects, auto-escalates, or auto-downgrades.
A process configured for PAPER that somehow gets asked to execute as LIVE
(e.g. a bug passing the wrong enum) fails loudly instead of silently doing
the "safer" or the "requested" thing.

## 4. Risk Engine Has Veto Authority, Not Advisory Authority

`RiskEngine.evaluate()` (`app/risk/engine.py`) returns a `RiskDecision`
that the orchestration pipeline (`app/orchestration/pipeline.py`) treats as
binding: if `decision.approved` is `False`, the candidate is journaled as
rejected and the pipeline **does not** call `broker.submit_order()` for it,
under any circumstance, regardless of score. There is no override path from
strategy code, no "confidence high enough to skip risk check," and no
config flag that disables risk evaluation in SHADOW/PAPER/LIVE. Only
RESEARCH mode skips it — and RESEARCH mode never reaches the broker anyway.

Prohibited by construction / explicitly not implemented anywhere in this
codebase: martingale position sizing, doubling down after a loss, or
removing/widening a stop because price is approaching it. `RiskEngine`
takes a static per-trade risk budget from config; nothing in the strategy
or orchestration layer recomputes position size upward after a loss.

## 5. Non-Bypassable Circuit Breaker

`app/risk/circuit_breaker.py` implements daily-loss, consecutive-loss,
stale-data, reconciliation-failure, and repeated-rejected-order trip
conditions, all read from `config/*.yaml`'s `circuit_breaker` block. Per
spec: **"the trading strategy cannot disable this protection."** No
strategy module holds a reference to the circuit breaker's trip state, so
none can clear or bypass it. Only an operator with file access can change
its config, and every parameter change is visible in a diff — there is no
runtime API to weaken it.

## 6. Fail-Closed on Stale Data

`app/marketdata/freshness.py` enforces `config/default.yaml`'s
`data_freshness` block (`quote_max_age_seconds: 5`,
`bar_max_age_seconds: 60`, etc., with `on_stale: FAIL_CLOSED` — the only
permitted value). `assert_fresh()`/`assert_all_fresh()` raise
`StaleDataError` rather than let a decision proceed on stale inputs; there
is no "trade anyway with lower confidence" path.

## 7. Every Number Is Explainable

`OpportunityScorer` (`app/strategy/scoring.py`) returns a full weighted
breakdown per component (catalyst quality, relative volume, liquidity,
technical alignment, etc.) alongside the total score — never a bare number.
`TradeJournal.record_candidate()` persists that full `score_breakdown_json`
for every candidate, approved or rejected, so no opportunity score is ever
opaque or unexplained, satisfying the spec's explicit prohibition on
"an unexplained number."

## 8. Credentials

- Broker/API keys are never hard-coded and never committed. They are read
  from environment variables (see `docs/SETUP.md`) or a git-ignored
  `config/local.yaml` — never from `config/default.yaml`.
- Alpaca keys should be scoped to trading only, with no withdrawal/transfer
  capability, per Alpaca's own API-key permission model — this is an
  operator-side setting when generating the key, not something this code
  can force, so it's called out here as a required manual step.

## 9. Promotion Discipline

`config/default.yaml`'s `promotion_criteria.paper_to_live` block encodes
the minimum evidence bar before even considering a LIVE run (≥100 paper
trades, ≥20 sessions, minimum expectancy/profit-factor thresholds, zero
unresolved execution errors, zero risk-control violations). Meeting these
thresholds does not auto-promote anything — it is a checklist an operator
reviews manually; see `docs/OPERATOR_MANUAL.md` §5 and
`docs/DISASTER_RECOVERY.md`. Per spec §34, **performance is not
permission** — good backtest or paper results are necessary, never
sufficient, for enabling LIVE.

## 10. What Has NOT Been Verified Yet

Being explicit about the boundary of what's actually proven so far, per the
spec's "Built ≠ Tested ≠ Verified ≠ Safe for Live Trading" instruction:

- All of the above is verified against `MockProvider` (synthetic, offline
  data) and `ShadowBroker` (in-process, no network). It has **not** been
  run against real market data or a real broker connection of any kind.
  yet.
- No real-money order has ever been placed, attempted, or is currently
  possible with a default checkout of this repository.
- PAPER-mode integration with Alpaca's actual paper endpoint is Phase 9
  work and has not started.
