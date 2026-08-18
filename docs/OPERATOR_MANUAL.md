# Aegis Trader — Operator Manual

## Who this is for

You, as the single operator of this system. Nothing here assumes a team —
commands are single-user, config files are local files, there's no
multi-tenant concept.

## Daily Commands

```bash
export PATH="$PATH:$(python -m site --user-base)/bin"   # if pytest/CLI scripts aren't on PATH

python -m app.cli status          # shows mode, risk limits, broker, strategies
python -m app.cli demo-scan       # one-shot scan+score against MockProvider, no journaling
python -m app.cli run --mode shadow   # full pipeline cycle: scan -> catalyst -> strategy ->
                                        # score -> risk check -> journal (SHADOW: no broker call)
python -m app.cli dashboard --port 8080   # visual snapshot at http://127.0.0.1:8080
```

`run --mode X` requires `X` to match `mode:` in your active config
(`default.yaml`, overridden by `local.yaml` if present) — this is
intentional (see `docs/SAFETY.md` §3): the CLI will never silently run in
a different mode than what's configured.

## Reading `run` Output

```
Scanned 1 candidates → 1 outcomes journaled → 0 orders submitted.
  COIN   stage=strategy_no_setup score=n/a
```

`stage_reached` tells you exactly where each candidate stopped:
- `strategy_no_setup` — none of the enabled strategies found a valid setup
  (most common outcome; not an error)
- `scored` — got a score but is below `min_score_to_consider`
- `risk_rejected` — `RiskEngine` vetoed it; `rule_triggered` in the journal
  DB explains which rule
- `risk_approved` / `submitted` — cleared risk and (in SHADOW) was recorded
  as a hypothetical fill, or (in PAPER/LIVE, not yet wired) sent to the
  broker

Every outcome — including `strategy_no_setup` — is written to the
`candidates` table in the journal DB (default: `data/journal.db`), so
nothing is lost even when nothing "interesting" happened.

## Inspecting the Journal Database Directly

```bash
sqlite3 data/journal.db
sqlite> select ticker, score, decision, rule_triggered from candidates order by created_at desc limit 20;
sqlite> select * from risk_events order by at desc limit 10;
sqlite> select * from circuit_breaker_events order by at desc limit 10;
```

## Promotion Checklist (SHADOW → PAPER → LIVE)

This is a manual review checklist, not something the system can pass
itself through automatically — per spec §34, "performance is not
permission."

**Before moving `mode:` from SHADOW to PAPER:**
- [ ] All tests pass (`python -m pytest tests/ -q`)
- [ ] `run --mode shadow` has been exercised across a range of mock/real
      market conditions and outcomes look sane on manual review of the
      journal DB
- [ ] Alpaca paper API keys obtained and set as environment variables
      (see `docs/SETUP.md` §5a) — never written into any config file
- [ ] A real market-data adapter is wired in place of `MockProvider` (see
      `docs/ARCHITECTURE.md` §7 "Known Gaps") — PAPER mode against
      synthetic data isn't meaningful

**Before moving `mode:` from PAPER to LIVE** — all of
`config/default.yaml`'s `promotion_criteria.paper_to_live` thresholds, at
minimum:
- [ ] ≥100 paper trades completed
- [ ] ≥20 distinct trading sessions
- [ ] Expectancy ≥ 0.05R, profit factor ≥ 1.2, max drawdown ≤ 15%
- [ ] Zero unresolved execution errors in the journal
- [ ] Zero risk-control violations (every `risk_events` row with
      `decision=REJECTED` correctly blocked what it should have)
- [ ] You have manually reviewed a sample of both approved and rejected
      trades and agree with the risk engine's reasoning
- [ ] You have generated a **separate, clearly-named** live Alpaca API key
      (see `docs/SETUP.md` §5b) and are prepared to start with the
      smallest possible size

Then, and only then: create `config/local.yaml` with `mode: LIVE` and run
with `--i-understand-this-is-live-trading`. See `docs/SAFETY.md` §2 for
exactly what this does and does not bypass.

## Emergency Stop

There is no running persistent process yet in this milestone (`run` is a
single scan-to-decision cycle, not a scheduler loop — Phase 12). Once a
scheduler loop exists (e.g. via `apscheduler`, already a dependency), the
emergency stop procedure will be: kill the process (`Ctrl+C` or `kill
<pid>`) — since `ShadowBroker`/`AlpacaBroker` never hold state that isn't
also durably in the journal DB, killing the process cannot "strand" an
order in an unknown state; `OrderStateMachine` always re-syncs from the
broker's own confirmed order state on the next read, never assumes a fill
locally. See `docs/DISASTER_RECOVERY.md`.
