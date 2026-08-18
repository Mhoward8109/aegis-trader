"""
Execution Engine (Milestone 2, PART 2).

The only supported way to reach a broker's order wire.

    Strategy -> Risk Engine -> ExecutionAuthorizer -> ExecutionEngine -> Broker

`submit()` requires an `ExecutionGrant`, which only `ExecutionAuthorizer.authorize()`
can produce. The broker adapter then re-verifies the same grant against its own
class-level `environment` declaration. Two independent verifications of one
grant, so bypassing either layer still does not get an order out.

WHAT THIS CLASS DELIBERATELY DOES NOT DO
----------------------------------------
It does not decide whether a trade is a good idea, and it holds no thresholds.
Every gate it consults was evaluated upstream and arrives as evidence. Putting
risk logic here would create a second place where "should we trade" is decided,
and two such places drift apart.

It also never interprets a submit return value as a fill. `submit()` returns a
`SubmissionReceipt` whose name is chosen to resist that mistake: what came back
is an acknowledgement that the broker received something, and the order
lifecycle manager is responsible for establishing what actually happened. See
PART 12.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import uuid

from app.broker.base import BrokerAdapter, BrokerError, BrokerOrderStatus, OrderRequest
from app.execution.authorization import (
    BrokerEnvironment,
    ExecutionGrant,
    ExecutionIntent,
    ExecutionNotAuthorizedError,
)

log = logging.getLogger("aegis.execution.engine")


class SubmissionUncertainError(Exception):
    """Raised when a submission may or may not have reached the broker.

    This is a distinct exception type on purpose. A caller must not retry on
    this, because a retry could double the position. The correct response is to
    reconcile against the broker and trip the circuit breaker, which is what
    `ExecutionEngine.submit()` arranges before raising.
    """

    def __init__(self, message: str, *, client_order_id: str, intent: ExecutionIntent):
        super().__init__(message)
        self.client_order_id = client_order_id
        self.intent = intent


@dataclasses.dataclass(frozen=True)
class SubmissionReceipt:
    """Evidence that a submission was ATTEMPTED and acknowledged.

    Named "receipt", not "fill", because the distinction is the point. Read
    `broker_status.status` to learn what the broker says the order is; do not
    infer a fill from the existence of this object.
    """

    client_order_id: str
    grant_id: str
    intent: ExecutionIntent
    broker_environment: BrokerEnvironment
    broker_status: BrokerOrderStatus
    submitted_at: dt.datetime

    @property
    def broker_order_id(self) -> str:
        return self.broker_status.broker_order_id

    @property
    def is_confirmed_fill(self) -> bool:
        """True ONLY when the broker itself reports the order fully filled with
        a nonzero filled quantity and an average price.

        Milestone 1 used `status.status == "filled"` at submit time as a fill
        test. That treated a market order's optimistic acknowledgement as a
        completed trade. The extra conditions here mean a status string alone is
        not enough.
        """
        s = self.broker_status
        return (
            str(s.status).lower() == "filled"
            and s.filled_qty > 0
            and s.filled_avg_price is not None
        )

    def as_record(self) -> dict:
        return {
            "client_order_id": self.client_order_id,
            "grant_id": self.grant_id,
            "broker_order_id": self.broker_order_id,
            "broker_environment": self.broker_environment.value,
            "broker_status": str(self.broker_status.status),
            "filled_qty": self.broker_status.filled_qty,
            "filled_avg_price": self.broker_status.filled_avg_price,
            "is_confirmed_fill": self.is_confirmed_fill,
            "submitted_at": self.submitted_at.isoformat(),
            "raw": self.broker_status.raw,
        }


class ExecutionEngine:
    """Submits authorized orders and nothing else.

    Args:
        broker: the adapter to submit through. Its `environment` declaration is
            the authoritative statement of where orders go -- the engine does
            not accept a separate "mode" argument that could disagree with it.
        on_submission_uncertain: optional callback invoked when a submission
            raises after the request may already have reached the broker. Wired
            by the pipeline to trip the circuit breaker, because an unknown
            submission state must stop trading rather than be retried.
    """

    def __init__(self, broker: BrokerAdapter, *, on_submission_uncertain=None):
        if broker.environment is None:
            raise ExecutionNotAuthorizedError(
                f"{type(broker).__name__} does not declare a BrokerEnvironment. "
                f"An execution engine cannot be built around an adapter whose "
                f"destination is unknown."
            )
        self.broker = broker
        self._on_submission_uncertain = on_submission_uncertain

    @property
    def environment(self) -> BrokerEnvironment:
        return self.broker.environment

    def submit(self, req: OrderRequest, grant: ExecutionGrant,
               intent: ExecutionIntent) -> SubmissionReceipt:
        """Submit one authorized order.

        The intent is passed explicitly rather than reconstructed from `req`, so
        that a mismatch between what was authorized and what is being submitted
        surfaces as a fingerprint failure instead of being papered over by
        re-deriving the fingerprint from the request we are about to send.
        """
        self._assert_request_matches_intent(req, intent)

        # Verified here AND again inside the adapter. The adapter's copy is the
        # one that matters for defence in depth; this one produces a clearer
        # error at the layer the caller is actually talking to.
        grant.assert_valid_for(intent, self.environment)

        client_order_id = req.client_order_id or f"aegis-{uuid.uuid4().hex[:20]}"
        req = dataclasses.replace(req, client_order_id=client_order_id)
        submitted_at = dt.datetime.now(dt.timezone.utc)

        log.info("Submitting %s %s x%s (%s) via %s [grant=%s coid=%s]",
                 intent.side, intent.ticker, intent.qty, intent.order_type,
                 self.environment.value, grant.grant_id, client_order_id)

        try:
            status = self.broker.submit_order(req, grant)
        except ExecutionNotAuthorizedError:
            # Authorization failures are definite: nothing was sent. Propagate
            # unchanged so callers can distinguish "refused" from "unknown".
            raise
        except BrokerError as exc:
            # A BrokerError raised by our own pre-flight validation means nothing
            # was sent. A BrokerError wrapping a transport failure might mean the
            # request landed. We cannot tell the two apart from here, so we treat
            # it as uncertain -- the conservative reading.
            self._handle_uncertain(exc, client_order_id, intent)
            raise SubmissionUncertainError(
                f"Submission of {intent.qty} {intent.ticker} raised "
                f"{type(exc).__name__}: {exc}. Whether the broker received this "
                f"order is UNKNOWN. Do not retry -- reconcile against the broker "
                f"first. A retry here could open a double position.",
                client_order_id=client_order_id, intent=intent,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            self._handle_uncertain(exc, client_order_id, intent)
            raise SubmissionUncertainError(
                f"Unexpected {type(exc).__name__} during submission of "
                f"{intent.qty} {intent.ticker}: {exc}. Submission state is "
                f"UNKNOWN. Do not retry -- reconcile first.",
                client_order_id=client_order_id, intent=intent,
            ) from exc

        if status is None:
            self._handle_uncertain(
                RuntimeError("adapter returned None"), client_order_id, intent)
            raise SubmissionUncertainError(
                f"Broker adapter returned no status for {intent.ticker}. "
                f"Treating submission state as UNKNOWN.",
                client_order_id=client_order_id, intent=intent,
            )

        receipt = SubmissionReceipt(
            client_order_id=client_order_id, grant_id=grant.grant_id,
            intent=intent, broker_environment=self.environment,
            broker_status=status, submitted_at=submitted_at,
        )
        log.info("Broker acknowledged %s: id=%s status=%s filled=%s@%s",
                 intent.ticker, receipt.broker_order_id, status.status,
                 status.filled_qty, status.filled_avg_price)
        return receipt

    def _handle_uncertain(self, exc: BaseException, client_order_id: str,
                          intent: ExecutionIntent) -> None:
        log.error("Submission state UNKNOWN for %s (coid=%s): %s",
                  intent.ticker, client_order_id, exc)
        if self._on_submission_uncertain is not None:
            try:
                self._on_submission_uncertain(exc=exc, client_order_id=client_order_id,
                                              intent=intent)
            except Exception:  # noqa: BLE001 - never mask the original failure
                log.exception("on_submission_uncertain callback itself failed")

    @staticmethod
    def _assert_request_matches_intent(req: OrderRequest, intent: ExecutionIntent) -> None:
        mismatches = []
        if req.ticker.upper() != intent.ticker.upper():
            mismatches.append(f"ticker {req.ticker!r} != {intent.ticker!r}")
        if req.side.upper() != intent.side.upper():
            mismatches.append(f"side {req.side!r} != {intent.side!r}")
        if abs(float(req.qty) - float(intent.qty)) > 1e-9:
            mismatches.append(f"qty {req.qty} != {intent.qty}")
        if req.order_type != intent.order_type:
            mismatches.append(f"order_type {req.order_type!r} != {intent.order_type!r}")
        if mismatches:
            raise ExecutionNotAuthorizedError(
                "The OrderRequest does not match the authorized ExecutionIntent: "
                + "; ".join(mismatches)
                + ". Refusing to submit an order that differs from what was "
                  "authorized."
            )

    # -- protective actions ------------------------------------------------
    #
    # These take NO grant. Reducing or eliminating exposure must never be
    # blocked by the entry-authorization path: a tripped breaker, a stale quote,
    # or an expired grant must not leave a position stranded. The asymmetry is
    # the safety property, not an oversight.
    def close_position(self, ticker: str, *, reason: str) -> BrokerOrderStatus:
        log.warning("Closing position %s (protective, ungated): %s", ticker, reason)
        return self.broker.close_position(ticker)

    def cancel_order(self, broker_order_id: str, *, reason: str) -> None:
        log.warning("Cancelling order %s (protective, ungated): %s",
                    broker_order_id, reason)
        self.broker.cancel_order(broker_order_id)

    def cancel_all_orders(self, *, reason: str) -> None:
        log.warning("Cancelling ALL orders (protective, ungated): %s", reason)
        self.broker.cancel_all_orders()

    def flatten_all(self, *, reason: str) -> None:
        """Cancel every order and close every position. The operator's panic
        button and the end-of-session handler."""
        log.critical("FLATTEN ALL (protective, ungated): %s", reason)
        self.broker.close_all_positions()
