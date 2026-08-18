# Aegis Trader — Security Documentation

## Credential Handling

- No broker or data-provider credential is ever hard-coded, logged, or
  committed. The documented PAPER credential variables are
  `ALPACA_PAPER_API_KEY_ID` and `ALPACA_PAPER_API_SECRET_KEY`; SEC EDGAR uses
  `SEC_EDGAR_CONTACT_EMAIL`. See `docs/SETUP.md`.
- `config/default.yaml` is version-controlled and must never contain a
  credential or `mode: LIVE`. `config/local.yaml` is the git-ignored
  override file for anything environment-specific; it is still safer to keep
  secrets in environment variables rather than in `local.yaml`.
- Broker API keys should be generated with **trading-only scope, no
  withdrawal/transfer permission** — this is a setting on Alpaca's own
  key-generation screen, not something this codebase can enforce
  programmatically.
- Paper credentials must be kept separate from any live-account credentials.
  PAPER execution uses the paper adapter and cannot be redirected to a live
  endpoint by a constructor argument or URL override.

## Network Surface

- `ShadowBroker` has no HTTP/WebSocket client of any kind — it is not
  merely unused in SHADOW mode, it structurally cannot make an outbound
  network call (`tests/test_shadow_broker.py` asserts this).
- PAPER order submission uses `AlpacaPaperBroker`, whose declared environment
  is `BrokerEnvironment.PAPER`. It constructs Alpaca's client with the literal
  `paper=True` and addresses `https://paper-api.alpaca.markets`; the class
  exposes neither a paper/live switch nor a `url_override`.
- LIVE execution is operationally disabled twice: `python -m app.cli run
  --mode live` refuses before constructing a broker, and
  `AlpacaLiveBroker` refuses construction because
  `OPERATIONALLY_ENABLED` is `False`. There is no supported live network path
  in this milestone.
- The FastAPI dashboard (`app/dashboard/server.py`) binds to
  `127.0.0.1` only by default in `app/cli.py::cmd_dashboard` — it is not
  exposed on the network unless an operator explicitly changes the host
  binding, which is a deliberate choice given the dashboard currently has
  no authentication layer.

## Data Handling

- All journal data (candidates, orders, risk events, and data faults) is
  stored locally in SQLite (`data/journal.db`). Circuit-breaker state is kept
  separately in SQLite (`data/circuit_breaker.db` by default), so a journal
  problem cannot silently clear a trip.
- SEC EDGAR calls request public filings and include the required contact-email
  User-Agent header. No broker credential is included in those requests.

## Known Gaps (documented, not hidden)

- **No dashboard authentication.** Fine for local-only (`127.0.0.1`)
  operation; must be added before ever exposing the dashboard beyond
  localhost.
- **No secrets manager / keychain integration yet** — environment variables
  are the current mechanism. This is a limitation for unattended or shared
  infrastructure.
- **No encryption at rest for the journal or breaker SQLite databases.** They
  contain operational and trade-history records, not credential values, but
  the limitation matters before storing anything more sensitive.
- **PAPER has not been verified against a real Alpaca account.** Nothing in
  this system has been run against Alpaca. The tests exercise mocks and
  fixtures only; passing tests establish neither an Alpaca connection nor a
  verified paper run.

## Incident Response

If a credential is suspected compromised: revoke/regenerate it immediately in
Alpaca's dashboard (or the relevant provider's console), update the
corresponding environment variable, and restart the process — there is no
cached/persisted copy of the credential in this codebase's storage to purge.
