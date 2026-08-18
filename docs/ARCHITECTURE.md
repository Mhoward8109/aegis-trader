# Aegis Trader — Architecture

Status: the PAPER execution path, authorization boundary, persistent circuit
breaker, freshness gate, and reconciliation controls are built. **PAPER has not
been verified against a real Alpaca account: nothing in this system has been
run against Alpaca.** Tests exercise mocks and fixtures only. **Built ≠ tested
≠ verified ≠ safe for live trading** — see `docs/SAFETY.md`.

## 1. Why Python, not JavaScript/TypeScript

The rest of the user's stack is Node/TS, but this project is Python. Reasons,
documented explicitly because it is a deliberate deviation:

- `pandas`/`numpy` are the de facto standard for the vectorized bar/indicator
  math this system runs constantly (VWAP, ATR, RSI, opening range, etc.).
- `alpaca-py` is Alpaca's official Python SDK.
- Backtesting and quantitative tooling are mature in the Python ecosystem.
- FastAPI supports the local dashboard without giving up the above.

## 2. Component Map

```
Scanner + Catalyst + Market Data ─> MarketContext ─> Strategy ─> OpportunityScorer
                                                                  │
                                                                  v
                                                             RiskEngine
                                                                  │
                                                                  v
FreshnessGate + PersistentCircuitBreaker + reconciliation ─> ExecutionAuthorizer
                                                                  │ (single-use grant)
                                                                  v
BrokerAdapter — ShadowBroker | AlpacaPaperBroker | AlpacaLiveBroker (refuses)
                                                                  │
                                                                  v
OrderLifecycleManager + TradeJournal
```

`app/orchestration/pipeline.py::run_pipeline()` orchestrates this path. It
requires both an `ExecutionAuthorizer` and a circuit breaker with no defaults;
a caller that omits either cannot form a valid call. A grant is issued only by
`ExecutionAuthorizer` from a complete `AuthorizationEvidence` bundle, and the
broker adapter independently checks the grant and its declared environment.

## 3. Mode Safety (spec §0, §2)

`app/common/modes.py::Mode` defines the named operating modes:

| Mode | Hypothetical trades | Order submission in the current milestone | Real money |
|---|---|---|---|
| RESEARCH | no | no | no |
| SHADOW | yes (journaled only) | no broker submission | no |
| PAPER | yes | yes, only through `AlpacaPaperBroker` | no |
| LIVE | no operational run | refused | no |

`ModeGovernor.assert_execution_allowed()` rejects a requested mode that does
not match configuration and continues to validate the LIVE configuration and
per-run acknowledgement. Those checks do **not** make LIVE executable. There
are two independent operational stops:

1. `python -m app.cli run --mode live` exits before constructing a broker.
2. `AlpacaLiveBroker.__init__` refuses because `OPERATIONALLY_ENABLED` is
   `False`.

`ShadowBroker` contains no network client, so SHADOW cannot reach a broker over
the network. PAPER and LIVE are separate adapter classes rather than variants
selected by strategy-controlled state. `MODE_REQUIRES_BROKER_ENVIRONMENT`
requires SHADOW, PAPER, and LIVE intents to match their declared broker
environments. `AuthorizationEvidence` fields default to a private missing-value
sentinel; unsupplied evidence becomes a failed authorization check, not a
skipped check. Consequently, no single boolean controlled by strategy code can
reach LIVE.

## 4. Broker & Market-Data Decision

**Broker: Alpaca paper endpoint.** `AlpacaPaperBroker` declares
`BrokerEnvironment.PAPER` and constructs its client with literal `paper=True`
at `https://paper-api.alpaca.markets`. The class accepts no argument that can
change this setting and exposes no `url_override`. The adapter confirms its
resolved URL matches the declared paper URL, but Alpaca does not provide a
server-side paper-versus-live signal; that local check cannot independently
classify credentials.

