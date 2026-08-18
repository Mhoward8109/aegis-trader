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

---

## 9. Defects discovered *during* Milestone 2 implementation

Sections 1–8 record the audit of the code as it stood at the *start* of
Milestone 2. This section records defects discovered *while building* Milestone 2
— including three introduced by the Milestone 2 work itself. They are separated
from §8 deliberately: a defect found in your own fresh work is a different
quality signal from a defect inherited from the previous milestone, and merging
them would flatter the newer code.

Three were exposed by the adversarial invariant suite (PART 18). Three were
found by manual inspection and by driving the CLI end to end.

### 9.1 Market-data `get_bars()` exceptions escaped the fail-closed freshness path

**Defect.** `app/orchestration/pipeline.py` called
`provider.get_bars(ticker, "1min", ...)` and `_fetch_prev_day_bars(...)` with no
failure boundary. A provider timeout or HTTP error propagated out of
`_process_candidate` into the broad per-candidate exception handler.

**Why it mattered.** The broad handler keeps the run alive and logs a generic
error, which superficially looks like correct resilience. But it produced **no
market-data refusal record**: the candidate was journaled as an unspecified
error rather than as "refused because required data was unusable". A feed that is
timing out is precisely the condition the freshness architecture exists to catch,
and it was the single condition that bypassed it. Worse, the run continued to the
next ticker and failed identically on every one, so an outage looked like a
scan that simply found nothing.

**Detection.** `tests/test_invariant_market_data.py::test_provider_failure_or_timeout_fails_closed_and_is_journaled`,
parameterised over `TimeoutError` and `RuntimeError`. Originally filed as a
strict `xfail`.

**Fix.** Wrapped both bar fetches in a `try/except Exception`. On failure the
pipeline journals a data fault via `_journal_data_fault`, returns
`stage_reached="market_data_unavailable"` with `rejection_reason="provider_failure"`,
and — in an order-submitting mode — trips the breaker with
`BreakerTrigger.STALE_MARKET_DATA`. The breaker is tripped rather than merely
skipping the symbol because an erroring provider is a property of the *feed*, not
of the *symbol*.

**Regression test.** The two parameterised cases above, now passing (the `xfail`
marker was removed). The test also asserts the stage is
`market_data_unavailable` specifically, and *not* `not_authorized` — an operator
reading the journal needs to see "the feed failed", not "authorization declined",
because those demand completely different remedies.

### 9.2 A crashed order with no durable broker ID was ignored by reconciliation

**Defect.** `app/execution/lifecycle.py` built its local-order index as
`{str(o.broker_order_id): o for o in local_open_orders if o.broker_order_id}`,
silently dropping any locally-open order that carried no broker ID. No
discrepancy was raised for a `RISK_APPROVED` order left behind by a crash.

**Why it mattered.** The omission rested on the assumption that an order without
a broker ID was simply never submitted. That holds only if the ID is written
*before* the submission call — and it is not. A crash in the window between
`submit_order()` reaching the broker and the receipt being persisted leaves
exactly this row, **with real exposure attached to it**. Treating "no ID
recorded" as "nothing was sent" is the optimistic reading of a genuinely
ambiguous record, and PART 12 requires unknown exposure to block trading rather
than resolve itself in the convenient direction.

**Detection.** `tests/test_invariant_recovery.py::test_crash_before_submission_blocks_new_entries_after_restart`,
filed as a strict `xfail`.

**Fix.** Added `DiscrepancyKind.UNSUBMITTED_LOCAL_ORDER`, raised for any locally
open order with no broker ID, and added it to `BLOCKING_DISCREPANCIES`. The
detail string states the ambiguity explicitly rather than asserting a cause.

**Regression test.** `test_crash_before_submission_blocks_new_entries_after_restart`,
now passing.

### 9.3 `MISSING_BROKER_ORDER` was recorded but did not block trading

**Defect.** `DiscrepancyKind.MISSING_BROKER_ORDER` was created for a locally-open
order absent from the broker's working list, but was omitted from
`BLOCKING_DISCREPANCIES`, so it never stopped anything.

**Why it mattered.** The original reasoning was that such an order has probably
just filled or been cancelled and can be refreshed individually. That reasoning
fails in the case that matters most: after a crash between submission and the
recording of the outcome, "probably filled" and "probably cancelled" imply
**opposite** exposure — one leaves an unmanaged position, the other leaves none.
Until the order is refreshed the system does not know which, so a restart could
resubmit and double the position.

