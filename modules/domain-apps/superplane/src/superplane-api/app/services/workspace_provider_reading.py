"""Read the provider selected by this workspace's durable provisioning request."""

from fastapi import HTTPException
from harness_jobs.identity import decode_payload, payload_digest


async def provisioning_credential(composition, workspace):
    async with composition.operation_connect() as connection:
        row = await connection.fetchrow(
            "SELECT request_payload,plan_digest FROM harness_operations "
            "WHERE org_id=$1 AND workspace_id=$2 AND operation_id=$3 AND action='provision'",
            str(workspace.org_id), str(workspace.id), workspace.provisioning_operation_id,
        )
    if row is None:
        raise HTTPException(503, "Workspace provisioning request is unavailable")
    try:
        payload = decode_payload(row["request_payload"])
        handle = payload.parameters.get("credential_id")
        if payload_digest(payload) != row["plan_digest"] or not isinstance(handle, str) or not handle:
            raise ValueError("invalid provisioning credential")
    except (ValueError, TypeError, KeyError):
        raise HTTPException(503, "Workspace provider observation is unavailable") from None
    return handle
