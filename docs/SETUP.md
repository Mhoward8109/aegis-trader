# Aegis Trader — Setup

## 1. Requirements

- Python 3.11+ (developed/tested on 3.14)
- No credentials are required for RESEARCH or SHADOW mode. Those paths use
  synthetic `MockProvider` data and the in-process `ShadowBroker`.

## 2. Install

```bash
cd aegis-trader
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Verify the install (no credentials needed)

```bash
python -m pytest tests/ -q
python -m app.cli status
python -m app.cli demo-scan
```

`demo-scan` runs against `MockProvider`'s deterministic synthetic data. The
test suite and this command exercise mocks and fixtures; they do **not** verify
Alpaca connectivity, Alpaca market data, a broker clock, or a paper fill.

**PAPER verification against a real Alpaca account has not yet occurred.
Nothing in this system has been run against Alpaca.** Built and tested do not
mean verified.

## 4. Configuration

- `config/default.yaml` is version-controlled and must never contain
  credentials or `mode: LIVE`.
- `config/local.yaml` is git-ignored and layers local settings over defaults.
  A requested `run --mode` must match the resolved configured mode.
- `config/events.yaml` is the manually maintained economic/earnings event
  calendar used by the event-risk blackout gate.

## 5. PAPER Credentials and Dependencies

### 5a. Alpaca paper-trading credentials — needed for PAPER mode

`AlpacaPaperBroker` is the only real-order adapter that the CLI constructs in
this milestone. It declares `BrokerEnvironment.PAPER` and uses the literal
`paper=True` against `https://paper-api.alpaca.markets`. It has no constructor
switch or URL override that can redirect it to LIVE.

The documented paper credential variables are:

- `ALPACA_PAPER_API_KEY_ID`
- `ALPACA_PAPER_API_SECRET_KEY`

Keep them outside configuration files and use a trading-only key without
withdrawal/transfer permission. Do not put credential values in shell history,
documentation, source, or configuration.

A PAPER run also constructs Alpaca market data, a broker-backed market session
service, a regime engine, a persistent breaker, and catalyst research. If a
required PAPER dependency cannot be constructed — credentials, broker, market
data, broker clock, regime engine, or catalyst provider — the CLI exits. It
does **not** fall back to `MockProvider` or `ShadowBroker`: a synthetic run
reported as PAPER would falsely present simulated results as real broker/market
results.

SEC EDGAR filing research uses `SEC_EDGAR_CONTACT_EMAIL`. The explicit
`--no-catalyst-research` PAPER option deliberately disables catalyst research
and prints a degraded banner; it is not an automatic dependency fallback.

### 5b. LIVE credentials and execution

Do not prepare or rely on a LIVE run from this milestone. `run --mode live`
refuses before constructing a broker, and `AlpacaLiveBroker` independently
refuses construction. No configuration, strategy setting, or boolean flag
enables live execution.

## 6. Environment Variable Summary

Set environment variables using your operating system's secure local mechanism;
never commit values or place them in configuration files. The documentation
refers to these names only:

- `ALPACA_PAPER_API_KEY_ID`
- `ALPACA_PAPER_API_SECRET_KEY`
- `SEC_EDGAR_CONTACT_EMAIL`

## 7. Running the Dashboard

```bash
python -m app.cli dashboard --port 8080
```

Then open `http://127.0.0.1:8080`. The dashboard is local by default and has no
authentication layer, so do not expose it beyond localhost.
