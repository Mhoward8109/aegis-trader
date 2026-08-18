# PAPER Verification Runbook

**Purpose:** first connection of Aegis Trader to an Alpaca **PAPER** account. This is a controlled verification procedure, not permission to automate, scale, or trade LIVE.

> **STOP RULE:** Do not continue after any failed check, unexpected result, timeout, discrepancy, stale timestamp, or uncertainty about whether an order reached Alpaca. Cancel/flatten as applicable, trip the breaker, and investigate before continuing.

> **Alpaca environment limitation:** Alpaca exposes **no server-side paper-vs-live signal**. `TradingClient(paper=True)` is client-side URL selection only. `AlpacaPaperBroker` hard-codes `paper=True` and expects `https://paper-api.alpaca.markets`; the live URL is `https://api.alpaca.markets`. The market-data URL is shared by PAPER and LIVE. Consequently, a displayed PAPER URL confirms this program's client configuration, not Alpaca's classification of the key. The compensating controls are separate PAPER-named credentials, the separate `AlpacaPaperBroker` class with `BrokerEnvironment.PAPER`, exact base-URL verification, execution-grant environment matching, and the independent refusal to construct `AlpacaLiveBroker`.

## 1. Prerequisites

- [ ] An Alpaca **PAPER** account exists. Do not use a LIVE account or LIVE API keys.
- [ ] PAPER API credentials have been created in the Alpaca PAPER environment.
- [ ] An email address monitored by the operator is available for SEC EDGAR identification.
- [ ] Python 3.14 is the active interpreter.
- [ ] Before every pytest or CLI command in this sandbox, set the required PATH:

```bash
cd /home/user/workspace/aegis-trader
export PATH="$PATH:/home/user/.local/bin"
```

- [ ] Dependencies are installed:

```bash
pip install -r requirements.txt
```

- [ ] The operator understands the data entitlement before testing: Alpaca Basic/free access is **IEX-only** and limited to **200 requests/minute**. IEX volume is a venue subset, **not consolidated-tape volume**. SIP REST data is 15 minutes delayed. Treat IEX-derived volume, dollar volume, ADV, RVOL, and premarket volume measures as degraded—not as consolidated market facts.
- [ ] The operator will use `buying_power` for account capacity. Do **not** branch on `pattern_day_trader`, `daytrade_count`, or `daytrading_buying_power`: Alpaca removed those API fields on 2026-07-06 and they are always `None`.
- [ ] The operator understands order constraints that will be enforced by the adapter: BRACKET and OCO orders require **both** `take_profit` and `stop_loss`; BRACKET orders are incompatible with `extended_hours=true` (Alpaca rejects them); extended-hours orders must be `limit` type with `day` or `gtc` time-in-force.

## 2. Credential setup

### Required environment variables

Set all three variables in the process environment that will run the checks:

- `ALPACA_PAPER_API_KEY_ID`
- `ALPACA_PAPER_API_SECRET_KEY`
- `SEC_EDGAR_CONTACT_EMAIL`

The unprefixed `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` pair is accepted as a fallback by the PAPER broker, market-data provider, and market-session client. Do **not** rely on it for this first connection: the PAPER-prefixed names are strongly preferred so a PAPER key cannot be confused with a LIVE key.

- [ ] Never write keys into source code, committed YAML, documentation, tickets, terminal logs, screenshots, or shell-history files.
- [ ] Never place keys in `config/default.yaml` or any other committed configuration.
- [ ] `config/local.yaml` and `.env` are gitignored. If used locally, they must **never** be committed; check this explicitly before proceeding.

### Avoiding shell history

Preferred: enter sensitive values through hidden prompts. The values are not echoed.

```bash
read -rs -p 'Alpaca PAPER key ID: ' ALPACA_PAPER_API_KEY_ID; echo
read -rs -p 'Alpaca PAPER secret: ' ALPACA_PAPER_API_SECRET_KEY; echo
read -r -p 'SEC EDGAR contact email: ' SEC_EDGAR_CONTACT_EMAIL
export ALPACA_PAPER_API_KEY_ID ALPACA_PAPER_API_SECRET_KEY SEC_EDGAR_CONTACT_EMAIL
```

