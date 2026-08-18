# Milestone 2 Audit Report

**Audit date:** 2026-08-18
**Auditor:** fresh code inspection (not documentation review)
**Method:** traced `app/cli.py` → `app/config/loader.py` → `app/common/modes.py` →
scanner → catalyst → strategy → scoring → risk → execution → broker adapter →
journal, reading source rather than docstrings. Cross-checked every safety claim
made in a docstring or in `docs/` against the code that would have to enforce it.

> **Headline finding.** Milestone 1 built the *components* of a safety system but
> wired almost none of them into the path an order actually travels. Four entire
> safety subsystems — data freshness, the circuit breaker, the order state
> machine, and the economic-event gate — were **never called from anywhere**.
> Several docstrings asserted properties the code did not have.

---

## 1. Method note: a path discrepancy in the brief

The mission brief refers to `app/common/freshness.py`. That file does not exist.
The freshness module is at **`app/marketdata/freshness.py`**. Audited there.

---

## 2. Dead safety code — subsystems that existed but were never invoked

Verified mechanically, not by reading comments:

```
$ grep -rn "CircuitBreaker\|circuit_breaker" --include=*.py app/ | grep -v app/risk/circuit_breaker.py
app/common/db.py:167:class CircuitBreakerEvent(Base):          # schema only
app/journal/store.py:76:    def record_circuit_breaker(...)     # writer only, never called
app/dashboard/server.py:61:  "circuit_breaker_tripped": False,  # HARD-CODED False

$ grep -rn "assert_fresh\|StaleData\|check_freshness" --include=*.py app/ | grep -v app/marketdata/freshness.py
(no results)

$ grep -rn "OrderStateMachine\|assert_transition_allowed" --include=*.py app/ | grep -v app/execution/
(no results)

$ grep -rn "EventRiskGate\|event_calendar" --include=*.py app/ | grep -v app/marketdata/event_calendar.py
(no results)
```

| Subsystem | Built? | Invoked on the order path? | Doc claim | Reality |
|---|---|---|---|---|
| `marketdata/freshness.py` | Yes | **No — zero call sites** | ARCHITECTURE §7 said "not called on *every* pipeline read" | Understated. Called on **no** reads. |
| `risk/circuit_breaker.py` | Yes | **No** | SAFETY.md: "non-bypassable kill switch" | Never consulted. Dashboard hard-codes `False`. |
| `execution/order_state_machine.py` | Yes | **No** | "Deterministic order state machine" | Pipeline mutates `order.state` directly, bypassing legality checks. |
| `marketdata/event_calendar.py` | Yes | **No** | Economic-event guard | `config/events.yaml` is read by nothing. |

**Consequence:** every one of these was safety theater on the live path. The unit
tests passed because they exercised the modules *directly*, never through the
pipeline. High test count concealed zero integration.

---

## 3. Authorization boundary defects (PART 2)

### 3.1 `run_pipeline()` performs no authorization of its own

`app/orchestration/pipeline.py` accepts `mode` and `broker` as *independent*
parameters and never cross-validates them, never calls `ModeGovernor`, and never
verifies the broker's environment matches the mode. Its own docstring admits
"the caller is responsible." Therefore:

- `run_pipeline(mode=Mode.SHADOW, broker=AlpacaBroker(allow_live=True), ...)`
  would send **real live orders while reporting SHADOW**.
- `run_pipeline(mode=Mode.LIVE, ...)` submits orders with **no operator
  authorization anywhere in the call**, because the LIVE refusal lives in
  `cmd_run`, not in the pipeline.

### 3.2 The SHADOW guard clause is a no-op

```python
if not mode.allows_order_submission:
    # SHADOW: risk-approved but structurally cannot reach a broker wire.
    pass                      # <-- does nothing

order = journal.open_order(...)          # executes unconditionally
status = broker.submit_order(...)        # executes unconditionally
```

The comment claims SHADOW "structurally cannot reach a broker wire." The code
falls straight through and calls `broker.submit_order()` in **every** mode. The
only thing that prevented a real order was that `cli.py` happened to pass a
`ShadowBroker`. That is a caller convention, not a structural property.

### 3.3 `AlpacaBroker(allow_live=True)` is self-service

The adapter docstring states: *"There is no other way to make this class talk to
the live endpoint."* False. `allow_live` is an ordinary default-`False` boolean
parameter; any caller — a test, a scheduler, a dashboard route, a future
refactor — can pass `True` with no proof that any authorization ran. This is
precisely the "single Boolean controlled by calling code" the brief prohibits.

