"""Protected canonical bootstrap completion journal reads; no provider credentials."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException


async def observe_bootstrap(request, body):
    from app.config import settings
    from app.routers.controller_recovery import claim_operation, composition
    from superplane_executor.recovery_authority import same_recovery_operation
    from workspace_provisioning.recovery_bootstrap import completed_bootstrap

    async with asyncio.timeout(20):
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
            raise HTTPException(
                403, "original bootstrap recovery registration unavailable"
            )
        context = SimpleNamespace(
            connect=connect,
            domain_connect=connect,
            policy_file=Path(settings.superplane_lifecycle_config_file),
        )
        row, facts = await completed_bootstrap(operation, context, body.idempotency_key)
        latest = await claim_operation(request, body.claim)
        if not same_recovery_operation(latest, operation):
            raise HTTPException(403, "bootstrap recovery authority changed")
    return {
        "phase": "bootstrap-workspace",
        "idempotency_key": body.idempotency_key,
        "plan_digest": operation.plan_digest,
        "result_artifact_id": row["artifact_id"],
        "facts": facts,
    }