Alternative: before manually entering an export assignment, set `HISTCONTROL=ignorespace` and begin that export line with a literal leading space. Do not paste the assignment, or any credential value, into this runbook, a terminal recording, or a support log.

```bash
export HISTCONTROL=ignorespace
```

Confirm **presence only**; this command must never display values, prefixes, or lengths:

```bash
python -m app.cli status
```

Expected: `ALPACA_PAPER_API_KEY_ID`, `ALPACA_PAPER_API_SECRET_KEY`, and `SEC_EDGAR_CONTACT_EMAIL` each show `SET`. If a required item is `not set`, stop and correct the process environment; do not substitute a value in a file.

### Set the operating mode deliberately

PAPER mode must be selected in the gitignored local configuration, not by a CLI flag or an `AEGIS__mode` environment override. Make the local configuration's effective `mode` equal `PAPER`, preserve the risk limits, and later confirm it with `status`. The `--config` overlay exists, but it is not permitted to set `mode`.

- [ ] `config/local.yaml` is local-only and remains uncommitted.
- [ ] No `AEGIS__mode` environment variable is set.

## 3. Preflight checklist

Run these checks in order. All snippets are read-only: none submits, cancels, changes, or closes an order.

### 1. Clean git tree

```bash
git status --short
git diff --check
```

Expected: `git status --short` prints nothing and `git diff --check` prints nothing. On failure: stop. Preserve and review the existing work; do not test PAPER against an unreviewed tree.

- [ ] Clean tree and whitespace check passed.

### 2. Full test suite passes

```bash
export PATH="$PATH:/home/user/.local/bin"
python -m pytest tests/ -q
```

Expected: pytest exits zero with all tests passing. On failure: stop; do not use PAPER to diagnose a failing local test suite.

- [ ] Full test suite passed.

### 3. Confirm configured mode is PAPER

```bash
python -m app.cli status
```

Expected: the banner says `MODE: PAPER`, and `Mode declared by:` identifies the expected local configuration source. On failure, including a mode other than PAPER: stop and correct `config/local.yaml`; never attempt to override the mode with `AEGIS__mode` or `--config`.

- [ ] Mode is PAPER and credential presence is correct.

### 4. Confirm LIVE remains unavailable

```bash
python - <<'PY'
from app.broker.alpaca_adapter import AlpacaLiveBroker

assert AlpacaLiveBroker.OPERATIONALLY_ENABLED is False
try:
    AlpacaLiveBroker()
except Exception as exc:
    print(type(exc).__name__)
    print(str(exc))
else:
    raise SystemExit("FAIL: AlpacaLiveBroker constructed")
PY
```

Expected: construction raises a broker error saying LIVE is operationally disabled. On failure: stop immediately. Do not continue with PAPER until the LIVE path has been investigated and restored to refusal.

- [ ] `AlpacaLiveBroker` refused construction.

### 5. Confirm the broker class and PAPER endpoint selection

```bash
python - <<'PY'
from app.broker.alpaca_adapter import AlpacaPaperBroker, PAPER_BASE_URL
from app.execution.authorization import BrokerEnvironment

broker = AlpacaPaperBroker()
assert type(broker).__name__ == "AlpacaPaperBroker"
assert broker.environment is BrokerEnvironment.PAPER
assert broker.base_url_in_use == PAPER_BASE_URL
record = broker.verify_environment()
assert record["resolved_base_url"] == PAPER_BASE_URL
assert record["server_side_environment_verification_available"] is False
print("adapter=AlpacaPaperBroker")
print("environment=PAPER")
print("base_url=", record["resolved_base_url"])
print("server_side_verification=unavailable")
PY
```

Expected: `AlpacaPaperBroker`, `PAPER`, and `https://paper-api.alpaca.markets`. The last line must say server-side verification is unavailable; that limitation is expected, not a pass to ignore controls. On failure: stop. Recheck variable names and code version; do not proceed on a claimed environment.

- [ ] PAPER adapter class, declared environment, and client-selected base URL verified.

### 6. Circuit breaker is healthy

```bash
python -m app.cli breaker status
```

Expected: `Circuit breaker: CLEAR`. On failure or `TRIPPED`: do not run an entry. Read the trip history, resolve the root cause, then use the controlled reset procedure in section 6 only if it is safe.

- [ ] Persistent breaker is clear.