### 3.4 `config/local.yaml` is checked for *existence*, not *content*

`ModeGovernor` receives `local_config_path_exists=LOCAL_CONFIG_PATH.exists()`
and its own error message reads *"mode: LIVE must be set in config/local.yaml."*
It never verifies the file **contains** `mode: LIVE`. Any operator who has a
`local.yaml` at all (i.e. every real operator — that is where paper settings
live) satisfies this check.

### 3.5 Two independent config-layer escalation paths to LIVE

Both defeat the documented "LIVE must come from git-ignored `local.yaml`" invariant:

1. **Environment variable.** `_apply_env_overrides()` is applied last and filters
   no keys, so `AEGIS__mode=LIVE` sets the configured mode to LIVE from the
   environment. Combined with §3.4, `local.yaml` need only *exist*.
2. **`--config` flag.** `main()` exposes a global `--config` that layers an
   arbitrary operator-supplied YAML over everything. `--config /tmp/x.yaml`
   containing `mode: LIVE` sets LIVE without touching `local.yaml`.

These were **latent, not exploitable for real orders in Milestone 1**, because
`cmd_run` `sys.exit(1)`s for both PAPER and LIVE. They would have become live
holes the moment execution was wired — which is what this milestone does.

### 3.6 Same credentials for paper and live

`AlpacaBroker.__init__` reads `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`
regardless of `allow_live`, while `docs/SETUP.md` describes separately-named
live keys as a safeguard. The safeguard was documented but not implemented, so a
single credential pair reached both endpoints.

---

## 4. Risk engine: correct in isolation, starved of inputs

`app/risk/engine.py` is genuinely good — deterministic, no martingale, no
history-dependent sizing, sizing is a pure function of `(equity, entry, stop)`,
and it has no method capable of widening or removing a stop. **Verified correct.**

But the pipeline feeds it hard-coded zeros:

```python
account = AccountState(
    ...,
    sector_exposure_pct={},     # <-- max_sector_exposure can never fire
    trades_today=0,             # <-- max_trades_per_day can never fire
    realized_pnl_today=0.0,     # <-- max_daily_loss can never fire
    realized_pnl_week=0.0,      # <-- max_weekly_loss can never fire
)
```

Four configured risk limits were **unreachable in practice**. Their unit tests
passed by calling `RiskEngine.evaluate()` with synthetic `AccountState` objects.
This is the clearest example in the codebase of tests validating a component
while the integration silently disabled it.

Additional risk gaps found:
- Position size is `round(shares, 4)` — **fractional shares**. Alpaca rejects
  bracket/OCO orders on fractional quantities, so every protective exit would
  have failed at the broker.
- No buying-power *check*, only a silent downward resize. A candidate needing
  $10,000 against $50 of buying power became a $50 position that no longer
  respected the risk-based stop distance.

---

## 5. Execution / order lifecycle defects

- **Fill assumed from the submit return value.** `status.status == "filled"` at
  submission time is treated as a fill. Directly violates "never treat
  `submit_order()` returned as equivalent to filled."
- **Illegal transitions performed.** The pipeline drives `PROPOSED → SUBMITTED`
  and `PROPOSED → FILLED`. The state machine forbids both (`SUBMITTED` is only
  legal from `RISK_APPROVED`). Because `journal.update_order_state()` never calls
  `assert_transition_allowed()`, these silently succeeded.
- **No protective exits were ever sent.** `stop` and `targets` were written to
  the DB and never transmitted to the broker. Every "managed" position was
  managed only in SQLite — the exact failure mode the brief warns about.
- **No reconciliation.** Nothing compares local state to broker open orders or
  broker positions, at startup or ever.
- **Shorts unverified.** `side="SHORT"` is submitted whenever a strategy says
  `direction != "long"`, with no shortability / easy-to-borrow check.
- **Duplicate outcome rows.** A submitted candidate appends both `risk_approved`
  and `submitted` outcomes, inflating reported counts.

---

## 6. Market data / regime / session defects

- `MockProvider` supplied **all** data in every mode. No real provider existed.
- `bars_prev_day=bars` — the pipeline passes the *same* one-hour intraday window
  as both intraday and previous-day bars. Any strategy relying on previous-day
  levels was reading wrong data, silently.
- `session="regular"` hard-coded. No calendar, no clock, no holiday/early-close
  awareness.
