"""
Execution Authorization Boundary (Milestone 2, PART 2).

WHY THIS MODULE EXISTS
----------------------
In Milestone 1, `run_pipeline()` accepted a `mode` and a `broker` as two
unrelated arguments and trusted that its caller had already consulted
`ModeGovernor`. Nothing verified that. Anything that could import the pipeline
could construct a broker and submit orders in any mode — and the SHADOW guard
inside the pipeline was literally a `pass` statement. See
docs/AUDIT_MILESTONE2.md §3.

This module replaces "trust the caller" with "produce evidence or you cannot
build the object you need." The order path is now:

    Strategy -> Risk Engine -> ExecutionAuthorizer -> ExecutionEngine -> Broker

`ExecutionEngine.submit()` requires an `ExecutionGrant`. A grant cannot be
obtained except from `ExecutionAuthorizer.authorize()`, which requires a fully
populated `AuthorizationEvidence` bundle and fails closed on any missing field.
Broker adapters independently re-verify the grant, so even a caller that
bypasses the execution engine and holds an adapter directly cannot submit
without one.

HONEST STATEMENT OF THE LIMIT OF THIS BOUNDARY
----------------------------------------------
Python has no private memory. A determined author of new code inside this
repository can `from app.execution.authorization import _GRANT_SEAL` and forge a
grant. This module does not claim to prevent that, and any documentation that
claimed otherwise would be false.

What it does achieve, which is what the brief actually asks for:

  1. **No accidental path.** No future caller, scheduler, test harness,
     dashboard endpoint, background worker, or refactor can reach a broker
     submission by ordinary means — the type system stops them at a required
     argument they cannot produce.
  2. **Deliberate bypass is a single, greppable, reviewable act.** Importing
     `_GRANT_SEAL` outside this module is the only way through, and
     `tests/test_authorization_invariants.py::test_no_module_outside_authorization_imports_the_grant_seal`
     fails the build if anything does.
  3. **Every authorization is an auditable record.** A grant carries the full
     list of checks that produced it, and it is journaled.

REQUIRED INVARIANT (PART 2)
---------------------------
A LIVE order requires ALL of the following simultaneously. Each is a named
check below; none is sufficient alone, and no Boolean owned by strategy code
appears anywhere in the list:

    config_mode_matches_target
    live_config_from_permitted_source
    operator_per_run_authorization
    risk_engine_approved
    data_freshness_passed
    broker_account_state_valid
    broker_connected
    circuit_breaker_clear
    market_session_permits_orders
    short_sale_verified          (only when the intent is a short)
    broker_environment_matches_mode
    execution_engine_authorizes  (the grant itself; the engine refuses without one)
    (+ broker confirmation of the order, enforced downstream by the order
       lifecycle manager — a submit return value is never treated as a fill)
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import uuid
from enum import Enum

from app.common.modes import Mode

# ---------------------------------------------------------------------------
# The seal. See the honesty note in the module docstring: this makes forgery a
# deliberate, greppable, test-detectable act rather than an accident.
# ---------------------------------------------------------------------------
_GRANT_SEAL = object()

# Grants are single-use, process-wide. A grant authorizes exactly one order and
# cannot be replayed for a second submission, a retry loop, or a different
# ticker.
_CONSUMED_GRANT_IDS: set[str] = set()


class BrokerEnvironment(str, Enum):
    """What a broker adapter is actually wired to. Declared by the adapter
    class itself, not inferred from the mode the caller claims to be in."""

    SHADOW = "SHADOW"    # no network client exists at all
    PAPER = "PAPER"      # broker's official paper/sandbox endpoint
    LIVE = "LIVE"        # real money


#: The one legal broker environment for each mode. RESEARCH maps to None: it may
#: not contact a broker for order submission at all.
MODE_REQUIRES_BROKER_ENVIRONMENT: dict[Mode, BrokerEnvironment | None] = {
    Mode.RESEARCH: None,
    Mode.SHADOW: BrokerEnvironment.SHADOW,
    Mode.PAPER: BrokerEnvironment.PAPER,
    Mode.LIVE: BrokerEnvironment.LIVE,
}


class ExecutionNotAuthorizedError(Exception):
    """Raised instead of returning a falsy value, so that a caller cannot
    accidentally proceed past a failed authorization by ignoring a result."""

    def __init__(self, message: str, checks: tuple[AuthorizationCheck, ...] = ()):
        super().__init__(message)
        self.checks = checks

    def failed_check_names(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]


class GrantForgeryError(Exception):
    """Raised when an ExecutionGrant is constructed without the module seal."""


class GrantReplayError(Exception):
    """Raised when a grant that has already authorized an order is reused."""


@dataclasses.dataclass(frozen=True)
class AuthorizationCheck:
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.name}: {self.detail}"


@dataclasses.dataclass(frozen=True)
class ExecutionIntent:
    """Exactly what is being asked for. A grant is bound to this fingerprint, so
    a grant issued for 10 shares of AAPL cannot submit 1000 shares of TSLA."""

    mode: Mode
    ticker: str
    side: str                 # BUY | SELL | SHORT | COVER
    qty: float
    order_type: str
    entry: float | None = None
    stop: float | None = None
    candidate_id: str | None = None

    @property
    def is_short_sale(self) -> bool:
        return self.side.upper() == "SHORT"

    def fingerprint(self) -> str:
        canonical = "|".join([
            self.mode.value, self.ticker.upper(), self.side.upper(),
            f"{float(self.qty):.6f}", self.order_type,
        ])
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]


@dataclasses.dataclass(frozen=True)
class ExecutionGrant:
    """Proof that every authorization check passed for one specific intent.

    Constructing this directly raises GrantForgeryError. It can only be produced
    by ExecutionAuthorizer.authorize().
    """

    grant_id: str
    mode: Mode
    broker_environment: BrokerEnvironment
    intent_fingerprint: str
    issued_at: dt.datetime
    checks: tuple[AuthorizationCheck, ...]
    seal: dataclasses.InitVar[object] = None

    def __post_init__(self, seal: object) -> None:
        if seal is not _GRANT_SEAL:
            raise GrantForgeryError(
                "ExecutionGrant cannot be constructed directly. Grants are "
                "issued only by ExecutionAuthorizer.authorize(), which requires "
                "a complete AuthorizationEvidence bundle. If you are writing a "
                "test, build a real authorizer with the evidence you want to "
                "exercise rather than fabricating a grant."
            )

    def matches(self, intent: ExecutionIntent) -> bool:
        return self.intent_fingerprint == intent.fingerprint()

    def assert_valid_for(self, intent: ExecutionIntent, environment: BrokerEnvironment) -> None:
        """Called by the execution engine AND independently by every broker
        adapter. Two independent verifications of the same grant, so bypassing
        one layer does not bypass the check."""
        if not self.matches(intent):
            raise ExecutionNotAuthorizedError(
                f"Grant {self.grant_id} was issued for a different order "
                f"(fingerprint mismatch). Refusing to submit.", self.checks,
            )
        if self.broker_environment is not environment:
            raise ExecutionNotAuthorizedError(
                f"Grant {self.grant_id} authorizes environment "
                f"{self.broker_environment.value} but was presented to a "
                f"{environment.value} broker adapter. Refusing to submit.",
                self.checks,
            )
        if self.mode is not intent.mode:
            raise ExecutionNotAuthorizedError(
                f"Grant {self.grant_id} authorizes mode {self.mode.value} but "
                f"the intent claims {intent.mode.value}.", self.checks,
            )

    def consume(self) -> None:
        """Single-use enforcement. Prevents a retry loop or a replay from
        turning one authorization into many orders."""
        if self.grant_id in _CONSUMED_GRANT_IDS:
            raise GrantReplayError(
                f"Grant {self.grant_id} has already authorized an order. "
                f"Each order requires its own authorization. Re-run the "
                f"authorizer if you intend to submit again."
            )
        _CONSUMED_GRANT_IDS.add(self.grant_id)

    def as_record(self) -> dict:
        """Journal-safe representation. Never includes the seal."""
        return {
            "grant_id": self.grant_id,
            "mode": self.mode.value,
            "broker_environment": self.broker_environment.value,
            "intent_fingerprint": self.intent_fingerprint,
            "issued_at": self.issued_at.isoformat(),
            "checks": [dataclasses.asdict(c) for c in self.checks],
        }


_MISSING = object()


@dataclasses.dataclass
class AuthorizationEvidence:
    """Everything the authorizer needs, supplied explicitly by the caller.

    Every field defaults to a sentinel meaning "not supplied". A missing field
    produces a FAILED check, never a skipped one. That is the fail-closed
    property: when a future milestone adds a new required check, every existing
    call site starts refusing instead of silently passing.
    """

    # --- mode / operator authorization ---
    config_mode: Mode | object = _MISSING
    config_mode_source: str | None | object = _MISSING
    live_config_from_permitted_source: bool | object = _MISSING
    operator_live_flag_present: bool | object = _MISSING

    # --- risk ---
    risk_approved: bool | object = _MISSING
    risk_detail: str = ""

    # --- market data freshness ---
    data_fresh: bool | object = _MISSING
    freshness_detail: str = ""

    # --- broker / account ---
    broker_environment: BrokerEnvironment | object = _MISSING
    broker_connected: bool | object = _MISSING
    account_state_valid: bool | object = _MISSING
    account_state_detail: str = ""

    # --- system-level halts ---
    circuit_breaker_tripped: bool | object = _MISSING
    circuit_breaker_detail: str = ""

    # --- session ---
    session_permits_orders: bool | object = _MISSING
    session_detail: str = ""

    # --- short sales (only consulted when intent.is_short_sale) ---
    short_sale_verified: bool | None | object = _MISSING
    short_sale_detail: str = ""

    def _get(self, field_name: str):
        value = getattr(self, field_name)
        return None if value is _MISSING else value

    def _supplied(self, field_name: str) -> bool:
        return getattr(self, field_name) is not _MISSING


@dataclasses.dataclass(frozen=True)
class ExecutionDecision:
    """Non-raising authorization result, for journaling and the health snapshot.
    `grant` is None whenever `authorized` is False."""

    authorized: bool
    checks: tuple[AuthorizationCheck, ...]
    grant: ExecutionGrant | None
    intent: ExecutionIntent

    @property
    def failed(self) -> list[AuthorizationCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def first_failure_reason(self) -> str | None:
        failed = self.failed
        return f"{failed[0].name}: {failed[0].detail}" if failed else None

    def as_record(self) -> dict:
        return {
            "authorized": self.authorized,
            "ticker": self.intent.ticker,
            "mode": self.intent.mode.value,
            "checks": [dataclasses.asdict(c) for c in self.checks],
            "grant_id": self.grant.grant_id if self.grant else None,
            "first_failure": self.first_failure_reason,
        }


class ExecutionAuthorizer:
    """The single choke point between a risk-approved candidate and a broker.

    Stateless with respect to trading decisions: it only reads the evidence it
    is handed. It never queries a broker or a data feed itself, so it cannot be
    fooled by a stale cached value it fetched earlier — the caller must present
    fresh evidence, and the freshness of that evidence is itself one of the
    checks.
    """

    def __init__(self, *, target_mode: Mode):
        self.target_mode = target_mode

    # ------------------------------------------------------------------
    def evaluate(self, intent: ExecutionIntent, evidence: AuthorizationEvidence) -> ExecutionDecision:
        """Run every check and return the full result without raising."""
        checks: list[AuthorizationCheck] = []

        def add(name: str, passed: bool, detail: str) -> None:
            checks.append(AuthorizationCheck(name=name, passed=bool(passed), detail=detail))

        def require(name: str, field: str, detail_when_true: str, detail_when_false: str,
                    extra_detail: str = "") -> bool:
            """A tri-state check: supplied-and-true, supplied-and-false, or not
            supplied at all. The third case FAILS."""
            if not evidence._supplied(field):
                add(name, False,
                    f"No evidence supplied for `{field}`. Fail closed: an "
                    f"unproven safety condition is treated as a violated one.")
                return False
            value = evidence._get(field)
            suffix = f" {extra_detail}" if extra_detail else ""
            add(name, bool(value), (detail_when_true if value else detail_when_false) + suffix)
            return bool(value)

        # --- 1. the requested mode must equal the configured mode ---------
        if not evidence._supplied("config_mode"):
            add("config_mode_matches_target", False,
                "No evidence supplied for `config_mode`. Fail closed.")
        else:
            configured = evidence._get("config_mode")
            add("config_mode_matches_target", configured is intent.mode,
                f"configured={getattr(configured, 'value', configured)} "
                f"requested={intent.mode.value}"
                + ("" if configured is intent.mode else
                   " — refusing to act outside the configured mode."))

        # target_mode is the mode this authorizer instance was built for; an
        # intent for any other mode is refused outright.
        add("intent_mode_matches_authorizer", intent.mode is self.target_mode,
            f"authorizer built for {self.target_mode.value}, "
            f"intent is {intent.mode.value}")

        # --- 2. LIVE-only: provenance of the LIVE setting -----------------
        if intent.mode is Mode.LIVE:
            require("live_config_from_permitted_source",
                    "live_config_from_permitted_source",
                    f"LIVE was set by the git-ignored config/local.yaml "
                    f"(source={evidence._get('config_mode_source')}).",
                    f"LIVE did not come from config/local.yaml "
                    f"(source={evidence._get('config_mode_source')}). An "
                    f"environment variable, a --config overlay, or a "
                    f"version-controlled default may not select LIVE.")
            require("operator_per_run_authorization", "operator_live_flag_present",
                    "Operator supplied --i-understand-this-is-live-trading for "
                    "this specific run.",
                    "LIVE requires --i-understand-this-is-live-trading on this "
                    "run. It was not supplied.")
        else:
            add("live_config_from_permitted_source", True,
                f"Not applicable: mode is {intent.mode.value}, not LIVE.")
            add("operator_per_run_authorization", True,
                f"Not applicable: mode is {intent.mode.value}, not LIVE.")

        # --- 3. risk engine has veto authority ---------------------------
        require("risk_engine_approved", "risk_approved",
                evidence.risk_detail or "Risk engine approved this candidate.",
                evidence.risk_detail or "Risk engine did not approve this candidate.")

        # --- 4. data freshness is a hard gate ----------------------------
        require("data_freshness_passed", "data_fresh",
                evidence.freshness_detail or "All required market data within freshness limits.",
                evidence.freshness_detail or "Required market data exceeded its freshness "
                                             "threshold. FAIL CLOSED — no order.")

        # --- 5. broker / account state ----------------------------------
        require("broker_connected", "broker_connected",
                "Broker reachable.", "Broker is not connected.")
        require("broker_account_state_valid", "account_state_valid",
                evidence.account_state_detail or "Account state valid and tradable.",
                evidence.account_state_detail or "Account state is invalid, blocked, or unknown.")

        # --- 6. circuit breaker -----------------------------------------
        if not evidence._supplied("circuit_breaker_tripped"):
            add("circuit_breaker_clear", False,
                "No evidence supplied for `circuit_breaker_tripped`. Fail closed.")
        else:
            tripped = bool(evidence._get("circuit_breaker_tripped"))
            add("circuit_breaker_clear", not tripped,
                evidence.circuit_breaker_detail or
                ("Circuit breaker is TRIPPED — no new entries permitted."
                 if tripped else "Circuit breaker clear."))

        # --- 7. market session ------------------------------------------
        require("market_session_permits_orders", "session_permits_orders",
                evidence.session_detail or "Current session permits order submission.",
                evidence.session_detail or "Current market session does not permit this order.")

        # --- 8. short-sale verification ---------------------------------
        if intent.is_short_sale:
            if not evidence._supplied("short_sale_verified"):
                add("short_sale_verified", False,
                    "Short sale requested but no shortability evidence supplied. "
                    "Fail closed: long-only is preferable to assuming a borrow.")
            else:
                verified = evidence._get("short_sale_verified")
                # None means "the broker could not tell us" -> treat as unverified.
                add("short_sale_verified", verified is True,
                    evidence.short_sale_detail or
                    ("Shortability confirmed by broker."
                     if verified is True else
                     "Shortability could not be reliably verified. Rejecting the "
                     "short rather than assuming a borrow is available."))
        else:
            add("short_sale_verified", True,
                f"Not applicable: {intent.side} is not a short sale.")

        # --- 9. broker environment must match the mode ------------------
        required_env = MODE_REQUIRES_BROKER_ENVIRONMENT[intent.mode]
        if required_env is None:
            add("broker_environment_matches_mode", False,
                f"{intent.mode.value} mode may not submit orders to any broker "
                f"environment. This is not a misconfiguration — RESEARCH means "
                f"no orders, not even hypothetical ones.")
        elif not evidence._supplied("broker_environment"):
            add("broker_environment_matches_mode", False,
                "No evidence supplied for `broker_environment`. Fail closed.")
        else:
            actual_env = evidence._get("broker_environment")
            add("broker_environment_matches_mode", actual_env is required_env,
                f"mode {intent.mode.value} requires a {required_env.value} "
                f"broker adapter; the supplied adapter reports "
                f"{getattr(actual_env, 'value', actual_env)}"
                + ("" if actual_env is required_env else
                   " — refusing. This is the check that stops a LIVE-configured "
                   "adapter from being used for a PAPER run, and vice versa."))

        authorized = all(c.passed for c in checks)
        grant = None
        if authorized:
            grant = ExecutionGrant(
                grant_id=uuid.uuid4().hex,
                mode=intent.mode,
                broker_environment=evidence._get("broker_environment"),
                intent_fingerprint=intent.fingerprint(),
                issued_at=dt.datetime.now(dt.timezone.utc),
                checks=tuple(checks),
                seal=_GRANT_SEAL,
            )
        return ExecutionDecision(authorized=authorized, checks=tuple(checks),
                                 grant=grant, intent=intent)

    # ------------------------------------------------------------------
    def authorize(self, intent: ExecutionIntent, evidence: AuthorizationEvidence) -> ExecutionGrant:
        """Raising variant. Use this on the order path; use evaluate() when you
        want to record why something was refused without aborting the cycle."""
        decision = self.evaluate(intent, evidence)
        if not decision.authorized:
            failed = "; ".join(f"{c.name} ({c.detail})" for c in decision.failed)
            raise ExecutionNotAuthorizedError(
                f"Execution not authorized for {intent.side} {intent.qty} "
                f"{intent.ticker} in {intent.mode.value}: {failed}",
                decision.checks,
            )
        assert decision.grant is not None  # invariant: authorized implies grant
        return decision.grant