### 7. Market clock and calendar are reachable

```bash
python - <<'PY'
from app.broker.alpaca_adapter import AlpacaPaperBroker
from app.marketdata.session import MarketSessionService, UNKNOWN

state = MarketSessionService(client=AlpacaPaperBroker().trading_client).current_session()
print("session=", state.session)
print("timestamp=", state.timestamp)
print("scheduled_open=", state.scheduled_open)
print("scheduled_close=", state.scheduled_close)
print("reason=", state.reason)
assert state.session != UNKNOWN
PY
```

Expected: `session` is one of the plain uppercase strings `CLOSED`, `PREMARKET`, `REGULAR`, `AFTER_HOURS`, `HOLIDAY`, or `EARLY_CLOSE`, never `UNKNOWN`. `current_session()` takes no arguments and `state.session` is already a string—do not use `.value`. On failure or `UNKNOWN`: stop; broker clock/calendar evidence is unavailable.

- [ ] Authoritative market-session lookup succeeded.

### 8. Account query succeeds

```bash
python - <<'PY'
from app.broker.alpaca_adapter import AlpacaPaperBroker

account = AlpacaPaperBroker().get_account()
print("equity=", account.equity)
print("buying_power=", account.buying_power)
print("cash=", account.cash)
print("currency=", account.currency)
print("timestamp=", account.timestamp)
assert account.buying_power >= 0
PY
```

Expected: a current account snapshot with numeric `equity`, `buying_power`, and `cash`. On failure: stop; credentials may be invalid or the broker unavailable. Do not replace this with any PDT/day-trade field.

- [ ] Account read and buying-power read succeeded.

### 9. Market data succeeds with API timestamps

```bash
python - <<'PY'
from datetime import datetime, timedelta, timezone
from app.broker.alpaca_adapter import AlpacaPaperBroker
from app.marketdata.alpaca_provider import AlpacaMarketDataProvider, FieldAvailability

provider = AlpacaMarketDataProvider(symbols=("AAPL",))
snapshots = provider.get_snapshots(("AAPL",))
assert "AAPL" in snapshots
quote = AlpacaPaperBroker().get_quote("AAPL")
assert quote.timestamp is not None
end = datetime.now(timezone.utc)
bars = provider.get_bars("AAPL", "1min", end - timedelta(minutes=10), end)
print("quote_timestamp=", quote.timestamp)
print("bar_count=", len(bars))
print("bar_data_timestamp=", bars.attrs["data_timestamp"])
print("availability=", {k: v.value for k, v in provider.field_availability().items()})
assert provider.field_availability()["current_volume"] is FieldAvailability.DEGRADED
PY
```

Expected: a timestamped quote, a successful snapshot response, and field availability showing IEX volume-derived fields as `DEGRADED`. A minute-bar response can legitimately be empty outside the requested active interval; in that case, repeat this check during a known session with a recent completed interval before attempting an entry. On exception, missing quote timestamp, malformed response, or unavailable required data: stop.

- [ ] Snapshot and timestamped quote succeeded.
- [ ] Bar request succeeded; a suitable timestamped bar interval has been observed.
- [ ] IEX volume limitations acknowledged.

### 10. SEC EDGAR User-Agent is configured

```bash
python - <<'PY'
from app.catalyst.sec_edgar import SecEdgarFilingProvider

provider = SecEdgarFilingProvider()
client = provider._client_for_request()
user_agent = client.headers["User-Agent"]
assert user_agent.startswith("AegisTrader/1.0 (SEC EDGAR research; contact: ")
assert "contact: " in user_agent
print("SEC EDGAR User-Agent is configured without printing the contact value")
PY
```

Expected: the confirmation line only. `SecEdgarFilingProvider` reads `SEC_EDGAR_CONTACT_EMAIL` and embeds it in the required User-Agent; if it is absent or invalid it raises `MissingSecEdgarContactEmail` before making a request. On failure: stop and correct the environment variable; do not use a placeholder email.

- [ ] SEC EDGAR contact-bearing User-Agent configured.

### 11. Reconciliation is clean