`AlpacaLiveBroker` declares `BrokerEnvironment.LIVE` but refuses construction.
It is not a usable live adapter in this milestone.

PAPER is deliberately all-or-nothing. `app/cli.py::_run_paper_cycle` exits if
it cannot construct required paper credentials, the paper broker, Alpaca market
data, the broker-backed market clock, the regime engine, or the required
catalyst provider. It never substitutes `MockProvider` or `ShadowBroker`.
Otherwise, the command could report simulated results under a PAPER heading
that claims real broker and market inputs.

The PAPER CLI supplies `DEFAULT_PAPER_UNIVERSE`, a fixed tuple of symbols, when
no scanner universe is configured because Alpaca's snapshot endpoint is a
symbol lookup rather than a market-wide scanner.

**Market data and catalyst inputs.** PAPER builds `AlpacaMarketDataProvider`,
uses the paper broker's client for `MarketSessionService`, and builds
`MarketRegimeEngine` from that provider. SEC EDGAR catalyst research requires
`SEC_EDGAR_CONTACT_EMAIL` unless the operator deliberately invokes the
PAPER-specific degraded `--no-catalyst-research` option; the command prints a
degraded banner in that explicit case.

## 5. Regulatory Note — the PDT Rule Was Retired (June 4, 2026)

The classic $25,000-equity Pattern Day Trader rule was retired by FINRA
effective June 4, 2026 and replaced by an intraday-margin-deficit framework
under FINRA Rule 4210(d)(2) ([FINRA Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10)).
The adapter does not rely on the removed `pattern_day_trader` or
`daytrade_count` response fields. `AccountSnapshot` carries optional
intraday-buying-power and margin-deficit values, while the risk engine sizes
against buying power.

## 6. Data Model and Operational State

SQLite stores the journal and a separate persistent breaker ledger. The breaker
uses `PersistentCircuitBreaker` and reads/writes SQLite state so an active trip
survives process restart. Trips and resets remain in the ledger; a reset does
not delete a trip record.

`OrderLifecycleManager` treats broker-confirmed state as authoritative.
`BLOCKING_DISCREPANCIES` includes unknown exposure cases such as unexpected or
missing positions, quantity mismatches, unexpected/missing broker orders,
unknown local submissions, order-state mismatches, and an unreachable broker.
A blocking reconciliation report trips the breaker and stops new entries until
an operator resolves the discrepancy. Protective actions are not automatically
blocked by a breaker trip.

## 7. Known Gaps / Explicit Limitations

- **No real Alpaca verification has occurred.** The PAPER path has not been run
  against a real Alpaca account, real Alpaca market data, or a real broker
  clock. Tests use mocks and fixtures only. Built and tested are not verified.
- **LIVE execution is intentionally disabled.** The disabled live class makes
  the boundary visible, but neither the CLI nor the class permits operational
  live trading.
- **PAPER environment verification is limited.** Alpaca exposes no server-side
  signal proving that a credential/session is paper or live. The code verifies
  its own paper URL selection and environment declarations; it cannot prove a
  credential's classification from the service.
- **Intraday margin-deficit monitoring is not yet a risk-engine gate.** It is
  required before any reviewed future enablement of margin or short-selling.
- **IBKR adapter is not built.**
- **Extended-hours validation is implemented but limited to the adapter's
  supported request forms.** The adapter rejects incompatible extended-hours
  requests and rejects bracket/OCO combinations with extended hours; operational
  behavior still requires broker-side verification before reliance.
- **No scheduler loop is built.** `run` performs one cycle.

## 8. Sources

- [Alpaca Paper Trading docs](https://docs.alpaca.markets/docs/paper-trading)
- [Alpaca Orders at Alpaca docs](https://docs.alpaca.markets/docs/orders-at-alpaca)
- [Alpaca Market Data plans](https://alpaca.markets/data)
- [FINRA Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10)
- [SEC EDGAR access policy](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
