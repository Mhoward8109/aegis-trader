# Aegis Trader

A personal day-trading research, decision-support, paper-trading, and (eventually,
under strict governance) automated-execution workstation for U.S. equities.

**Status: Milestone 1 complete — MODE 0/1 (Research / Shadow) scanner +
catalyst engine + strategy scoring + risk engine run end-to-end via
`python -m app.cli run --mode shadow`, against offline synthetic data, with
zero broker credentials wired anywhere in this codebase and no possible
path to a real-money order. 64 automated tests pass
(`python -m pytest tests/ -q`). See `docs/ARCHITECTURE.md` and
`docs/SAFETY.md` before doing anything else. Built ≠ tested ≠ verified ≠
safe for live trading — real market data, real broker paper integration,
and the continuous-monitoring loop are later phases, not yet built.**

## Quick orientation

- `docs/ARCHITECTURE.md` — system design, provider research, tech-stack rationale
- `docs/SAFETY.md` — the four operating modes and how promotion works
- `docs/OPERATOR_MANUAL.md` — how to run it day to day
- `docs/STRATEGY_GUIDE.md` — how to add/measure a new strategy
- `docs/SETUP.md` — installation and credential setup
- `docs/DISASTER_RECOVERY.md` — what to do when something breaks mid-session
- `docs/SECURITY.md` — credential handling, network surface, known gaps

Run `python -m app.cli status` any time to see current mode, config, and
whether any broker is connected. Run `python -m app.cli run --mode shadow`
to exercise the full milestone pipeline end to end.