```bash
python - <<'PY'
from pathlib import Path
from sqlalchemy.orm import sessionmaker
from app.broker.alpaca_adapter import AlpacaPaperBroker
from app.common.db import make_engine
from app.config.loader import load_config
from app.execution.lifecycle import OrderLifecycleManager
from app.journal.store import TradeJournal
from app.orchestration.pipeline import _local_positions

cfg = load_config()
journal_path = Path(cfg.get("logging.dir") or "data") / "journal.db"
assert journal_path.exists(), f"journal database does not exist: {journal_path}"
journal = TradeJournal(sessionmaker(bind=make_engine(str(journal_path)), future=True)())
report = OrderLifecycleManager(AlpacaPaperBroker(), journal).reconcile(
    journal.open_orders(), _local_positions(journal)
)
print(report.detail)
print("clean=", report.clean)
print("blocks_trading=", report.blocks_trading)
assert report.clean
assert not report.blocks_trading
PY
```

Expected: `clean=True` and `blocks_trading=False`. On failure: stop. Treat every member of `BLOCKING_DISCREPANCIES` as an entry halt; do not auto-close an unexpected broker position merely to make the report clean.

- [ ] Local journal, broker positions, and broker open orders reconcile cleanly.

### Health endpoint (observability only)

The FastAPI health route is exactly `GET /health`. Start the dashboard with the supported CLI command, then read the route locally:

```bash
python -m app.cli dashboard --port 8080
```

In a second terminal:

```bash
curl http://127.0.0.1:8080/health
```

Expected: a JSON health snapshot with an overall `status`, explicit availability/value/reason records, `blocking_reasons`, reconciliation state, and circuit-breaker state. The current dashboard deliberately uses an offline `MockProvider` and no broker adapter, so its `/health` response is expected to be `BLOCKED`/degraded for PAPER entry evidence. It is an observability-route verification only; it is **not** evidence that PAPER execution is healthy and must not be used to authorize an order.

## 4. First external verification — staged

**Stage rule:** A stage passes only when every stated pass criterion is met. A failure, uncertainty, or abort criterion ends the session at section 6. Do not skip forward.

### Stage A — Connectivity only. NO ORDER.

**Goal:** prove the first real broker, market-data, clock, and EDGAR reads work without placing an order.

**Precise steps**

- [ ] Confirm the breaker is clear with `python -m app.cli breaker status`.
- [ ] Run this read-only probe exactly once. It has no call to `submit_order`, `ExecutionEngine.submit`, `cancel_order`, `close_position`, or `close_all_positions`.

```bash
python - <<'PY'
from datetime import datetime, timedelta, timezone
from app.broker.alpaca_adapter import AlpacaPaperBroker
from app.catalyst.sec_edgar import SecEdgarFilingProvider
from app.marketdata.alpaca_provider import AlpacaMarketDataProvider
from app.marketdata.session import MarketSessionService

broker = AlpacaPaperBroker()
account = broker.get_account()
session = MarketSessionService(client=broker.trading_client).current_session()
provider = AlpacaMarketDataProvider(symbols=("AAPL",))
quote = broker.get_quote("AAPL")
now = datetime.now(timezone.utc)
bars = provider.get_bars("AAPL", "1min", now - timedelta(minutes=10), now)
edgar = SecEdgarFilingProvider().research("AAPL", now - timedelta(days=2))

print("account_timestamp=", account.timestamp)
print("buying_power=", account.buying_power)
print("session=", session.session)
print("quote_timestamp=", quote.timestamp)
print("bar_data_timestamp=", bars.attrs["data_timestamp"])
print("edgar_result=", edgar.reason)
print("NO ORDER WAS PLACED")
PY
```

**Observe**

- [ ] Account, buying power, session string, and quote timestamp print without an exception.
- [ ] The EDGAR result is either verified filings or an explicit verified-negative result; an explicit “no verified catalyst found” is acceptable, but a timeout/configuration failure is not.
- [ ] The final line says `NO ORDER WAS PLACED`.

**Pass criteria**

- [ ] All reads return without error.
- [ ] Session is not `UNKNOWN`.
- [ ] Quote has an API timestamp.
- [ ] EDGAR request completes through the contact-bearing User-Agent.
- [ ] No broker order ID was created because no order path was called.

**Abort criteria**

- [ ] Any exception, `UNKNOWN` session, missing timestamp, EDGAR configuration error, unexpected broker state, or evidence of an order.

