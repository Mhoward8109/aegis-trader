"""Shared test helpers.

The important property of this module is what it does NOT contain: there is no
way here to fabricate an `ExecutionGrant`. `_GRANT_SEAL` is never imported. Every
grant used by a test is produced by the real `ExecutionAuthorizer` running its
real checks, so a test cannot accidentally prove that execution works while
bypassing the authorization boundary it is supposed to be exercising.

If a future change makes the authorizer stricter, these helpers start failing
loudly instead of silently minting grants the production path would refuse.
"""

from __future__ import annotations

from app.broker.base import OrderRequest
from app.common.modes import Mode
from app.execution.authorization import (
    AuthorizationEvidence,
    BrokerEnvironment,
    ExecutionAuthorizer,
    ExecutionGrant,
    ExecutionIntent,
)

# Modes that can legitimately reach a broker adapter, and the environment their
# adapter must report. Mirrors MODE_REQUIRES_BROKER_ENVIRONMENT deliberately
# rather than importing it, so a change to that table breaks a test.
_MODE_ENVIRONMENTS = {
    Mode.SHADOW: BrokerEnvironment.SHADOW,
    Mode.PAPER: BrokerEnvironment.PAPER,
}


def intent_for(req: OrderRequest, mode: Mode = Mode.SHADOW) -> ExecutionIntent:
    return ExecutionIntent(
        mode=mode,
        ticker=req.ticker,
        side=req.side,
        qty=req.qty,
        order_type=req.order_type,
        entry=getattr(req, "limit_price", None),
        stop=getattr(req, "stop_price", None),
    )


def all_conditions_satisfied_evidence(
    mode: Mode = Mode.SHADOW, **overrides
) -> AuthorizationEvidence:
    """Evidence in which every safety condition is genuinely satisfied.

    Tests that want to prove a *refusal* should override exactly one field, so
    the test demonstrates that this one condition is load-bearing.
    """
    environment = _MODE_ENVIRONMENTS.get(mode)
    base = dict(
        config_mode=mode,
        config_mode_source="default.yaml",
        live_config_from_permitted_source=False,
        operator_live_flag_present=False,
        risk_approved=True,
        risk_detail="test fixture: risk engine approved",
        data_fresh=True,
        freshness_detail="test fixture: data within freshness limits",
        broker_environment=environment,
        broker_connected=True,
        account_state_valid=True,
        account_state_detail="test fixture: account tradable",
        circuit_breaker_tripped=False,
        circuit_breaker_detail="test fixture: breaker clear",
        session_permits_orders=True,
        session_detail="test fixture: REGULAR session",
        short_sale_verified=True,
        short_sale_detail="test fixture: shortability confirmed",
    )
    base.update(overrides)
    return AuthorizationEvidence(**base)


def grant_for(req: OrderRequest, mode: Mode = Mode.SHADOW, **overrides) -> ExecutionGrant:
    """Mint a real grant for `req` by running the real authorizer.

    Raises `ExecutionNotAuthorizedError` if the authorizer refuses, which is the
    intended behaviour: a test may not proceed to a broker call that production
    would have blocked.
    """
    authorizer = ExecutionAuthorizer(target_mode=mode)
    return authorizer.authorize(
        intent_for(req, mode), all_conditions_satisfied_evidence(mode, **overrides)
    )
