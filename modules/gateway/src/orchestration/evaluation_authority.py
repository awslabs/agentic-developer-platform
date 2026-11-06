"""Use the same effective evaluation bounds for protected and shared flows."""

from datetime import UTC, datetime, timedelta

from .policy_admission import load_in_force_policy
from .review_cycle import CycleBlockedError
from .runtime_policy import flow_started_at


async def evaluation_inputs(session, *, org_id, flow_id):
    from .shared_policy import is_shared_continuation, shared_inputs

    if await is_shared_continuation(session, org_id=org_id, flow_id=flow_id):
        return await shared_inputs(session, org_id=org_id, flow_id=flow_id)
    inputs = await load_in_force_policy(session, org_id=org_id, flow_id=flow_id)
    if inputs.refusal or inputs.policy is None:
        raise CycleBlockedError("evaluation_policy_unverifiable")
    started = await flow_started_at(session, org_id=org_id, flow_id=flow_id)
    deadline = (
        min(inputs.policy.expires_at, started + timedelta(seconds=inputs.policy.limits.max_wall_clock_seconds))
        if started
        else inputs.policy.expires_at
    )
    if datetime.now(UTC) >= deadline:
        raise CycleBlockedError("wall_clock_limit_exceeded")
    # Membership is keyed by canonical users.id; the accepted policy records the
    # approver's Cognito sub. Dispatch and the protected path resolve it the same
    # way (#7419); authorizing with the raw sub answered membership_revoked.
    from src.shared.identity.resolver import UnresolvableUserEntityError, resolve_root_user_entity_id

    try:
        principal = await resolve_root_user_entity_id(session, org_id, inputs.policy.principal_id)
    except UnresolvableUserEntityError:
        # Keep the recorded principal; the membership check then answers for it
        # exactly as before, rather than inventing a second refusal here.
        principal = inputs.policy.principal_id
    return inputs, {"prior_spend_usd": "0", "principal_user_id": principal}