**Rollback**

- [ ] No order should exist. Run `python -m app.cli breaker status`, then the reconciliation check from preflight item 11. If either indicates an order or position, immediately use section 6.

### Stage B — Paper account read/write probe

**Goal:** submit the smallest safe PAPER order the architecture supports, then establish broker-confirmed state through an independent read and verify broker-side protection.

**Architecture constraint — do not bypass it:** there is currently **no supported CLI subcommand or flag that creates one bounded manual PAPER order**. The supported `python -m app.cli run --mode paper` command runs the full scan-to-pipeline cycle over its configured universe and has no `--symbol`, `--quantity`, `--one-order`, or dry-run flag. The pipeline can iterate multiple candidates. Therefore this runbook does **not** authorize constructing a private grant, hand-writing authorization evidence, or calling the broker adapter directly to force a “one share” order. That would bypass the normal strategy/risk/pipeline controls and would not be a valid verification of the architecture.

**Precise steps**

- [ ] **BLOCKED until a reviewed, supported single-order PAPER probe exists.** Record this as a failed acceptance gate; do not substitute an invented command.
- [ ] When such a capability is added and reviewed, use only a highly liquid symbol, minimal whole-share quantity, normal session rules, current quote/bar/account freshness, and broker-native protection.
- [ ] The future probe must record its broker order ID, then call `get_order_status(broker_order_id)` independently. A submit response is an acknowledgement, not a fill.
- [ ] If unfilled, cancel it using the supported protective cancellation path. If filled or partially filled, verify the broker accepted a protective exit before treating the stage as passed; if protection cannot be confirmed, flatten.

**Observe**

- [ ] A future supported probe must show a unique broker order ID.
- [ ] Its independent broker read must show status, `filled_qty`, and `filled_avg_price` where applicable.
- [ ] A BRACKET/OCO protective structure must contain both take-profit and stop-loss; no extended-hours bracket is permitted.

**Pass criteria**

- [ ] Not currently satisfiable with the existing supported CLI surface.
- [ ] This stage becomes passable only after a reviewed single-order procedure can prove exactly one controlled PAPER order, independent broker-status confirmation, cancellation when unfilled, and accepted protection when filled.

**Abort criteria**

- [ ] Any temptation to use a fabricated CLI flag, a direct `submit_order` call with manually created evidence, a non-liquid symbol, an order outside allowed session rules, a submission timeout, or missing protection.

**Rollback**

- [ ] If no order was sent, record the capability gap and stop.
- [ ] If an order or position exists for any reason, follow section 6 immediately. Do not retry a submission whose outcome is uncertain.

### Stage C — Pipeline supervised PAPER trade

**Goal:** observe one full real-data PAPER pipeline cycle and verify every system gate and journal record.

**Precise steps**

- [ ] Stage A must pass. Stage B must be resolved by a reviewed supported capability before any order-capable pipeline test.
- [ ] Before starting, reduce exposure using only reviewed local configuration values and confirm the effective configuration with `python -m app.cli status`. The configuration must still say PAPER.
- [ ] Confirm the market session is explicitly allowed by the current configuration. Default allowed sessions are `REGULAR`; `PREMARKET`, `AFTER_HOURS`, `CLOSED`, `HOLIDAY`, `EARLY_CLOSE`, and `UNKNOWN` are not an entry authorization unless explicitly configured and compatible with order constraints.
- [ ] Start the single supported cycle while the operator watches its entire output:

```bash
python -m app.cli run --mode paper
```

- [ ] Do not use `--no-catalyst-research` for this verification; that flag deliberately disables SEC research and produces a degraded run.

**Observe**

- [ ] Startup states `PAPER MODE — Alpaca paper endpoint` and reports `AlpacaPaperBroker`'s PAPER endpoint.
- [ ] The run is halted before scanning if the breaker, session, broker connection, reconciliation, or account-state gate fails.
- [ ] For each candidate, inspect the journal evidence for freshness, risk, authorization, broker acknowledgement, independent broker confirmation, and protective-exit outcome.
- [ ] If a candidate reaches submission, it must transition through `SUBMITTED`; only the later independent broker query may establish `FILLED` or `PARTIALLY_FILLED`.

**Pass criteria**

