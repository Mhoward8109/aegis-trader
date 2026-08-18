# Aegis Trader — Architecture

Status: Milestone 1 complete (scanner + catalyst engine + strategy scoring +
risk engine, running end-to-end in SHADOW mode against an offline mock data
source). Not connected to any live credentials. Not tested against real
market data. **Built ≠ tested ≠ verified ≠ safe for live trading** — see
`docs/SAFETY.md`.

## 1. Why Python, not JavaScript/TypeScript

The rest of the user's stack is Node/TS, but this project is Python. Reasons,
documented explicitly because it's a deliberate deviation:

- `pandas`/`numpy` are the de facto standard for the vectorized bar/indicator
  math this system runs constantly (VWAP, ATR, RSI, opening range, etc.) —
  reimplementing that layer in JS would be slower to build and slower to run.
- `alpaca-py` is Alpaca's official, actively maintained SDK; the Node
  equivalent (`@alpacahq/alpaca-trade-api`) is thinner and less current.
- Backtesting/quant tooling (pandas-based walk-forward splits, vectorized
  slippage models) is far more mature in the Python ecosystem.
- FastAPI gives the same "fast to iterate" dashboard experience Node/Express
  would, without giving up the above.

If a future phase needs a Node-based UI layer, the FastAPI backend already
exposes a plain JSON API (`/api/snapshot`) that any frontend can consume —
Python only owns the trading engine, not necessarily the eventual UI.

## 2. Component Map

```
Scanner (app/scanner) ──┐
                         ├─> MarketContext ──> Strategy (app/strategy) ──> Setup
Catalyst (app/catalyst) ─┘                                                   │
                                                                              v
                                                            OpportunityScorer (app/strategy/scoring.py)
                                                                              │
                                                                              v
                                                              RiskEngine (app/risk/engine.py) <── CircuitBreaker
                                                                              │ (approved)
                                                                              v
                                                     BrokerAdapter (app/broker) — Shadow | Alpaca(paper) | Alpaca(live, gated)
                                                                              │
                                                                              v
                                                        OrderStateMachine (app/execution) + TradeJournal (app/journal)
```

Orchestrated end-to-end by `app/orchestration/pipeline.py::run_pipeline()`,
invoked by `app/cli.py`. The dashboard (`app/dashboard/server.py`) reads a
snapshot of the same pipeline for display; it does not have its own
parallel logic path.

Every arrow above is a plain Python interface (`abc.ABC` base classes) so
any box can be swapped without touching its neighbors — e.g. replacing
`MockProvider` with a real Alpaca market-data adapter never touches
`Strategy`, `RiskEngine`, or `TradeJournal`.

## 3. Mode Safety (spec §0, §2)

`app/common/modes.py::Mode` is the single source of truth for what the
system is allowed to do:

| Mode | Hypothetical trades | Order submission | Real money |
|---|---|---|---|
| RESEARCH | no | no | no |
| SHADOW | yes (journaled only) | no | no |
| PAPER | yes | yes, broker's paper endpoint only | no |
| LIVE | yes | yes | **yes** |

`ModeGovernor.assert_execution_allowed()` is the only gate that can move
between modes at runtime, and reaching LIVE additionally requires **both**:
1. `mode: LIVE` set in `config/local.yaml` (git-ignored, never `default.yaml`)
2. the CLI flag `--i-understand-this-is-live-trading`

This is enforced structurally, not just by convention: `ShadowBroker`
(`app/broker/shadow_adapter.py`) contains no network client at all — it is
*impossible*, not just discouraged, for it to reach a broker over the wire
(verified by `tests/test_shadow_broker.py::test_shadow_broker_has_no_http_client_attribute`).
`AlpacaBroker`'s live base URL is only reachable if its constructor receives
`allow_live=True`, which only `app/cli.py` ever sets, and only after
`ModeGovernor` has already approved LIVE.

## 4. Broker & Market-Data Decision

