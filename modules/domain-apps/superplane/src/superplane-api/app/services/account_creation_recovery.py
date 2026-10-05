"""Observe original accepted account requests using management read authority."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from .lifecycle_recovery import bounded_json, scoped_observation_provider


READS = {
    ("sts", "get_caller_identity"): "sts:GetCallerIdentity",
    (
        "organizations",
        "describe_create_account_status",
    ): "organizations:DescribeCreateAccountStatus",
    ("organizations", "list_parents"): "organizations:ListParents",
}


async def account_context(request, body):
    from app.config import settings
    from app.routers.controller_recovery import claim_operation, composition
    from workspace_provisioning.account_recovery_observer import original_creation_call

    operation = await claim_operation(request, body.claim)
    connect = composition(request).operation_connect
    async with connect() as connection:
        registered = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
            "AND provisioning_operation_id=$3)",
            body.claim.workspace_id,
            body.claim.org_id,
            body.claim.operation_id,
        )
    if not registered:
        raise HTTPException(403, "original account recovery registration unavailable")
    context = SimpleNamespace(
        connect=connect,
        domain_connect=connect,
        policy_file=Path(settings.superplane_lifecycle_config_file),
    )
    source, call = await original_creation_call(
        operation, context, body.idempotency_key
    )
    return operation, context, source, call


@asynccontextmanager
async def observation_provider(*, account_id, region, current):
    async with scoped_observation_provider(
        account_id=account_id,
        region=region,
        current=current,
        reads=READS,
        role_environment="SUPERPLANE_ACCOUNT_RECOVERY_OBSERVATION_ROLE_ARN",
        read_limit=3,
        session_name="superplane-account-observe",
    ) as provider:
        yield provider


async def observe_account(request, body):
    from superplane_executor.recovery_authority import same_recovery_operation
    from workspace_provisioning.account_recovery_observer import observe_account_handoff

    async with asyncio.timeout(20):
        operation, context, source, call = await account_context(request, body)

        async def current():
            latest, _, approved, original = await account_context(request, body)
            if (
                not same_recovery_operation(latest, operation)
                or approved != source
                or original != call
            ):
                raise HTTPException(
                    403, "account recovery authority or request changed"
                )

        async with observation_provider(
            account_id=source.management_account_id,
            region=source.region,
            current=current,
        ) as provider:
            facts = await observe_account_handoff(
                operation,
                context,
                idempotency_key=body.idempotency_key,
                provider=provider,
            )
        await current()
    return {
        "phase": "create-account",
        "idempotency_key": body.idempotency_key,
        "plan_digest": operation.plan_digest,
        "facts": bounded_json(facts, 65536),
    }