- [ ] All system gates are present and pass, or the run fails closed before an entry.
- [ ] No `MockProvider` or `ShadowBroker` appears anywhere in a PAPER execution path.
- [ ] Every considered candidate is journaled, including rejections and data faults.
- [ ] Any submitted order has a broker order ID, fresh broker-status reconciliation, and a recorded protection result if filled/partially filled.

**Abort criteria**

- [ ] `RUN HALTED`, stale/missing data, a breaker trip, reconciliation discrepancy, broker disconnect, `submission_unknown`, an unknown order state, a missing broker order ID, or a protection failure.
- [ ] More orders than the operator has explicitly bounded and reviewed. The present CLI has no per-run order-count flag; stop rather than rely on observation alone.

**Rollback**

- [ ] If no order exists, trip the breaker if the failure signals unsafe data, reconciliation, broker, or submission uncertainty; otherwise document the refusal.
- [ ] If an order/position exists, use section 6. Reconcile before any restart or retry.

### Stage D — Reconciliation / restart

**Goal:** prove that restart does not erase local/broker truth and that a blocking discrepancy halts new entries.

**Precise steps**

- [ ] Stage C must have produced a known PAPER order or position under a reviewed supported procedure.
- [ ] Record the current broker order IDs, position quantities, and `python -m app.cli breaker status` output.
- [ ] Stop the foreground cycle only after the current command returns; `run` is a one-cycle command, not an unattended scheduler.
- [ ] Restart by running the same supervised command only after a clean, explicit reconciliation:

```bash
python -m app.cli run --mode paper
```

- [ ] Run preflight reconciliation item 11 before and after the restart attempt.

**Observe**

- [ ] A filled/partially filled local order and broker position remain visible after restart.
- [ ] A local open/in-flight order that is absent from broker open orders is classified, not guessed away.
- [ ] Any blocking discrepancy (`UNEXPECTED_BROKER_POSITION`, `MISSING_BROKER_POSITION`, `POSITION_QTY_MISMATCH`, `UNEXPECTED_BROKER_ORDER`, `MISSING_BROKER_ORDER`, `ORDER_STATE_MISMATCH`, `BROKER_UNREACHABLE`, or `UNSUBMITTED_LOCAL_ORDER`) halts new entries and trips the persistent breaker through the pipeline.

**Pass criteria**

- [ ] Clean state reconciles cleanly across restart.
- [ ] A deliberately observed real discrepancy blocks new entries; do not create a fake discrepancy solely for this runbook.
- [ ] Protective exits and cancellations remain possible while the breaker is tripped.

**Abort criteria**

- [ ] Restart appears to clear a breaker, loses a known order/position, permits a new entry with a blocking discrepancy, or labels an unknown submission as safe.

**Rollback**

- [ ] Leave the breaker tripped when state is uncertain.
- [ ] Cancel/flatten only confirmed PAPER orders/positions under section 6, then reconcile cleanly before any reset.

### Stage E — Limited supervised session

**Goal:** observe a short, deliberate PAPER-only operating window after Stages A–D pass.

**Precise steps**

- [ ] Limit the session to one operator-attended cycle at a time using only `python -m app.cli run --mode paper`.
- [ ] Before each cycle, check `status`, breaker status, market session, and reconciliation.
- [ ] After each cycle, inspect the journal and broker state before starting another.
- [ ] Keep size and risk at the reviewed minimum. Do not add symbols, strategies, extended-hours behavior, or different data feeds during this stage.

**Observe**

- [ ] Gates fail closed when data is stale, the broker is unavailable, the session is disallowed, or reconciliation is not clean.
- [ ] Journal records reconstruct each decision and broker lifecycle event.

**Pass criteria**

- [ ] Every cycle is supervised end-to-end and leaves reconciliation clean.
- [ ] No LIVE request, credential, endpoint, or adapter is involved.

**Abort criteria**

- [ ] Any operational uncertainty, unexplained reject, stale-data trip, unresolved discrepancy, or lost visibility into an order/position.

**Rollback**

- [ ] Stop initiating new cycles; use section 6 if any order or position is open.

> **Unattended PAPER automation is NOT authorized yet.** Stage E is limited to operator-attended, one-cycle invocations.

## 5. Acceptance gates

