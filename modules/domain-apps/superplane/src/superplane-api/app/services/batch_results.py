"""Authenticated reads of executor-retained text, including after Job cleanup."""

import hashlib

from fastapi import HTTPException
from harness_jobs.identity import decode_payload, payload_digest

from app.adapters.operation_authority_source import GrantBackedAuthority
from app.database import async_session_factory
from app.services.deployment_operations import composition, intent_for, stored_preview


async def read(request, db, org_id, workspace_id, job_id):
    authority = GrantBackedAuthority(async_session_factory)

    async def permitted():
        if (
            await authority.resolve(
                org_id=str(org_id),
                workspace_id=str(workspace_id),
                permission="workspace:read",
            )
            is None
        ):
            raise HTTPException(403, "batch result access refused")

    await permitted()
    intent = await intent_for(db, org_id, workspace_id, job_id, workload_kind="batch")
    original = stored_preview(intent)
    async with composition(request).operation_connect() as connection:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            source = await connection.fetchrow(
                "SELECT r.operation_id,r.allocation_id,r.plan_digest,o.request_payload "
                "FROM controller_deployment_operations r JOIN harness_operations o "
                "ON o.operation_id=r.operation_id AND o.org_id=r.org_id AND o.workspace_id=r.workspace_id "
                "AND o.plan_digest=r.plan_digest WHERE r.deployment_id=$1 AND r.org_id=$2 "
                "AND r.workspace_id=$3 AND r.action='provision'",
                str(job_id),
                str(org_id),
                str(workspace_id),
            )
            if (
                source is None
                or source["plan_digest"] != payload_digest(original.request)
                or decode_payload(source["request_payload"]) != original.request
                or source["allocation_id"]
                != original.request.parameters["allocation_id"]
            ):
                raise HTTPException(503, "original batch result operation unavailable")
            row = await connection.fetchrow(
                "SELECT * FROM controller_batch_results WHERE operation_id=$1 AND org_id=$2::text::uuid "
                "AND workspace_id=$3::text::uuid AND deployment_id=$4::text::uuid",
                source["operation_id"],
                str(org_id),
                str(workspace_id),
                str(job_id),
            )
            result = None
            if row is not None:
                reference = (
                    f"kubernetes:Job:{intent.namespace}:{intent.name}:{row['job_uid']}"
                )
                captured_uid = await connection.fetchval(
                    "SELECT count(*) FROM harness_allocation_resource WHERE operation_id=$1 "
                    "AND org_id=$2 AND workspace_id=$3 AND allocation_id=$4 AND provider='aws' "
                    "AND kind='workspace_object' AND provider_reference=$5",
                    source["operation_id"],
                    str(org_id),
                    str(workspace_id),
                    source["allocation_id"],
                    reference,
                )
                if (
                    row["allocation_id"] != source["allocation_id"]
                    or row["plan_digest"] != source["plan_digest"]
                    or captured_uid != 1
                    or len(row["content"].encode()) > 16384
                    or hashlib.sha256(row["content"].encode()).hexdigest()
                    != row["sha256"]
                ):
                    raise HTTPException(503, "retained batch result binding changed")
                result = {
                    key: row[key]
                    for key in (
                        "job_uid",
                        "pod_uid",
                        "content",
                        "sha256",
                        "redacted",
                        "captured_at",
                    )
                }
    await permitted()
    return {
        "workspace_id": str(workspace_id),
        "job_id": str(job_id),
        "operation_id": source["operation_id"],
        "result": result,
        "status": "retained" if result is not None else "not_captured",
        "media_type": "text/plain",
        "cleanup_status": "independent",
    }
