# Milestone 2 Closure Report

**Date:** 2026-08-18
**Branch:** `master`
**Milestone goal:** consume real current market data, identify and research real
opportunities, generate explainable trade plans, and safely execute/monitor those
plans against an official PAPER brokerage account — with no supported or
accidental path to real-money execution.

**Status: CLOSED AS BUILT + TESTED. NOT VERIFIED.**

Nothing in this system has ever contacted Alpaca. Every claim below rests on
tests against mocks and fixtures. The single most important sentence in this
document is that the PAPER path is **unverified**, and no amount of passing tests
changes that. See §E and `docs/PAPER_VERIFICATION_RUNBOOK.md`.

---

## A. Audit Report

### What existed before this milestone

Milestone 1 delivered a structurally complete, well-organised system: 64 passing
tests, a documented architecture, a deterministic scorer, a risk engine, a
backtester with walk-forward splitting, a journal, and a dashboard. It read as a
finished system.

### What was correct

Verified by fresh inspection, not taken from the docs:

- The **risk engine's arithmetic** — position sizing as `risk budget / stop
  distance`, then caps. Correct in isolation.
- The **deterministic scorer**, which emits a component-by-component breakdown.
  No unexplained numbers.
- The **journal recording rejected candidates** at the same fidelity as accepted
  ones.
- The **backtester's** walk-forward train/validation/OOS split and overfit check.
- **Secret hygiene.** `.env`, `config/local.yaml`, and `data/*.db` were correctly
  git-ignored. No credential has ever been committed.
- The **regime stub was honestly labelled** `as_of="stub-no-live-feed"` rather
  than disguised as a real reading.

### What was weak

- Every safety component existed as a well-written *module* with no *caller*.
- Documentation described enforcement that the code did not perform. The gap
  between `docs/SAFETY.md` and `app/` was the single largest defect.

### What was missing — the central finding

**Four safety subsystems had zero call sites.** `marketdata/freshness.py`,
`risk/circuit_breaker.py`, `execution/order_state_machine.py`, and
`marketdata/event_calendar.py` were fully implemented, fully tested in isolation,
and never invoked by the pipeline. They were unit-tested, which is why they
looked healthy.

Alongside those:

- `run_pipeline`'s SHADOW guard clause was literally `pass`, then fell through to
  `broker.submit_order()` in **every** mode. SHADOW placed real orders.
- `AlpacaBroker(allow_live=True)` was a plain default-`False` keyword argument
  callable from anywhere, despite a docstring claiming it was protected.
- `ModeGovernor` checked whether `config/local.yaml` **existed**, not whether it
  contained `mode: LIVE`.
- Two config-layer escalation paths to LIVE: the `AEGIS__mode=LIVE` environment
  variable, and the global `--config` overlay.
- The dashboard hard-coded `"circuit_breaker_tripped": False`. The one surface an
  operator would check in an emergency was a literal.

### What changed

Summarised in §C; per-file in §G.

### What defects were discovered during implementation

Eleven in total, fully documented in `docs/AUDIT_MILESTONE2.md`:

- **Five** silent failures found before the first commit (§5 of the audit),
  including two shipped strategies **structurally incapable** of producing a
  setup — `compute_all` never emitted the indicator keys they read.
- **Six** found during this session (§9 of the audit), three of which were
  introduced by the Milestone 2 work itself. Most significant:
  - a `NameError` in a new safety branch that **166 passing tests did not catch**;
  - `record_data_fault` destroying the diagnostic it existed to preserve;
  - SHADOW skipping freshness entirely, contaminating promotion evidence.

One prior assessment is **retracted** in §11 of the audit: the order state
machine's `SUBMITTED → FILLED` prohibition was called correct, and was not.
Forbidding that transition forced fast fills into `UNKNOWN`, and `UNKNOWN`
suppresses protective-exit attachment — the stricter table produced *fewer*
protected positions.

### What remains unverified

Everything that touches an external system. No Alpaca call, no EDGAR request, no
real quote, no real order, no real fill, no real reconciliation. See §D and §E.

---

## B. Safety Invariants

Reported by invariant, per PART 19. Every invariant below is enforced by
**production code** and proven by a **named test**. "Status" describes the
strength of the evidence, not the developer's confidence.

| # | Invariant | Enforcing component | Proving tests | Status |
|---|---|---|---|---|
| 1 | LIVE is operationally disabled | `AlpacaLiveBroker.OPERATIONALLY_ENABLED=False` (refuses in `__init__`) + `cli.cmd_run` LIVE branch refusing before any broker construction | `test_live_broker_refuses_construction_while_operationally_disabled` | TESTED (two independent stops) |
| 2 | Unauthorized LIVE execution is blocked | `ExecutionAuthorizer` (9 check groups) | `test_live_refuses_config_not_from_permitted_local_source`, `test_live_refuses_without_per_run_operator_authorization`, `test_direct_pipeline_call_cannot_bypass_live_cli_guard`, `test_execution_engine_and_broker_refuse_calls_without_real_grant` | TESTED |
| 3 | PAPER cannot silently route to LIVE | `AlpacaPaperBroker` hard-codes `paper=True`; no `url_override`; `MODE_REQUIRES_BROKER_ENVIRONMENT` | `test_grant_is_rejected_by_different_broker_environment` | TESTED — **structurally**, not confirmed against Alpaca (see §E limitation) |
| 4 | Authorization fails closed on missing evidence | `AuthorizationEvidence` fields default to a `_MISSING` sentinel | `test_each_unset_authorization_evidence_field_fails_closed` (parameterised over every field) | TESTED |
| 5 | Strategy code cannot mint authority | Grant seal private to `authorization`; reset seal private to the breaker module | `test_no_module_outside_authorization_imports_the_grant_seal`, `test_no_strategy_risk_execution_scanner_or_orchestration_module_uses_reset_seal` | TESTED (enforced by import-graph assertion, so a future refactor trips it) |
| 6 | Stale critical data blocks execution | `FreshnessGate.require` + pipeline freshness gate (all modes) | `test_unusable_market_data_refuses_submission_and_is_journaled`, `test_shadow_refuses_stale_data_instead_of_journaling_a_hypothetical_trade` | TESTED against synthetic timestamps only |
| 7 | Unusable/erroring market data fails closed | Pipeline provider-failure boundary | `test_provider_failure_or_timeout_fails_closed_and_is_journaled` | TESTED |
| 8 | Incoherent data blocks execution | Pipeline Gate C1b quote/bar coherence (`quote_bar_tolerance_pct=5.0`) | `test_quote_bar_price_scale_disagreement_refuses_and_journals` | TESTED |
| 9 | Broker/data initialization failure blocks PAPER startup | `cli._run_paper_cycle` — every failure calls `_refuse()` → `sys.exit(1)`; **no** `except` substitutes a mock | Verified by inspection and by CLI execution; **no automated test** | BUILT — gap noted in §E |
| 10 | A submit receipt is never treated as a fill | `OrderLifecycle` + broker-confirmed state | `test_submit_receipt_never_marks_filled_without_fresh_broker_query`, `test_rejected_broker_order_is_not_recorded_as_a_fill`, `test_partial_fill_stays_partial_until_broker_reports_full_fill`, `test_timeout_after_submission_is_unknown_and_never_retried`, `test_unknown_broker_status_fails_to_unknown`, `test_duplicate_partial_broker_event_is_idempotent` | TESTED |
| 11 | Circuit breaker blocks new entries, preserves protective exits | `PersistentCircuitBreaker` | `test_tripped_breaker_blocks_entries_but_permits_protective_exits` | TESTED |
| 12 | Breaker state survives restart | SQLite-backed breaker | `test_breaker_trip_persists_across_fresh_process_object` | TESTED |
| 13 | Operator reset is controlled | Module-level `issue_operator_reset()` is the only source of a reset token | `test_plain_code_cannot_reset_breaker_without_operator_authorization`, `test_reset_requires_explicit_operator_path_and_same_session_opt_in` | TESTED |
| 14 | Reconciliation discrepancies stop trading | `BLOCKING_DISCREPANCIES` incl. `MISSING_BROKER_ORDER`, `UNSUBMITTED_LOCAL_ORDER` | `test_crash_before_submission_...`, `test_crash_after_submission_...`, `test_crash_after_partial_fill_...`, `test_restart_while_holding_position_...`, `test_restart_with_open_order_...`, `test_unexpected_broker_position_blocks_new_entries`, `test_local_broker_position_disagreement_blocks_new_entries`, `test_disconnect_blocks_pipeline_before_new_entries` | TESTED via fresh objects on shared SQLite paths — **not** via real process termination |
| 15 | Strategies cannot bypass deterministic risk authorization | `run_pipeline` requires `authorizer` + `circuit_breaker` with no defaults; state machine forbids `PROPOSED →` broker states | `test_each_load_bearing_risk_limit_refuses_unsafe_entry` (parameterised over every load-bearing limit), `test_risk_gate_cannot_be_skipped_on_the_way_to_a_broker`, `test_risk_rejected_is_terminal_so_a_rejection_cannot_be_walked_back` | TESTED |
| 16 | Sizing is deterministic; no martingale | `RiskEngine` | `test_sizing_is_floor_of_risk_budget_divided_by_stop_then_capped`, `test_losses_never_increase_size_no_martingale` | TESTED |
| 17 | Critical diagnostics never silently disappear | `_json_safe` coercion in `journal/store.py`; `record_data_fault` | `test_shadow_stale_data_is_journaled_as_a_data_fault_not_silently_dropped` | TESTED |
| 18 | Health surfaces never fabricate healthy values | `observability/health.py`; dashboard literal removed | `test_snapshot_reports_real_tripped_breaker_reason`, `test_unavailable_inputs_are_marked_and_never_healthy`, `test_status_is_blocked_when_breaker_is_tripped`, `test_stale_last_quote_is_not_healthy`, `test_health_endpoint_returns_all_required_fields` | TESTED |
| 19 | Shorts are refused without positive verification | Shortability gate (`require_easy_to_borrow=True`, `long_only=True` default) | Covered within `test_each_load_bearing_risk_limit_refuses_unsafe_entry` | TESTED against fixture asset data only |

**Invariant 9 is the weakest entry in this table** and is the one most likely to
regress silently, because it is enforced only by the *absence* of a fallback
`except`. A future well-intentioned "make PAPER more robust" change could
reintroduce one. Recommended next-milestone item: an automated test asserting
that a PAPER run with a broken provider exits non-zero and never constructs
`MockProvider`.

---

## C. Architecture Changes

### C.1 Authorization became a distinct layer

**Before:** strategy → risk → `broker.submit_order()`, with a boolean.
**Now:** Strategy → Risk Engine → **Execution Authorization** → Execution Engine
→ Broker Adapter.

`ExecutionAuthorizer.evaluate()` requires an `AuthorizationEvidence` record whose
18 fields each default to a `_MISSING` sentinel that fails closed. A LIVE order
requires *all* of: config says LIVE; the LIVE config came from the permitted
local source; explicit per-run operator authorization; risk approval; freshness
passed; valid broker/account state; no breaker trip; execution-engine
authorization; a broker adapter explicitly constructed for LIVE; and broker
confirmation.

**Why:** the previous design let one boolean, reachable from strategy code, span
the whole distance to real money. Evidence that must be *supplied* cannot be
*forgotten* — an omission becomes a refusal rather than a permission.

### C.2 PAPER and LIVE became separate classes

`allow_live` is gone. `AlpacaPaperBroker` (`BrokerEnvironment.PAPER`, literal
`paper=True`, no `url_override`) and `AlpacaLiveBroker`
(`OPERATIONALLY_ENABLED=False`) are distinct types.

**Why:** a boolean argument is flipped by a typo, a default change, or a
misplaced kwarg. Reaching LIVE now requires *naming a different class* that
refuses to construct. The type system carries the safety property.

### C.3 The four dead safety subsystems were wired in

Freshness, circuit breaker, order state machine, and event calendar now have real
call sites in `run_pipeline`. `run_pipeline` requires `authorizer` and
`circuit_breaker` as keyword arguments **with no defaults**, so a caller cannot
omit them.

**Why:** an unwired safety module is worse than an absent one — it produces
documentation and test coverage that describe protection nobody has.

### C.4 The breaker became persistent and operator-gated

SQLite-backed, surviving restart. Reset tokens come only from module-level
`issue_operator_reset(operator=, reason=)`, so holding a breaker instance does not
confer the power to reset it. An import-graph test forbids strategy, risk,
execution, scanner, and orchestration modules from touching the reset seal.

**Why:** an in-memory breaker resets itself on the restart that a crash causes —
it is weakest at the exact moment it is needed.

### C.5 Reconciliation gained separate "finished" concepts

`_BROKER_WORK_COMPLETE` (reconciliation only) is distinct from `TERMINAL_STATES`
(full trade lifecycle, in which `FILLED` is deliberately non-terminal). See audit
§9.3.1 and §10.3.

**Why:** using one predicate for both halted trading after every successful fill.

### C.6 Diagnostic writes became lossy rather than fatal

`_json_safe` recursively coerces any payload into something serializable, falling
back to `repr`.

**Why:** two independent defects in this codebase involved a diagnostic write
aborted by its own payload. A `repr` is readable by a human; a missing row is
readable by nobody.

### C.7 PAPER has no synthetic fallback

`cli._run_paper_cycle` exits on any dependency failure. There is deliberately no
`except` that substitutes `MockProvider` or `ShadowBroker`.

**Why:** a PAPER run that degraded to synthetic data would report *simulated*
results under a heading claiming *real* ones, and those results feed promotion
decisions.

### C.8 Absent inputs report UNKNOWN

RESEARCH/SHADOW pass `session_service=None, regime_engine=None`, so regime reports
`UNKNOWN` rather than a fabricated flat/neutral reading.

**Why:** a wrong label is worse than an absent one, because it looks usable.

---

## D. Capability Matrix

Labels are used strictly. **VERIFIED requires evidence from a real external
system, which no subsystem has.** Anything exercised only through mocks is capped
at TESTED, regardless of coverage.

| Subsystem | Status | Basis and honest limitation |
|---|---|---|
| Scanner | **TESTED** | Universe + criteria filtering tested. Real universe (16 liquid tickers) is a hard-coded default, not a screened universe. |
| Market Data | **BUILT** | `AlpacaMarketDataProvider` written against verified API docs; `field_availability()` marks unsupported fields rather than fabricating. **Never called against Alpaca.** IEX-only on the free tier — IEX volume is a venue subset, not consolidated tape. |
| Catalyst Research | **BUILT** | `SecEdgarFilingProvider` replaces `NullNewsProvider`. **No EDGAR request has ever been sent.** |
| SEC Research | **BUILT** | Form classification (8-K, 10-Q, 10-K, S-1, S-3) is deterministic; dilution signals are classified by code, never decided by an LLM. Unexercised against real filings. |
| Technical Analysis | **TESTED** | Indicators tested on fixtures. The Milestone 1 defect where `compute_all` never emitted keys the strategies read is fixed and covered. |
| Strategy | **TESTED** | Both strategies now produce setups (they previously could not). Two strategies only — no sprawl, per PART 20. |
| Scoring | **TESTED** | Deterministic, component-by-component breakdown. No unexplained numbers. |
| Risk | **TESTED** | Every load-bearing limit has a refusal test. Sizing is `floor(risk budget / stop distance)` then capped. No martingale, no averaging down. |
| Freshness | **TESTED** | Hard gate in all modes. Tested against synthetic timestamps; **never against real feed latency.** |
| Execution Authorization | **TESTED** | Strongest-evidence subsystem: bypass attempts, direct lower-level calls, sentinel fail-closed, and import-graph isolation all tested. |
| Broker | **BUILT** | Paper/live class split and grant requirement tested with fakes. **No real broker call.** |
| Order Management | **TESTED** | 13-state machine wired; broker-confirmed state authoritative. Tested with a fake broker. |
| Reconciliation | **TESTED** | Blocking discrepancies tested. Restart is simulated with fresh objects on shared SQLite, **not** real process termination. |
| Exit Management | **BUILT** | Broker-native brackets preferred; a stop existing only in the database does not count as managed. **Broker acceptance has never been observed.** |
| Circuit Breaker | **TESTED** | Persistence, entry blocking, exit preservation, controlled reset all tested. |
| Journal | **TESTED** | Records rejections and data faults; `_json_safe` prevents self-destruction. Full PART 21 field set written. Reconstruction of a *real* transaction unproven. |
| Backtesting | **TESTED** | Walk-forward splitting and overfit check as documented. Unchanged this milestone. |
| Dashboard | **TESTED** | Hard-coded `circuit_breaker_tripped: False` removed; `GET /health` added; unavailable inputs report UNAVAILABLE/UNKNOWN/BLOCKED. |
| Recovery | **TESTED** | Five crash/restart scenarios block conservatively. Simulated restarts only. |
| CLI | **TESTED** | `status`, `demo-scan`, `run`, `breaker status/reset`, `dashboard` exercised. PAPER path exercised only to its refusal points. |

**No subsystem qualifies for VERIFIED or PAPER-LIVE.**

---

## E. Remaining Blockers

### E.1 Primary blocker — real PAPER verification (requires operator action)

Three environment variables:

| Variable | Where to obtain | Minimum permissions |
|---|---|---|
| `ALPACA_PAPER_API_KEY_ID` | alpaca.markets → sign up (free, email only, no funding) → dashboard → API Keys with the **Paper** toggle enabled | **Trading only. Do NOT enable transfers or withdrawals.** |
| `ALPACA_PAPER_API_SECRET_KEY` | Shown once at key generation | As above |
| `SEC_EDGAR_CONTACT_EMAIL` | Any address you control | None — EDGAR is free and keyless, but rejects requests without a contact email in the User-Agent |

The unprefixed `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` pair is accepted as a
fallback, but the prefixed names are strongly preferred so a paper key can never
be confused with a live key.

Procedure: `docs/PAPER_VERIFICATION_RUNBOOK.md` (staged A–E with acceptance
gates).

### E.2 Alpaca exposes no server-side paper-vs-live signal

`TradingClient(paper=True)` is **purely client-side URL selection**. There is no
API field that confirms a key is a paper key. `AccountStatus.PAPER_ONLY`
semantics are unconfirmed.

Compensating controls — all structural, none runtime-verified:
- `AlpacaPaperBroker` hard-codes `paper=True` with no parameter able to change it;
- no `url_override` is exposed;
- `base_url_in_use` is inspectable and asserted against
  `https://paper-api.alpaca.markets`;
- `trading_client` is a read-only property with no setter;
- the authorizer requires the grant's broker environment to match the mode.

**This cannot be closed by us.** It is an upstream limitation and must be stated
in any claim about paper isolation.

### E.3 No single-order CLI probe exists (found while writing the runbook)

Stage B of the runbook — the smallest possible real order — has **no supported
CLI entry point**. The only path to an order is a full pipeline cycle, which
means the first real order would arrive with all gates, strategy selection, and
scoring in play simultaneously. That is a poor first external test: a failure
would be hard to attribute.

The runbook marks Stage B **BLOCKED** rather than inventing an unsafe bypass.
Next-milestone item: an explicitly operator-gated, size-capped, single-symbol
probe command.

### E.4 Invariant 9 has no automated test

See §B. Enforced only by the absence of a fallback `except`.

### E.5 Restart testing is simulated

Recovery tests use fresh objects against shared SQLite paths, not real process
termination. A defect that only manifests under genuine process death — a
partially-flushed WAL, an OS-level file lock — would not be caught.

### E.6 Not built this milestone

Economic event guard (PART 11) is wired but has no populated event source; it
must not be hard-coded with future dates. Trailing stops exist in configuration
but are unexercised. No unattended scheduler exists, deliberately.

---

## F. Test Evidence

Command:

```
cd /home/user/workspace/aegis-trader
export PATH="$PATH:/home/user/.local/bin"
python -m pytest -q
```

Output (final run, working tree as committed):

```
..............................                                           [100%]
=============================== warnings summary ===============================
../../../../usr/local/lib/python3.14/site-packages/websockets/legacy/__init__.py:6
  /usr/local/lib/python3.14/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning:
  websockets.legacy is deprecated; see
  https://websockets.readthedocs.io/en/stable/howto/upgrade.html for upgrade instructions
    warnings.warn(  # deprecated in 14.0 - 2024-11-09

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
174 passed, 1 warning in 19.07s
```

- **Passed:** 174
- **Failed:** 0
- **Errors:** 0
- **xfail / xpass:** 0 — every strict `xfail` filed by the adversarial suite has
  been fixed and its marker removed
- **Warnings:** 1 — third-party `DeprecationWarning` from the `websockets`
  package imported via `alpaca-py`. Not our code; no action available to us.
- **Duration:** 19.07s

Collected count breakdown for the new adversarial and observability suites (58 of
the 174; the remaining 116 pre-date this session):

| Suite | Collected |
|---|---|
| `test_invariant_authorization.py` | 9 |
| `test_invariant_broker.py` | 9 |
| `test_invariant_circuit_breaker.py` | 4 |
| `test_invariant_market_data.py` | 9 |
| `test_invariant_recovery.py` | 5 |
| `test_invariant_risk.py` | 14 |
| `test_invariant_shadow_data_integrity.py` | 3 |
| `test_observability_health.py` | 5 |

Per PART 19, the meaningful report is §B, not this count. The count is included
only because PART 24F asks for actual output. **174 passing tests did not prevent
a `NameError` in a new safety branch** (audit §9.5) — that is the honest
interpretation of this section.

---

## G. Changed Files

### Modified

| File | Change |
|---|---|
| `app/cli.py` | Rewritten (+534/-110). The prior version called the removed `run_pipeline(regime=...)` and would have crashed. Adds `_run_paper_cycle` (real Alpaca paper + real providers, no fallback), `_run_offline_cycle` (UNKNOWN regime rather than fabricated), `breaker status`/`breaker reset`, `--no-catalyst-research` degraded banner, credential-presence-only `status`, LIVE refusal before broker construction. |
| `app/orchestration/pipeline.py` | Freshness gate now fires in **all** modes (was skipped in SHADOW); provider-failure boundary around `get_bars`/`_fetch_prev_day_bars`; data faults journaled with string-coerced sources. |
| `app/execution/lifecycle.py` | Added `UNSUBMITTED_LOCAL_ORDER` and `RESOLVED_BROKER_ORDER`; `MISSING_BROKER_ORDER` and `UNSUBMITTED_LOCAL_ORDER` now blocking; added `_BROKER_WORK_COMPLETE` to stop over-blocking normal fills. |
| `app/journal/store.py` | Added `_json_safe` recursive coercion so a diagnostic write cannot be aborted by its own payload. |
| `app/broker/alpaca_adapter.py` | Added read-only `trading_client` property (no setter, so a caller cannot swap in a client pointed elsewhere). |
| `app/dashboard/server.py` | Removed hard-coded `"circuit_breaker_tripped": False`; added `GET /health`; health included in `/api/snapshot` and the inline dashboard. |
| `docs/ARCHITECTURE.md` | Rewritten for the real authorization model; §7 gaps corrected. |
| `docs/SAFETY.md` | Rewritten; §10 corrected. |
| `docs/SECURITY.md` | `allow_live` removed; paper/live class split documented. |
| `docs/SETUP.md` | Paper credential variables; no-fallback behaviour. |
| `docs/OPERATOR_MANUAL.md` | Breaker commands; unverified status. |
| `docs/AUDIT_MILESTONE2.md` | +346 lines: §9 six defects, §10 lessons, §11 retraction of a §8.3 claim. |

### Created

| File | Purpose |
|---|---|
| `app/observability/` | `health.py` — fail-closed `HealthSnapshot` + `build_health_snapshot()`. |
| `tests/invariant_support.py` | Shared adversarial fixtures. |
| `tests/test_invariant_authorization.py` | Invariants 1–5. |
| `tests/test_invariant_market_data.py` | Invariants 6–8. |
| `tests/test_invariant_broker.py` | Invariant 10. |
| `tests/test_invariant_risk.py` | Invariants 15–16, 19. |
| `tests/test_invariant_recovery.py` | Invariant 14. |
| `tests/test_invariant_circuit_breaker.py` | Invariants 11–13. |
| `tests/test_invariant_shadow_data_integrity.py` | Invariants 6, 17. |
| `tests/test_observability_health.py` | Invariant 18. |
| `docs/ADVERSARIAL_TEST_REPORT.md` | PART 18/19 deliverable. |
| `docs/PAPER_VERIFICATION_RUNBOOK.md` | PART 24 / next-phase procedure. |
| `docs/MILESTONE2_CLOSURE.md` | This document. |

### Deliberately not committed

Scratch patch scripts (`/home/user/workspace/patch_*.py`) and subagent reports
outside the repo. Two stray pytest output files were deleted before commit.

---

## H. Updated Documentation

All five stale documents were reviewed and corrected. `grep -rn "allow_live"`
across `docs/`, `app/`, and `tests/` now returns matches **only** in explicitly
historical contexts: the Milestone 1 audit record in `docs/AUDIT_MILESTONE2.md`,
and source comments in `app/broker/base.py` and `app/broker/alpaca_adapter.py`
explaining *why* the design was replaced. Those are intentional — deleting the
explanation would invite the pattern's return.

Each document now states: LIVE is operationally disabled; PAPER uses
`AlpacaPaperBroker`; PAPER has no MockProvider/ShadowBroker fallback;
authorization is performed by `ExecutionAuthorizer`; breaker state is persistent;
reset requires operator and reason; freshness is a hard execution gate;
reconciliation discrepancies can block trading; **PAPER verification against
Alpaca has not occurred**; and BUILT/TESTED ≠ VERIFIED.

No document describes future architecture in the present tense.

---

## I. Git State

| Item | Value |
|---|---|
| Branch | `master` |
| Pre-commit HEAD | `707fb8b71ace5ce603957b9de007806a7d40de30` |
| Final commit | *(recorded at commit time — see `git log -1`)* |
| Working tree after commit | clean |

Secret hygiene confirmed before commit: no `.env`, no `config/local.yaml`, no
`*.db`, no logs, no API responses. `data/circuit_breaker.db`, `logs/journal.db`,
and `logs/circuit_breaker.db` exist locally from CLI runs and are correctly
git-ignored. A scan for credential-shaped strings returned only Alpaca **header
names** in research documentation, which are public API identifiers, not secrets.

---

## Closing statement

Milestone 2's architecture goal is met: there is no supported or accidental path
to real-money execution, and the safety subsystems that were previously dead code
are now invoked, gated, and adversarially tested.

Milestone 2's *capability* goal is **not** verified. The system has never spoken
to a broker. The next action is `docs/PAPER_VERIFICATION_RUNBOOK.md` Stage A —
connectivity only, no orders — after the three environment variables in §E.1 are
available.

Unattended PAPER automation is not authorized. LIVE remains disabled.
