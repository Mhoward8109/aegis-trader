# Aegis Trader — Operator Manual

## Who this is for

You, as the single operator of this system. Nothing here assumes a team —
commands are single-user, config files are local files, and there is no
multi-tenant concept.

## Daily Commands

```bash
export PATH="$PATH:$(python -m site --user-base)/bin"   # if pytest/CLI scripts aren't on PATH

python -m app.cli status
python -m app.cli demo-scan
python -m app.cli run --mode shadow
python -m app.cli run --mode paper
python -m app.cli breaker status
python -m app.cli dashboard --port 8080
```

`run --mode X` requires `X` to match `mode:` in the active configuration. A
PAPER run constructs its real-paper dependencies or exits; it never switches to
`MockProvider` or `ShadowBroker`. `run --mode live` is not an operator path:
it refuses before broker construction, and `AlpacaLiveBroker` independently
refuses construction.

**PAPER verification has not occurred.** Nothing in this repository has been
run against an Alpaca account. A PAPER command and its adapter are built, but
that is not evidence of a verified broker connection or paper fill.

## Reading `run` Output

```
Scanned 1 candidates → 1 outcomes journaled → 0 orders submitted.
  COIN   stage=strategy_no_setup score=n/a
```

`stage_reached` tells you where each candidate stopped:
- `strategy_no_setup` — none of the enabled strategies found a valid setup
  (most common outcome; not an error)
- `scored` — got a score but is below `min_score_to_consider`
- `risk_rejected` — `RiskEngine` vetoed it; the journal explains which rule
- `stale_data`, `market_data_unavailable`, or `data_incoherent` — required
  market data was unusable; no order is placed
- `not_authorized` — `ExecutionAuthorizer` refused the evidence bundle; no
  execution grant exists for the candidate
- `submitted` — a SHADOW hypothetical trade was recorded, or a PAPER order was
  submitted and then queried again at the broker. Submission is not a fill.

Every candidate outcome and data-integrity refusal is journaled. Do not read a
missing order as a successful trade.

## Inspecting the Journal Database Directly

```bash
sqlite3 data/journal.db
sqlite> select ticker, score, decision, rule_triggered from candidates order by created_at desc limit 20;
sqlite> select * from risk_events order by at desc limit 10;
```

The persistent breaker has its own SQLite database (default:
`data/circuit_breaker.db`). Inspect it through the CLI:

```bash
python -m app.cli breaker status
```

## Promotion Checklist (SHADOW → PAPER → LIVE)

This is a manual review checklist, not something the system can pass itself
through automatically: performance is not permission.

**Before attempting PAPER:**
- [ ] All tests pass (`python -m pytest tests/ -q`). This proves only behavior
      exercised by mocks and fixtures; it does not verify Alpaca.
- [ ] `run --mode shadow` output and the journal have been manually reviewed as
      synthetic, hypothetical results — not as market evidence.
- [ ] The PAPER credential variables named in `docs/SETUP.md` are set outside
      config files.
- [ ] The operator understands that PAPER constructs `AlpacaPaperBroker` and
      Alpaca market data, a broker clock, a regime engine, and catalyst
      research (unless catalyst research is deliberately disabled). Failure to
      construct a required PAPER dependency exits rather than producing a
      synthetic substitute.
- [ ] The first actual PAPER connection, if authorized outside this document,
      is treated as verification work and reviewed as such. It has not yet
      happened.

**LIVE is not promotable in the current milestone.** The configuration and
per-run acknowledgement checks remain part of mode validation, but they do not
enable execution. `run --mode live` refuses before any broker is built, and
`AlpacaLiveBroker.OPERATIONALLY_ENABLED` is `False`. No checklist, strategy
setting, or single boolean changes that.

## Emergency Stop and Breaker Reset

`run` is a single cycle, not a scheduler loop. If a process must be stopped,
interrupt it. A restarted order-submitting run reconciles its local records
against broker state; blocking discrepancies prevent new entries until an
operator resolves them. It must not assume that an interrupted submission
filled or did not fill.

A tripped breaker survives restart in its separate SQLite state store. New
entries remain blocked while protective exits, position closes, and order
cancellation remain permitted. Resetting it requires an explicit, audited
operator command:

```bash
python -m app.cli breaker reset --operator NAME --reason "..."
```

The command may request an interactive confirmation. A same-session reset also
requires the deliberate `--same-session` option. The reset authorization is
minted by module-level `issue_operator_reset()`; holding a
`PersistentCircuitBreaker` instance does not provide reset authority, and
strategy code cannot reset it.
