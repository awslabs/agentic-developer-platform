"""One bounded retry of a proven-undispatched admission, under fresh authority.

The transport must prove absence while this transaction holds the same operation
lock used before allocating any durable dispatch mapping. Database absence alone
is never a transport proof. An immutable receipt preserves the abandoned cycle;
approval consumption, request bytes, operation/job/attempt and budget stay intact.
"""

import json
from datetime import UTC, datetime

from .execution import audit
from .identity import OperationRefused, decode_payload, payload_digest

EFFECT_TABLES = (
    "harness_operation_leases",
    "harness_provider_call_intent",
    "harness_provider_report",
    "harness_allocation_resource",
    "harness_execution_audit",
)


async def recover_undispatched(
    connection,
    *,
    principal,
    operation_id,
    expected_version,
    reauthorization_id,
    authorize,
    prove_absence,
):
    """Callbacks recheck real domain authority and the protected transport.

    They run inside the transaction; timeout/error rolls everything back. Only
    the original requester with current provisioning permission may recover. A
    receipt permits response-loss replay but never a second recovery cycle.
    """
    if "workspace:provision" not in principal.permissions:
        raise OperationRefused("current provisioning authority required")
    async with connection.transaction():
        # Match outbox settlement's lock ordering, then serialize Gateway dispatch.
        outbox = await connection.fetchrow(
            "SELECT * FROM harness_dispatch_outbox WHERE operation_id=$1 "
            "AND org_id=$2 AND workspace_id=$3 FOR UPDATE",
            operation_id,
            principal.org_id,
            principal.workspace_id,
        )
        operation = await connection.fetchrow(
            "SELECT * FROM harness_operations WHERE operation_id=$1 "
            "AND org_id=$2 AND workspace_id=$3 FOR UPDATE",
            operation_id,
            principal.org_id,
            principal.workspace_id,
        )
        paid = await connection.fetchrow(
            "SELECT * FROM harness_approval_consumption WHERE operation_id=$1 "
            "AND org_id=$2 AND workspace_id=$3 FOR UPDATE",
            operation_id,
            principal.org_id,
            principal.workspace_id,
        )
        if operation is None or outbox is None or paid is None:
            raise OperationRefused("original admission unavailable")
        if (
            any(
                outbox[key] != operation[key]
                for key in (
                    "org_id",
                    "workspace_id",
                    "job_id",
                    "attempt_id",
                    "action",
                    "request_payload",
                )
            )
            or paid["requester"] != principal.subject
            or paid["plan_digest"] != operation["plan_digest"]
            or payload_digest(decode_payload(operation["request_payload"]))
            != operation["plan_digest"]
        ):
            raise OperationRefused("original admission identity differs")
        previous = await connection.fetchrow(
            "SELECT * FROM harness_dispatch_reauthorizations WHERE operation_id=$1",
            operation_id,
        )
        if previous is not None:
            if (
                previous["reauthorization_id"] != reauthorization_id
                or previous["expected_version"] != expected_version
                or previous["requester"] != principal.subject
            ):
                raise OperationRefused("this admission already used its recovery")
            return dict(previous)
        if (
            operation["state"] != "unknown"
            or operation["version"] != expected_version
            or operation["cancel_requested_at"] is not None
            or operation["cleanup_required"]
            or outbox["delivered_at"] is not None
            or outbox["abandoned_at"] is None
            or outbox["claimed_until"] is not None
            or paid["reservation_state"] != "confirmed"
        ):
            raise OperationRefused(
                "admission is not eligible for undispatched recovery"
            )
        for table in EFFECT_TABLES:
            if await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM " + table + " WHERE operation_id=$1)",
                operation_id,
            ):
                raise OperationRefused(
                    "execution evidence prevents undispatched recovery"
                )
        deadline = await authorize(dict(operation), dict(paid))
        proof = await prove_absence(dict(operation))
        if not isinstance(proof, dict) or proof.get("undelivered") is not True:
            raise OperationRefused("protected dispatch absence was not established")
        if deadline <= datetime.now(UTC):
            raise OperationRefused("reauthorization expired during recovery")
        snapshot = {k: v for k, v in dict(outbox).items() if k != "request_payload"}
        receipt = await connection.fetchrow(
            "INSERT INTO harness_dispatch_reauthorizations "
            "(operation_id,org_id,workspace_id,reauthorization_id,original_approval_id,"
            "requester,approved_by,plan_digest,expected_version,restored_version,"
            "outbox_snapshot,transport_proof) VALUES "
            "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING *",
            operation_id,
            principal.org_id,
            principal.workspace_id,
            reauthorization_id,
            paid["approval_id"],
            principal.subject,
            paid["approved_by"],
            operation["plan_digest"],
            expected_version,
            expected_version + 1,
            json.dumps(snapshot, default=str),
            json.dumps(proof),
        )
        await connection.execute(
            "UPDATE harness_operations SET state='pending',version=version+1,"
            "detail='Original undispatched admission reauthorized',"
            "updated_at=clock_timestamp() "
            "WHERE operation_id=$1",
            operation_id,
        )
        # Attempts count this new bounded cycle; the complete old cycle is immutable
        # above. The claim fence never resets, so old acknowledgements cannot win.
        await connection.execute(
            "UPDATE harness_dispatch_outbox SET attempts=0,"
            "claim_generation=claim_generation+1,"
            "abandoned_at=NULL,claimed_until=NULL,last_error=NULL "
            "WHERE operation_id=$1",
            operation_id,
        )
        await audit(
            connection,
            operation_id=operation_id,
            org_id=principal.org_id,
            workspace_id=principal.workspace_id,
            attempt_id=operation["attempt_id"],
            event="undispatched-reauthorization",
            actor=principal.subject,
            allowed=True,
            detail=(
                "Fresh human reauthorization; "
                "original identities and reservation preserved"
            ),
        )
        return dict(receipt)