**Detection.** Two strict `xfail` tests:
`test_crash_after_submission_blocks_new_entries_after_restart` and
`test_restart_with_open_order_blocks_new_entries`.

**Fix.** Added `MISSING_BROKER_ORDER` to `BLOCKING_DISCREPANCIES`. Refreshing the
order clears the discrepancy and unblocks entries; guessing does not.

**Regression test.** Both tests above, now passing.

### 9.3.1 The over-correction, and how it was corrected

Making `MISSING_BROKER_ORDER` blocking immediately broke two previously-passing
tests, and the breakage was correct: the fix **halted trading after every normal
fill**.

The cause was a conflation of two different meanings of "finished". This
system's `TERMINAL_STATES` (in `app/execution/order_state_machine.py`) describes
the *full trade lifecycle*, in which `FILLED` is deliberately **not** terminal —
a filled entry still has an exit ahead of it. Reconciliation needs a narrower
question: *is the broker still expected to be working this order?* A `FILLED`
entry order is complete at the broker even though the trade is not complete in
this system, so its absence from the broker's working list is the expected end
state, not a fault.

Using `is_terminal()` therefore flagged every completed fill as a reconciliation
failure. A discrepancy that fires constantly is not a safety feature: it trains
operators to clear the alert without reading it, which destroys the value of the
real signal on the day it matters.

**Fix.** Introduced a separate set, `_BROKER_WORK_COMPLETE = {FILLED, CLOSED,
CANCELLED, REJECTED, RISK_REJECTED, EXPIRED}`, used *only* by reconciliation.
Absence from the broker's working list is now classified by local state:

