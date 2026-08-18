# Aegis Trader — Setup

## 1. Requirements

- Python 3.11+ (developed/tested on 3.14)
- No credentials required for RESEARCH or SHADOW mode — this is the whole
  point of the first milestone.

## 2. Install

```bash
cd aegis-trader
python -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
```

## 3. Verify the install (no credentials needed)

```bash
python -m pytest tests/ -q
# expect: NN passed

python -m app.cli status
python -m app.cli demo-scan
```

`demo-scan` runs the scanner + catalyst engine + scoring against
`MockProvider` (deterministic synthetic data, seeded) — if this prints a
ticker/score table, the core pipeline works with zero external
dependencies.

## 4. Configuration

- `config/default.yaml` — checked into version control, holds every
  trading-relevant parameter (spec §32: nothing trading-relevant lives in
  source code). **Never put `mode: LIVE` or credentials here.**
- `config/local.yaml` (create this yourself, it does not exist by default)
  — git-ignored, overrides `default.yaml` key-by-key. This is where you'd
  eventually set `mode: PAPER` or (much later, deliberately) `mode: LIVE`.
- `config/events.yaml` — manually-maintained economic/earnings event
  calendar used by the event-risk blackout gate. Empty by default; must be
  refreshed by the operator (see comments in the file).

## 5. Credentials — What You Will Eventually Need, and Why

Nothing below is required for the current milestone (RESEARCH/SHADOW mode
work with zero credentials, using `MockProvider`). This is what unlocks
each *later* phase:

### 5a. Alpaca paper-trading API key — needed for Phase 9 (PAPER mode)

- **What**: An Alpaca API key ID + secret, scoped to the **paper**
  environment.
- **Why**: Lets `AlpacaBroker` submit orders to Alpaca's simulated paper
  account (`https://paper-api.alpaca.markets`) instead of `ShadowBroker`'s
  in-process simulation — the first time orders leave the process at all,
  still with zero real money at risk.
- **Where**: Sign up free at [alpaca.markets](https://alpaca.markets) —
  email only, no funded/live account required. Generate keys under
  Dashboard → "API Keys", making sure the **Paper Trading** toggle is
  selected (not Live).
- **Permissions**: Trading-only scope if Alpaca's key-generation UI offers
  granular scopes at key-creation time; there is no reason this system
  ever needs withdrawal/transfer permission, and it should never be
  granted.
- **How it's read**: environment variables `ALPACA_API_KEY_ID` and
  `ALPACA_API_SECRET_KEY` (never written to any config file, never
  committed).
- **Safer alternative**: none needed — the paper endpoint already *is* the
  safe, no-real-money alternative to a live account.

### 5b. Alpaca live API key — needed only for Phase 14, and only by hand

- **What/where**: same Alpaca dashboard, but generated against a funded,
  live brokerage account, with the **Live Trading** toggle selected.
- **Why**: Required only if/when you and this assistant jointly decide, after
  reviewing `config/default.yaml`'s `promotion_criteria.paper_to_live`
  evidence bar, to actually enable Mode 3 LIVE. See `docs/SAFETY.md` §2 and
  §9 — this is a deliberate, two-factor, manual promotion, never automatic.
- **Env vars**: `ALPACA_LIVE_API_KEY_ID` / `ALPACA_LIVE_API_SECRET_KEY` —
  deliberately named differently from the paper vars so a copy-paste
  mistake can't silently point a paper run at a live key.
- **Safer alternative**: keep using PAPER indefinitely; nothing forces a
  timeline for this step.

### 5c. SEC EDGAR contact email — optional, free, needed for catalyst filings lookups

- **What**: Just an email address, used in the `User-Agent` header SEC's
  API requires ([SEC EDGAR access policy](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)).
- **Why**: `SecEdgarProvider` (`app/catalyst/engine.py`) needs a compliant
  User-Agent to query EDGAR's REST/full-text search APIs for 8-K/10-Q
  filings as catalyst candidates.
- **Where**: Not "obtained" — you already have an email address. No signup.
- **Permissions**: none; EDGAR's API is public and free.
- **How it's read**: environment variable `SEC_EDGAR_CONTACT_EMAIL`.
- **Safer alternative**: none needed — this has no real-money or account
  risk implication of any kind.

### 5d. Benzinga News API key — optional, needed only if EDGAR filings aren't enough catalyst coverage

- **What**: A Benzinga API key.
- **Why**: `NewsProvider` is pluggable; a `BenzingaNewsProvider` (not yet
  built) would give real-time news catalysts beyond SEC filings.
- **Where**: [benzinga.com/apis](https://www.benzinga.com/apis/) — starts
  on a free AWS Marketplace tier.
- **Permissions**: read-only news API key; no account-linkage risk.
- **Safer alternative**: keep using `SecEdgarProvider`/`NullNewsProvider`
  only — the system already runs correctly (just with fewer catalysts
  detected) without this key.

## 6. Environment Variable Summary

```bash
# Optional today, needed for Phase 9+:
export ALPACA_API_KEY_ID="..."
export ALPACA_API_SECRET_KEY="..."

# Optional, only for a deliberate, reviewed promotion to LIVE (Phase 14):
export ALPACA_LIVE_API_KEY_ID="..."
export ALPACA_LIVE_API_SECRET_KEY="..."

# Optional, improves catalyst engine, free:
export SEC_EDGAR_CONTACT_EMAIL="you@example.com"

# Optional, improves catalyst engine, paid beyond free tier:
export BENZINGA_API_KEY="..."
```

Put these in your shell profile or a local `.env` file loaded by
`python-dotenv` (already a dependency) — never in a committed config file.

## 7. Running the Dashboard

```bash
python -m app.cli dashboard --port 8080
# then open http://127.0.0.1:8080
```

Shows the current mode banner (color-coded; LIVE pulses red), top-10
scored opportunities with full score breakdowns, risk-engine limits, and
current positions. Defaults to `MockProvider` until a real market-data
adapter is wired in.