- Regime hard-coded to flat/neutral via `build_regime_snapshot(spy_pct=0.0, ...)`.
  Honestly labelled `as_of="stub-no-live-feed"` — **doc claim verified correct**.
- `CatalystEngine` ran with `NullNewsProvider` only; `SecEdgarProvider` existed
  but was never constructed by any caller.

---

## 7. Circuit breaker: two false claims in one docstring

```
"Once tripped for a session_date, is_tripped() returns True for the
 remainder of that date no matter what calls it afterward."
```
False — `clear_for_new_session(session_date)` accepts *today's* date and pops it,
un-tripping the current session. Any caller, including strategy code, can do this.

```
"non-bypassable kill switch"
```
False — state lives in `self._tripped_dates`, an in-memory dict. Process restart
clears it entirely. A crash-loop would reset the breaker on every restart.

---

## 8. Self-assessment against previously documented gaps (PART 22)

### 8.1 Previously known gaps (documented in ARCHITECTURE §7 / SAFETY §10)
- No real market-data adapter; MockProvider only.
- Regime is a hard-coded flat stub.
- No bracket/OCO support in the Alpaca adapter.
- Freshness "not called on every pipeline read."
- `vwap_reclaim.py` dead-code reference to nonexistent `ctx.raw_signals_history`.
- No PAPER/LIVE reconciliation on restart.
- Intraday-margin-deficit not used as a risk gate.
- No dashboard auth; no secrets manager; no journal encryption; unpinned deps.
- Extended-hours order constraints not enforced in code.
- No IBKR adapter.

### 8.2 Newly discovered gaps the documentation did not acknowledge
1. Circuit breaker **never invoked anywhere** (docs implied it was active).
2. Circuit breaker state is **in-memory only** — lost on restart.
3. `clear_for_new_session()` can un-trip the **current** session, contradicting
   its own docstring and the "strategy cannot disable this" requirement.
4. Order state machine **never invoked**; the live path performs transitions the
   machine explicitly forbids.
5. Event calendar / `EventRiskGate` **never invoked**; `config/events.yaml` dead.
6. Freshness gap was worse than documented: **zero** call sites, not "some."
7. Four risk limits (daily loss, weekly loss, trades/day, sector exposure)
   **unreachable** because the pipeline hard-codes zeros into `AccountState`.
8. The SHADOW mode guard in `run_pipeline` is a **`pass` statement** — mode does
   not gate broker submission at all.
9. `AlpacaBroker(allow_live=True)` is callable by **any** code, contradicting its
   docstring's explicit claim that no other path exists.
10. `local_config_path_exists` checks **existence, not content** — the documented
    "`mode: LIVE` must be in `local.yaml`" invariant was never enforced.
11. `AEGIS__mode=LIVE` **environment variable** can set LIVE, bypassing the
    config-file requirement entirely.
12. Global `--config` flag can layer an **arbitrary YAML** that sets LIVE.
13. Paper and live used the **same credential env vars**, despite SETUP.md
    documenting separate ones.
14. Fills inferred from the **submit return value**.
15. Protective exits **never transmitted** to any broker.
16. **No shortability verification** before submitting a short.
17. Position sizing emits **fractional shares**, which are incompatible with
    Alpaca bracket/OCO orders.
18. `bars_prev_day=bars` — **wrong data** silently supplied to strategies.
19. Buying-power constraint **silently resizes** rather than rejecting, breaking
    the risk-based stop-distance relationship.
20. `journal.update_order_state()` performs **no transition validation**.

### 8.3 Claims verified correct
- `ShadowBroker` genuinely has no network client — structural, not conventional.
- `RiskEngine` sizing is genuinely a pure function of equity/entry/stop, with no
  access to trade history; no martingale or averaging-down capability exists.
- `RiskEngine` has no stop-widening or stop-removal method.
- `Strategy.evaluate()` gate sequence is non-overridable as documented.
- `OpportunityScorer` emits a component-by-component breakdown; no unexplained
  numbers.
- Rejected candidates are journaled with the same fidelity as accepted ones.
- Regime stub was **honestly labelled** `as_of="stub-no-live-feed"` rather than
  disguised as real.
- `config/local.yaml`, `.env`, and `data/*.db` are correctly git-ignored; no
  credentials were committed.
- Backtester implements walk-forward train/validation/OOS splitting and an
  overfit check as documented.
- The order state machine's transition table itself is correct — it forbids
  `SUBMITTED → FILLED` without a broker-confirmed event, as claimed. Its defect
  was non-invocation, not incorrectness.