Do not mark a box complete based on a submit acknowledgement alone. A submitted order is not a filled order until a fresh independent broker-status read confirms it.

### Broker

| Gate | Pass | Fail / required action |
|---|---|---|
| PAPER credentials authenticate | - [ ] | - [ ] Any authentication error: stop; correct environment only. |
| Account reads | - [ ] | - [ ] Query error: stop. |
| Buying power reads | - [ ] | - [ ] Missing/invalid `buying_power`: stop; do not use removed PDT fields. |
| Positions read | - [ ] | - [ ] Query error or unknown position: reconcile and halt. |
| Order submission succeeds | - [ ] | - [ ] Currently blocked pending a reviewed one-order probe; no invented command. |
| Order-status reconciliation succeeds | - [ ] | - [ ] Uncertain/mismatched state: breaker trip and reconcile. |
| Cancel succeeds | - [ ] | - [ ] Stop and reconcile broker orders. |
| Protective exit accepted | - [ ] | - [ ] Flatten if protection cannot be verified. |

### Market data

| Gate | Pass | Fail / required action |
|---|---|---|
| Quote timestamps are valid | - [ ] | - [ ] Missing/stale/future timestamp: block entry. |
| Bar timestamps are valid | - [ ] | - [ ] Missing/stale/future timestamp: block entry. |
| Freshness gates work against real responses | - [ ] | - [ ] Evidence absent or stale: fail closed and inspect breaker. |
| No mock provider appears anywhere in PAPER execution | - [ ] | - [ ] Stop; a PAPER run must never fall back to synthetic data. |

### Catalyst/SEC

| Gate | Pass | Fail / required action |
|---|---|---|
| EDGAR request succeeds | - [ ] | - [ ] Contact/header/request failure: stop PAPER verification. |
| Timestamps and sources captured | - [ ] | - [ ] Missing source/timestamp evidence: do not treat catalyst as verified. |
| Failures degrade safely | - [ ] | - [ ] A failure must be explicit; it must not become a fabricated catalyst. |

### Safety

| Gate | Pass | Fail / required action |
|---|---|---|
| LIVE still refuses | - [ ] | - [ ] Stop all testing. |
| Breaker can trip | - [ ] | - [ ] See deliberate emergency trip in section 6. |
| Breaker blocks entry | - [ ] | - [ ] Stop all testing; investigate immediately. |
| Controlled reset works | - [ ] | - [ ] Leave breaker tripped; do not bypass. |
| Stale real-world data blocks entry | - [ ] | - [ ] Stop; freshness is a hard gate. |
| Broker timeout/error blocks or reconciles safely | - [ ] | - [ ] Do not retry; trip/reconcile. |

### Journal

| Gate | Pass | Fail / required action |
|---|---|---|
| A real PAPER transaction can be reconstructed from journal records alone | - [ ] | - [ ] Do not promote; preserve evidence and repair observability. |

For a completed PAPER transaction, reconstruction must include candidate, score/risk decision, authorization checks, broker order ID, submit acknowledgement, independent broker status, protection result, and all `OrderEvent` transitions.

## 6. Abort and rollback procedure

### Stop safely at any point

- [ ] Do not start another `run` cycle.
- [ ] If the foreground command is still running and no order submission is in progress, interrupt it with `Ctrl+C`.
- [ ] Do **not** retry after a timeout or `SubmissionUncertainError`; the request may already have reached the broker.
- [ ] Read broker state and perform reconciliation before deciding whether anything must be cancelled or flattened.

### Emergency breaker trip

There is no CLI `breaker trip` subcommand. The supported persistent breaker object and `BreakerTrigger.OPERATOR_MANUAL_HALT` exist, so use this explicit emergency action only when halting new entries is required:

```bash
python - <<'PY'
from pathlib import Path
from app.config.loader import load_config
from app.risk.persistent_circuit_breaker import PersistentCircuitBreaker, BreakerTrigger

cfg = load_config()
path = Path(cfg.get("logging.dir") or "data") / "circuit_breaker.db"
PersistentCircuitBreaker(path, cfg=cfg.get("circuit_breaker")).trip(
    BreakerTrigger.OPERATOR_MANUAL_HALT,
    "Operator emergency halt during first PAPER verification",
)
print("Breaker tripped: new entries blocked; protective exits remain permitted")
PY
python -m app.cli breaker status
```

