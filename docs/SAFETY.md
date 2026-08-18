# Aegis Trader — Safety Model

This document records implemented controls and their limits. It does not grant
permission to trade. **PAPER verification against a real Alpaca account has not
yet occurred; nothing in this system has been run against Alpaca.**

## 1. The Four Modes

| Mode | Meaning | Can generate hypothetical trades | Can submit orders now | Real money |
|---|---|---|---|---|
| RESEARCH | Pure analysis | no | no | no |
| SHADOW | Offline hypothetical-trade path | yes (journaled only) | no | no |
| PAPER | Alpaca paper endpoint | yes | yes, through `AlpacaPaperBroker` | no |
| LIVE | Named architecture mode | no operational run | no — refused | no |

`Mode` is defined in `app/common/modes.py`. The default configuration starts in
SHADOW and version-controlled defaults must not select LIVE.

## 2. How LIVE Is Structurally Blocked

`ModeGovernor.assert_execution_allowed()` still rejects mode mismatches and
validates the configured LIVE choice and per-run acknowledgement. Those checks
are necessary mode evidence, but they do not enable execution.

LIVE has two independent operational stops:

1. `python -m app.cli run --mode live` refuses before constructing any broker.
2. `AlpacaLiveBroker.__init__` refuses because
   `AlpacaLiveBroker.OPERATIONALLY_ENABLED` is `False`.

A caller would therefore have to defeat both stops before it could even begin a
live adapter path. Neither strategy code nor a single strategy-owned boolean
can authorize LIVE. The order path requires an `ExecutionGrant` from
`ExecutionAuthorizer`, and the broker independently validates the grant against
its declared environment.

`ShadowBroker` has no HTTP or WebSocket client. In SHADOW mode, it is
structurally unable to reach a broker over the network.

## 3. PAPER Is a Separate, Fail-Closed Path

PAPER uses `AlpacaPaperBroker` with `BrokerEnvironment.PAPER` against
`https://paper-api.alpaca.markets`. Its Alpaca `TradingClient` is constructed
with literal `paper=True`; the class accepts no parameter that can change this
and exposes no URL override.

`app.cli::_run_paper_cycle` constructs each required PAPER dependency before it
runs the pipeline. If credentials, the paper broker, market data, broker clock,
regime engine, or catalyst provider cannot be constructed, the CLI exits. It
never falls back to `MockProvider` or `ShadowBroker`. A silent synthetic
fallback would report simulated results under a PAPER heading that claims real
broker and market inputs.

Alpaca supplies no server-side signal that independently proves a session is
paper or live. The adapter verifies its own declared paper URL and journals the
limitation; that is a control against client misconfiguration, not proof of a
credential's server-side classification.

## 4. Execution Authorization Is a Required Boundary

`ExecutionAuthorizer` is the required choke point between a risk-approved
candidate and order submission. It evaluates `AuthorizationEvidence` and issues
a single-use `ExecutionGrant` only when every required check passes. The grant
is bound to the order intent and broker environment; adapters re-check it before
the network submission.

`AuthorizationEvidence` fields default to a private `_MISSING` sentinel rather
than permissive booleans. Missing evidence produces a failed check. The mapping
`MODE_REQUIRES_BROKER_ENVIRONMENT` requires a PAPER intent to use PAPER and a
LIVE intent to use LIVE; RESEARCH has no permitted broker environment. Thus a
strategy cannot reach LIVE merely by passing a boolean or naming a mode.

`run_pipeline()` requires an authorizer and a circuit breaker with no defaults.
Omitting either is a call-site error, not an unprotected run.

## 5. Persistent Circuit Breaker and Reconciliation

`PersistentCircuitBreaker` keeps trip state in SQLite, separate from the trade
journal. State is read from disk and survives process restart; trips and resets
remain in the audit history. A tripped breaker blocks new entries while leaving
protective exits, position closes, and order cancellation available.

Resetting requires an explicit, audited operator command:

```bash
python -m app.cli breaker reset --operator NAME --reason "..."
```

The command gets a reset token from module-level `issue_operator_reset()`, not
from a breaker method. Merely holding a `PersistentCircuitBreaker` instance
does not confer reset power; strategy code cannot reset it. Clearing a current
or future session also requires the deliberate `--same-session` option.

Before order-submitting runs, `OrderLifecycleManager.reconcile()` compares local
state to broker state. `BLOCKING_DISCREPANCIES` covers discrepancies that make
exposure unknown, including unexpected or missing positions, quantity and order
state mismatches, unexpected/missing broker orders, unsubmitted local orders,
and an unreachable broker. A blocking report trips the breaker and prevents new
trading until an operator resolves it. An unexpected position is not
automatically closed.

## 6. Freshness Is a Hard Execution Gate

`FreshnessGate.require()` registers data whose staleness blocks trading;
`FreshnessGate.observe()` records non-blocking data. Missing, malformed, stale,
or implausibly future timestamps fail the required-data verdict. The resulting
report is passed to `ExecutionAuthorizer` as evidence, so stale required data
fails closed and no order is placed.

In order-submitting modes, a provider failure or stale required data trips the
persistent breaker. In SHADOW, stale required data rejects the hypothetical
candidate and journals a data fault; it does not latch the persistent breaker
because SHADOW has no broker exposure.

## 7. Risk Engine Has Veto Authority

`RiskEngine.evaluate()` returns a binding decision. If it rejects a candidate,
the pipeline journals the refusal and does not submit an order. There is no
strategy override path that skips risk evaluation in SHADOW or PAPER.

## 8. Credentials

Broker credentials are never hard-coded or committed. The documented PAPER
credential names are `ALPACA_PAPER_API_KEY_ID` and
`ALPACA_PAPER_API_SECRET_KEY`; SEC EDGAR uses
`SEC_EDGAR_CONTACT_EMAIL`. Keep values outside configuration files and use
trading-only broker keys without withdrawal/transfer capability.

## 9. Promotion Discipline

Performance is not permission. Passing the paper-to-live criteria can inform
future review, but it cannot promote the system or enable LIVE in this
milestone. The CLI and `AlpacaLiveBroker` both continue to refuse live
execution.

## 10. What Has NOT Been Verified Yet

- **Nothing has been run against Alpaca.** PAPER execution has not been
  verified against a real Alpaca account, real Alpaca market data, a broker
  clock, or a real paper fill.
- Tests exercise mocks and fixtures only. They can establish implemented
  behavior under those controlled conditions; they do not establish an external
  broker integration. **Built and tested do not mean verified.**
- No real-money order has been placed or attempted by this milestone, and LIVE
  execution is operationally disabled by the two independent stops described
  above.
- The system cannot independently prove a PAPER credential's classification
  because Alpaca does not expose the required server-side signal.
