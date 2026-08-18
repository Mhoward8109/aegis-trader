# Aegis Trader — Security Documentation

## Credential Handling

- No broker or data-provider credential is ever hard-coded, logged, or
  committed. `app/broker/alpaca_adapter.py` and `app/catalyst/engine.py`
  read all secrets from environment variables
  (`ALPACA_API_KEY_ID`/`ALPACA_API_SECRET_KEY`, separately-named
  `ALPACA_LIVE_API_KEY_ID`/`ALPACA_LIVE_API_SECRET_KEY`,
  `SEC_EDGAR_CONTACT_EMAIL`, `BENZINGA_API_KEY` — see `docs/SETUP.md` §6).
- `config/default.yaml` is version-controlled and must never contain a
  credential or `mode: LIVE`. `config/local.yaml` is the git-ignored
  override file for anything environment-specific; it's still recommended
  to keep secrets in env vars rather than `local.yaml`, since a file is
  easier to accidentally commit or copy than a shell environment.
- Broker API keys should be generated with **trading-only scope, no
  withdrawal/transfer permission** — this is a setting on Alpaca's own
  key-generation screen, not something this codebase can enforce
  programmatically, hence documented here as a required manual step
  (`docs/SETUP.md` §5a).
- Paper and live keys use **different, distinctly-named** environment
  variables specifically so a copy-paste or `.env` mistake surfaces as a
  missing-variable error rather than silently routing a paper-mode run's
  keys at a live account or vice versa.

## Network Surface

- `ShadowBroker` has no HTTP/WebSocket client of any kind — it is not
  merely unused in SHADOW mode, it structurally cannot make an outbound
  network call (`tests/test_shadow_broker.py` asserts this).
- `AlpacaBroker`'s live base URL (`https://api.alpaca.markets`) is only
  ever constructed when `allow_live=True` is passed by the caller, and the
  only caller permitted to do that is `app/cli.py`, gated by
  `ModeGovernor` (`app/common/modes.py`) — see `docs/SAFETY.md` §2.
- The FastAPI dashboard (`app/dashboard/server.py`) binds to
  `127.0.0.1` only by default in `app/cli.py::cmd_dashboard` — it is not
  exposed on the network unless an operator explicitly changes the host
  binding, which is a deliberate choice given the dashboard currently has
  no authentication layer.

## Data Handling

- All journal data (candidates, orders, risk events, circuit-breaker
  trips) is stored locally in SQLite (`data/journal.db`) — no data is sent
  to any third party beyond the broker/market-data/news providers
  themselves, and only for the specific tickers/timeframes being
  evaluated.
- SEC EDGAR and (future) Benzinga calls only ever request public market
  data / public filings — no personal or account-identifying information
  is ever included in those requests beyond the required contact-email
  User-Agent header for EDGAR.

## Known Gaps (documented, not hidden)

- **No dashboard authentication.** Fine for local-only (`127.0.0.1`)
  operation; must be added before ever exposing the dashboard beyond
  localhost.
- **No secrets manager / keychain integration yet** — environment
  variables are the current mechanism. Acceptable for a single-operator
  system; a future hardening pass could move to macOS Keychain or a
  proper secrets manager if this needs to run unattended on shared
  infrastructure.
- **No encryption at rest for the journal DB.** It is trade
  history/decisions, not credentials, so this is lower priority, but flag
  it before storing anything more sensitive there.
- **Dependency pinning**: `requirements.txt` currently lists unpinned or
  loosely-pinned versions for rapid initial development. Before any
  PAPER/LIVE use, pin exact versions (`pip freeze > requirements.lock.txt`
  or equivalent) so a `pip install` six months from now can't silently
  pull in a broker SDK with a breaking or behavior-changing update.

## Incident Response

If a credential is suspected compromised: revoke/regenerate it
immediately in the Alpaca dashboard (or the relevant provider's console),
update the environment variable, and restart the process — there is no
cached/persisted copy of the credential anywhere in this codebase's
storage to also purge.