| Local state | Classification | Blocks new entries |
|---|---|---|
| In `_BROKER_WORK_COMPLETE` | `RESOLVED_BROKER_ORDER` | No — expected end state |
| Still in flight (`SUBMITTED`, `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `UNKNOWN`) | `MISSING_BROKER_ORDER` | Yes — exposure unknown |

One test expectation was also updated rather than the production behaviour
weakened. `test_crash_after_partial_fill_preserves_exposure_and_blocks_duplicate_entry`
originally asserted that the run would continue and merely reject the symbol as a
duplicate. A `PARTIALLY_FILLED` order absent from the broker's working list means
the remainder is gone *and* the actually-filled quantity is unconfirmed, so
continuing would imply the exposure is known when it is not. The test now asserts
the halt. This is the one place in this milestone where a test assertion was
changed to expect **stricter** behaviour than it originally demanded; the change
is recorded here so it cannot be mistaken for a weakened test.

### 9.4 SHADOW mode skipped freshness enforcement entirely

**Defect.** The freshness gate was written as:

```python
if not freshness_report.all_required_fresh and mode.allows_order_submission:
```

`Mode.SHADOW.allows_order_submission` is `False`, so in SHADOW the entire gate
was skipped and hypothetical trades were recorded from arbitrarily stale data
with no comment.

**Why it mattered.** This moves no money, and it was initially tempting to
classify it as cosmetic. It is not. SHADOW output *is* the evidence base used to
decide whether a strategy deserves promotion to PAPER (PART 21), and a
hypothetical fill priced off a two-day-old quote is **indistinguishable in the
journal** from one priced off a good quote. The failure mode is not a bad trade;
it is a promotion decision made on corrupted evidence, months later, by someone
who reasonably trusts the journal. Poisoned evidence is worse than absent
evidence because it survives into the decision.

**Detection.** Manual inspection while wiring `app/cli.py`, prompted by noticing
that a SHADOW run reported `orders_submitted=3` even though
`Mode.SHADOW.allows_order_submission` is `False` — which exposed that several
gates keyed on that flag were inactive in SHADOW.

**Fix.** The gate now fires in all modes, with mode-appropriate consequences. In
an order-submitting mode it trips the breaker. In SHADOW it journals a data fault
and returns `stage_reached="stale_data"`, `gate="freshness"`,
`rejection_reason="stale_required_data"`. The persistent breaker is deliberately
**not** tripped in SHADOW: SHADOW places nothing at a broker, so there is no
exposure to arrest, and letting an offline run latch a persistent breaker would
block later real runs over a fault that risked nothing.

**Regression test.** `tests/test_invariant_shadow_data_integrity.py`, three tests:
the stale-data refusal itself, the journaling of the refusal as a data fault
attributed to `strategy="n/a"`, and a **control** case proving fresh data still
reaches submission — without the control, the first test could pass for an
unrelated reason.

### 9.5 The first fix for 9.4 contained a `NameError` the whole suite missed

**Defect.** The first version of the SHADOW freshness branch returned
`PipelineOutcome(..., score=scored["score"], ...)`. The freshness gate runs at
roughly line 506; `scored` is not assigned until roughly line 562. The branch
would have raised `NameError` on its first execution.

**Why it mattered.** Not for the bug itself — it was found and fixed within
minutes — but for what it proves about the test suite. **The full 166-test suite
passed with this defect present**, because no test drove stale data through
SHADOW. A crash-on-first-execution defect in a freshly written safety branch was
invisible to 166 green tests. This is the clearest single piece of evidence in
this milestone for PART 19's instruction not to treat a test count as a quality
metric.

**Detection.** Writing the regression test for 9.4 — that is, the defect was
found only because a test was written specifically to execute the new branch.

**Fix.** Removed the `score=` argument. The branch now reports no score at all,
with an inline comment recording that this return happens before scoring and
emitting a score here would mean inventing one.

**Regression test.** `test_shadow_refuses_stale_data_instead_of_journaling_a_hypothetical_trade`
asserts `outcome.score is None`, so a future re-introduction of a fabricated
score fails.

### 9.6 `record_data_fault` destroyed the diagnostic it was written to preserve

**Defect.** `TradeJournal.record_data_fault` assigned
`c.setup_json = {"data_fault": context}` and committed. When `context` held a
non-JSON-serializable value the flush raised `TypeError: Object of type
SourceFreshness is not JSON serializable`, the transaction rolled back, and the
subsequent `commit()` raised `PendingRollbackError`.

**Why it mattered.** The net effect was **no row at all**. The mechanism whose
entire purpose is to preserve a record of a data fault was destroyed by the
detail it was trying to carry. This is the same failure shape as the
`major_risks` list-bind defect in §5 of this audit: a diagnostic write aborted by
its own payload. Two independent instances of one pattern in one codebase means
the pattern, not the instance, is the defect.

**Detection.** `tests/test_invariant_shadow_data_integrity.py::test_shadow_stale_data_is_journaled_as_a_data_fault_not_silently_dropped`
failed with the SQLAlchemy `PendingRollbackError` traceback above. The caller was
passing `freshness_report.stale_required_sources`, which contains
`SourceFreshness` dataclasses rather than strings.

**Fix.** Two changes, deliberately at two levels:

1. **Caller** — the pipeline now passes
   `[str(getattr(s, "name", s)) for s in ...stale_required_sources]`.
2. **Journal** — a new `_json_safe(value)` helper recursively coerces any payload
   into something `json.dumps` can accept: scalars pass through, dicts and
   sequences recurse, objects exposing `as_record()` use it, objects with a
   populated `__dict__` are converted, and anything else becomes its `repr`.

The helper is deliberately **lossy rather than strict**. It is used on diagnostic
payloads written during a fault, and raising there would destroy the record being
written. A `repr` is inspectable by a human reading the journal even when it is
not machine-parseable; a missing row is inspectable by nobody. Fixing only the
caller would have left the next caller free to reintroduce the same defect, which
is why both levels changed.

**Regression test.** `test_shadow_stale_data_is_journaled_as_a_data_fault_not_silently_dropped`,
which asserts the fault row exists, is `decision="REJECTED"`, and carries
`strategy="n/a"`.

---

## 10. Engineering lessons

These are recorded as durable conclusions, not as narrative. Each is stated
because a defect in §9 demonstrated it concretely in this codebase.

### 10.1 Diagnostic paths must be more robust than ordinary paths

An error-reporting mechanism that can fail on the data it is recording is itself
a reliability defect — and a particularly bad one, because it removes the
evidence needed to diagnose the original fault. Ordinary code may fail loudly.
Diagnostic code must degrade rather than raise.

Consequences adopted: payload coercion at the persistence boundary is lossy by
design (§9.6); a data fault is never attributed to a strategy, so a feed problem
cannot poison a strategy's measured hit rate; and a fault row is always written
even when its detail cannot be fully represented.

Two independent instances of this pattern were found in this codebase (§5
`major_risks`, §9.6 `record_data_fault`), which is why the fix was applied to the
journal's write boundary rather than only to the calling site.

### 10.2 Test coverage must include cross-mode behaviour, and test count is not a quality signal

166 tests passed while the SHADOW stale-data branch contained a `NameError` that
would raise on first execution (§9.5). The same suite passed while both shipped
strategies were structurally incapable of producing a setup (§8.2), and while
`_last_bar_timestamp` crashed every candidate in the freshness gate.

The common cause is that behaviour was gated on `mode.allows_order_submission`
while tests exercised predominantly one mode. A safety property that is
implemented per-mode must be *tested* per-mode.

Consequences adopted: tests are reported by safety invariant rather than by count
(PART 19); every new gate gets a control case proving the negative test could
have failed; and a branch that has never executed in any test is treated as
unwritten regardless of how many tests pass around it.

### 10.3 Reconciliation semantics require separate concepts, not one shared "terminal"

Four distinct meanings were collapsed into a single `TERMINAL_STATES` set:

| Concept | Question it answers | Where it belongs |
|---|---|---|
| Broker order work complete | Should the broker still be working this order? | `_BROKER_WORK_COMPLETE`, reconciliation only |
| Entry filled | Did the entry execute? | `OrderState.FILLED` |
| Position still open | Do we hold exposure right now? | Broker positions, not order state |
| Full trade lifecycle terminal | Is this trade finished for accounting? | `TERMINAL_STATES` |

`FILLED` answers *yes* to the first, *yes* to the second, *yes* to the third, and
*no* to the fourth. Using one concept for all four produced a fix that halted
trading after every successful fill (§9.3.1). Where two predicates disagree on
even one state, they are two predicates.

### 10.4 Promotion evidence must be trustworthy even when no money is at stake

SHADOW data is the input to the decision about whether a strategy deserves PAPER
or LIVE promotion. Stale, malformed, or misattributed SHADOW data is therefore a
safety issue with a long fuse: nothing goes wrong at the time, and the
consequence appears later as a confidently-made promotion decision resting on
corrupted evidence.

Consequences adopted: freshness is enforced in SHADOW (§9.4); data faults are
recorded rather than skipped, because a silent skip is indistinguishable from
"no candidate found" when reviewing a run months later; faults are attributed to
`strategy="n/a"`; and the regime attached to a hypothetical trade is recorded as
`UNKNOWN` rather than as a fabricated flat/neutral reading, since a wrong label
is worse than an absent one precisely because it looks usable.

---

## 11. Correction to §8.3 of this document

§8.3 lists as *verified correct*:

> The order state machine's transition table itself is correct — it forbids
> `SUBMITTED → FILLED` without a broker-confirmed event, as claimed. Its defect
> was non-invocation, not incorrectness.

**That assessment was wrong, and this document is the place to say so.**

`ACKNOWLEDGED` is a state the *broker* passes through, not one the system can
require the broker to report. Alpaca may transition an order to `filled` without
this system ever observing an intermediate acknowledgement, particularly on a
fast fill. Forbidding `SUBMITTED → FILLED` therefore forced every fast fill into
`UNKNOWN` — and `UNKNOWN` suppresses protective-exit attachment. The stricter
table produced **fewer protected positions than a more permissive one**, which is
the opposite of what its strictness appeared to buy.

`OrderState.SUBMITTED` now permits `{ACKNOWLEDGED, PARTIALLY_FILLED, FILLED,
REJECTED, EXPIRED, UNKNOWN, CANCELLED}`. The property that actually matters is
preserved and tested separately: the **risk gate cannot be skipped**, so
`PROPOSED → SUBMITTED`, `PROPOSED → FILLED`, `PROPOSED → PARTIALLY_FILLED` and
`PROPOSED → ACKNOWLEDGED` all still raise
(`test_risk_gate_cannot_be_skipped_on_the_way_to_a_broker`), and
`RISK_REJECTED` remains terminal
(`test_risk_rejected_is_terminal_so_a_rejection_cannot_be_walked_back`).

The general lesson: **strictness is not the same as safety.** A constraint that
cannot be satisfied by the real external system does not prevent the bad
outcome; it routes execution into the error path, and the error path is usually
less protected than the success path.