Two research passes were run before writing broker/market-data code
(full reports: `research_broker_apis.md`, `research_data_infra.md` in the
project root).

**Broker: Alpaca (Trading API, paper endpoint first).**
- Free paper account via email signup, $100k simulated balance, no live
  account required — versus IBKR, which requires a funded, approved live
  account *before* any paper trading or API access at all
  ([Alpaca Paper Trading docs](https://docs.alpaca.markets/docs/paper-trading);
  [IBKR Paper Trading setup](https://www.interactivebrokers.com/campus/trading-lessons/how-to-open-an-ibkr-paper-trading-account/)).
- Pure hosted REST/WebSocket API — no locally-run gateway process to keep
  alive, unlike IBKR's TWS/Client Portal Gateway requirement
  ([IBKR Gateway auth lesson](https://www.interactivebrokers.com/campus/trading-lessons/launching-and-authenticating-the-gateway/)).
- Base URLs are hard-separated in code: `https://paper-api.alpaca.markets`
  (paper) vs. `https://api.alpaca.markets` (live) — see
  `app/broker/alpaca_adapter.py`. The adapter never constructs a live
  client unless `allow_live=True` is explicitly passed.
- Order types confirmed available: market, limit, stop, stop-limit, bracket,
  OCO, OTO, trailing-stop, plus MOO/LOO/MOC/LOC auction orders
  ([Alpaca Orders docs](https://docs.alpaca.markets/docs/orders-at-alpaca)).
  Bracket/OCO order-class support is a near-term follow-up (currently the
  adapter implements the six base order types; see "Known Gaps" below).
- Rate limits: 200 req/min (Trading API), 200 req/min market data on the
  free tier / up to 10,000 req/min on Algo Trader Plus ($99/mo)
  ([Alpaca Market Data plans](https://alpaca.markets/data)).
- IBKR remains a documented second-adapter candidate for later (broader
  order-type/algo catalog, cheaper marginal market-data cost) but is not
  built in this milestone — see `app/broker/base.py`'s abstract interface,
  which any second adapter must implement without touching strategy code.

**Market data: Alpaca, `feed=iex` (free) by default, `feed=sip` config-ready.**
- The free IEX-only feed is directionally useful but not NBBO-complete —
  `config/default.yaml`'s `market_data.feed` key exists specifically so this
  can be upgraded to `sip` (Alpaca's "All US Exchanges" $99/mo tier) without
  a code change, once budget allows
  ([Alpaca Market Data plans](https://alpaca.markets/data)).
- **IEX Cloud is discontinued** (shut down August 31, 2024) and does not
  appear anywhere in this codebase — it is a different product from the
  still-operating IEX Exchange feed Alpaca's free tier uses.

**News/catalysts: SEC EDGAR now (free, keyless), Benzinga later.**
- `app/catalyst/engine.py::SecEdgarProvider` is live today — free, no key,
  rate-limited to the SEC's documented ~10 req/sec ceiling, requires only a
  `SEC_EDGAR_CONTACT_EMAIL` env var for a compliant User-Agent header
  ([SEC EDGAR access policy](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)).
- Benzinga is the recommended second provider once real-time news (not just
  filings) is needed — free AWS Marketplace tier to start, ~$99–166/mo
  beyond that; beats StockTwits (noisy), MT Newswires (~$4,000/mo
  enterprise), and NewsAPI.org (cheapest usable tier $449/mo, free tier is
  24h-delayed and non-commercial)
  ([Benzinga APIs](https://www.benzinga.com/apis/)).
- The catalyst engine is provider-pluggable (`NewsProvider` ABC) precisely
  so Benzinga can be added as a second provider without touching scoring
  or strategy code — see `app/catalyst/engine.py::CatalystEngine`.

## 5. Regulatory Note — the PDT Rule Was Retired (June 4, 2026)

This matters architecturally, not just as trivia: the classic
$25,000-equity Pattern Day Trader rule that most day-trading platform
tutorials assume is still in force **was retired by FINRA effective June 4,
2026**, replaced by a new "intraday margin deficit" framework under FINRA
Rule 4210(d)(2) ([FINRA Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10)).
Alpaca has already frozen and scheduled removal (by July 6, 2026 — already
past) of the legacy `pattern_day_trader`/`daytrade_count` fields
([Alpaca PDT retirement announcement](https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/)).

Consequently, this codebase **does not** implement day-trade counting or a
static $25k-equity gate. `AccountSnapshot` (`app/broker/base.py`) instead
carries optional `intraday_buying_power`/`margin_deficit` fields, read
defensively via `getattr(...)` from whatever the installed `alpaca-py`
version currently exposes, with `buying_power` as the always-available
fallback the risk engine actually sizes against. If/when Alpaca's SDK
formally exposes the new Intraday Margin fields under stable names, wire
them into `RiskEngine.evaluate()` as an additional hard gate — this is
flagged as a near-term follow-up, not yet implemented (this milestone's
default long-only/cash-first posture doesn't strictly require it yet, but
short-selling or leveraged sizing must not go live without it).

Cash-account "free-riding" rules (Reg T + FINRA Rule 4210(f)(9)) are
unaffected by the PDT retirement and still apply if a cash (non-margin)
account is used — see `research_broker_apis.md` §3.3 for detail.

## 6. Data Model

SQLite via SQLAlchemy (`app/common/db.py`) — file-based, zero-ops for a
single-operator system, explicitly documented as swappable to Postgres by
changing the connection string alone (no ORM-specific SQLite features are
used). Every candidate the scanner/strategy layer ever produces is written
to the `candidates` table — approved *and* rejected — because rejected
setups are training signal too (spec §19). Orders are append-only via
`OrderEvent` rows so the full state-transition history survives even if a
process crashes mid-trade.

## 7. Known Gaps / Explicit Follow-ups (not silently deferred)

- **Bracket/OCO orders** are not yet implemented in `AlpacaBroker` — only
  the six base order types (market/limit/stop/stop-limit/trailing-stop as
  standalone orders). Needed before any strategy relies on broker-native
  stop-loss attachment rather than software-managed exits.
- **Intraday margin deficit monitoring** (see §5) is not yet a risk-engine
  gate. Required before enabling margin/short-selling in PAPER or LIVE.
- **IBKR adapter** is not built. `BrokerAdapter` is ready for it.
- **Real market-data adapter** (`feed=iex` live Alpaca data) is not yet
  wired into the scanner — `app/scanner/mock_provider.py` is the only
  provider today, by design, so this milestone runs with zero credentials.
  Wiring the real Alpaca market-data adapter is the natural next step
  before Phase 9 (paper broker integration).
- **Extended-hours/overnight order constraints** (Alpaca: limit+day/gtc
  only, no brackets; IBKR: limit/adaptive-limit only, no GTC) are
  documented in `research_broker_apis.md` §1.4/§2.5 but not yet enforced
  in code — must be added to `AlpacaBroker.submit_order()` validation
  before enabling extended-hours trading.

## 8. Sources

- [Alpaca Paper Trading docs](https://docs.alpaca.markets/docs/paper-trading)
- [Alpaca Orders at Alpaca docs](https://docs.alpaca.markets/docs/orders-at-alpaca)
- [Alpaca Market Data plans](https://alpaca.markets/data)
- [Alpaca FINRA PDT retirement blog](https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/)
- [FINRA Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10)
- [IBKR Trading API Solutions](https://www.interactivebrokers.com/en/trading/ib-api.php)
- [IBKR Paper Trading Account setup](https://www.interactivebrokers.com/campus/trading-lessons/how-to-open-an-ibkr-paper-trading-account/)
- [SEC EDGAR access policy](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- [Benzinga APIs](https://www.benzinga.com/apis/)
- Full detail: `research_broker_apis.md`, `research_data_infra.md` (project root)