- [ ] Confirm breaker status is `TRIPPED` before proceeding with any protective action.
- [ ] A tripped breaker blocks new entries but deliberately permits protective exits, position closes, and cancellation.

### Cancel and flatten confirmed PAPER exposure

This project has no CLI cancel/flatten command. Do not invent one. If reconciliation confirms an open PAPER order or position, use the existing protective `ExecutionEngine` methods with the exact broker IDs/tickers you recorded; these methods are specifically ungated to reduce exposure. Perform one operation at a time and immediately re-read broker state. If you cannot establish the exact order ID/ticker and state, do not guess—leave the breaker tripped and resolve through the Alpaca PAPER account interface with the operator observing.

To cancel one **confirmed** open order, enter its broker order ID only when prompted:

```bash
read -r -p 'Confirmed PAPER broker order ID to cancel: ' BROKER_ORDER_ID
python - "$BROKER_ORDER_ID" <<'PY'
import sys
from app.broker.alpaca_adapter import AlpacaPaperBroker
from app.execution.engine import ExecutionEngine

broker = AlpacaPaperBroker()
engine = ExecutionEngine(broker)
engine.cancel_order(sys.argv[1], reason="first PAPER verification rollback")
status = broker.get_order_status(sys.argv[1])
print("broker_status=", status.status)
print("filled_qty=", status.filled_qty)
PY
```

To close one **confirmed** PAPER position, enter its ticker only when prompted:

```bash
read -r -p 'Confirmed PAPER position ticker to close: ' PAPER_TICKER
python - "$PAPER_TICKER" <<'PY'
import sys
from app.broker.alpaca_adapter import AlpacaPaperBroker
from app.execution.engine import ExecutionEngine

broker = AlpacaPaperBroker()
engine = ExecutionEngine(broker)
receipt = engine.close_position(sys.argv[1], reason="first PAPER verification rollback")
print("close_broker_order_id=", receipt.broker_order_id)
print("close_status=", receipt.status)
PY
```

These requests reduce exposure but their return values are not proof that the order is finished; re-read the close order by ID and re-run reconciliation. Do not use `flatten_all` unless the operator has independently confirmed that closing **every** PAPER position is intended.

- [ ] Cancel every confirmed open order before flattening a confirmed position.
- [ ] Flatten every confirmed position only after confirming its ticker and quantity at the broker.
- [ ] Re-run preflight reconciliation item 11 until it is clean.
- [ ] Preserve the journal and breaker databases; never delete either database to clear a fault.

### Controlled reset only after investigation

After cancellation/flattening and a clean reconciliation, a same-session reset requires both the explicit flag and a substantive reason (at least 10 characters). Omit `--yes` unless the operator has already reviewed the displayed trip reason and deliberately wants to skip the interactive `CLEAR` prompt.

```bash
python -m app.cli breaker reset --operator OPERATOR_NAME --reason "Describe the investigation and correction" --same-session
```

Expected: the CLI displays the active trip, asks for `CLEAR` unless `--yes` is supplied, and records both trip and reset in durable history. On failure: leave the breaker tripped. A reset is not a substitute for reconciliation.

### Leave the system safe

- [ ] No open PAPER orders remain at the broker.
- [ ] No PAPER positions remain at the broker unless explicitly retained with verified protective exits and documented operator acceptance.
- [ ] Reconciliation is clean.
- [ ] The breaker is left `TRIPPED` for any unresolved uncertainty; otherwise its reset history explains why it was cleared.
- [ ] Credentials remain only in the process environment/local ignored storage, never in version control or logs.

## 7. What this runbook does NOT authorize

- [ ] Unattended PAPER automation.
- [ ] LIVE trading, LIVE credentials, LIVE endpoints, or constructing a working `AlpacaLiveBroker`.
- [ ] Increasing share size, risk limits, or the configured universe.
- [ ] Adding strategies, feeds, order types, extended-hours behavior, or new broker integrations.
- [ ] Bypassing the pipeline by hand-creating authorization evidence/grants or directly calling order-submission methods.
- [ ] Treating a submit acknowledgement as a fill, or treating a client-selected PAPER URL as an Alpaca server-side guarantee.
