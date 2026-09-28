"""Cancelled-source cleanup eligibility, separate from execution authority.

The existing paid recovery task calls service.recover -> ScopedRecovery.run ->
sweep_scoped_expired_leases -> fence_expired_lease(recovery_principal=...). Its
durable claim binding proves the shared expiry/dispatch-lock takeover. Preview
only reads that evidence. It never fences a source or borrows its credentials.
"""

from harness_jobs.identity import (
    OperationRefused,
    decode_payload,
    encode_payload,
    payload_digest,
)

from .deployment_plan import teardown_request


async def source_for(
    connection, *, org_id, workspace_id, deployment_id, source_id=None
):
    row = await connection.fetchrow(
        "SELECT o.*,l.closed_at,l.holder AS lease_holder,l.attempt_id AS lease_attempt,"
        "l.closed_holder,l.closed_attempt_id,l.fence_token AS source_fence,"
        "l.acquired_at AS source_acquired_at FROM controller_deployment_operations r "
        "JOIN harness_operations o ON o.operation_id=r.operation_id AND o.org_id=r.org_id "
        "AND o.workspace_id=r.workspace_id AND o.plan_digest=r.plan_digest "
        "JOIN harness_operation_leases l ON l.operation_id=o.operation_id "
        "AND l.org_id=o.org_id AND l.workspace_id=o.workspace_id "
        "WHERE r.org_id=$1 AND r.workspace_id=$2 AND r.deployment_id=$3 "
        "AND r.action='provision' AND ($4::text IS NULL OR r.operation_id=$4)",
        org_id,
        workspace_id,
        deployment_id,
        source_id,
    )
    if row is None:
        raise OperationRefused("original registered deployment source is unavailable")
    original = decode_payload(row["request_payload"])
    if original.action != "provision" or payload_digest(original) != row["plan_digest"]:
        raise OperationRefused("original deployment source identity changed")
    return row


async def existing(connection, source):
    return await connection.fetchrow(
        "SELECT * FROM controller_cleanup_bindings WHERE source_operation_id=$1 "
        "AND org_id=$2 AND workspace_id=$3",
        source["operation_id"],
        source["org_id"],
        source["workspace_id"],
    )


async def eligibility(connection, source, *, binding=None):
    settled = source["closed_at"] is not None and source["state"] in {
        "succeeded",
        "failed",
        "cancelled",
    }
    if settled and binding is None:
        return None  # Preserve the original settled-source teardown path.
    if source["cancel_requested_at"] is None or not source["cancel_requested_by"]:
        raise OperationRefused(
            "request authenticated source cancellation before cleanup"
        )
    holder = (
        source["closed_holder"]
        if source["closed_at"] is not None
        else source["lease_holder"]
    )
    attempt = (
        source["closed_attempt_id"]
        if source["closed_at"] is not None
        else source["lease_attempt"]
    )
    claim = await connection.fetchrow(
        "SELECT * FROM harness_recovery_claim_bindings WHERE operation_id=$1 "
        "AND org_id=$2 AND workspace_id=$3 AND fence_token=$4 AND holder=$5 AND attempt_id=$6",
        source["operation_id"],
        source["org_id"],
        source["workspace_id"],
        source["source_fence"],
        holder,
        attempt,
    )
    if claim is None or not holder or not holder.startswith("recovery:"):
        raise OperationRefused(
            "cancelled source awaits authenticated recovery takeover after execution expiry and dispatch quiescence"
        )
    if binding is None:
        paid = await connection.fetchval(
            "SELECT 1 FROM harness_approval_consumption a JOIN operation_budget_reservations r "
            "ON r.reservation_id=a.reservation_id AND r.org_id=a.org_id AND r.workspace_id=a.workspace_id "
            "WHERE a.operation_id=$1 AND a.org_id=$2 AND a.workspace_id=$3 AND a.plan_digest=$4 "
            "AND a.reservation_state IN ('confirmed','retained') AND r.state IN ('confirmed','retained')",
            source["operation_id"],
            source["org_id"],
            source["workspace_id"],
            source["plan_digest"],
        )
        if not paid:
            raise OperationRefused(
                "cancelled source has no retained original paid reservation"
            )
    elif (
        binding["source_plan_digest"] != source["plan_digest"]
        or binding["cancel_requested_at"] != source["cancel_requested_at"]
        or binding["cancel_requested_by"] != source["cancel_requested_by"]
        or source["source_fence"] < binding["source_fence"]
    ):
        raise OperationRefused("cancelled source cleanup fencing changed")
    if binding is not None and not await connection.fetchval(
        "SELECT 1 FROM harness_recovery_claim_bindings WHERE operation_id=$1 "
        "AND org_id=$2 AND workspace_id=$3 AND fence_token=$4 AND holder=$5 AND attempt_id=$6 AND subject=$7",
        source["operation_id"],
        source["org_id"],
        source["workspace_id"],
        binding["source_fence"],
        binding["claim_holder"],
        binding["claim_attempt_id"],
        binding["claim_subject"],
    ):
        raise OperationRefused("original cleanup takeover evidence is unavailable")
    # No expiry test: a cleanup has its own separately approved lease. A later
    # authenticated observation takeover may advance the source fence safely.
    return claim


def require_original_request(source, request, graph=None):
    if (
        teardown_request(
            decode_payload(source["request_payload"]),
            org_id=source["org_id"],
            workspace_id=source["workspace_id"],
            request_id=request.idempotency_key,
            source_operation_id=source["operation_id"],
            cleanup_graph=graph,
        )
        != request
    ):
        raise OperationRefused("cleanup changed the exact original allocation or plan")


def matches(binding, source, request, *, approval_id=None):
    if (
        binding["source_operation_id"] != source["operation_id"]
        or binding["source_plan_digest"] != source["plan_digest"]
        or binding["allocation_id"] != request.parameters["allocation_id"]
        or binding["deployment_id"] != request.parameters["controller_deployment_id"]
        or binding["request_id"] != request.idempotency_key
        or binding["request_payload"] != encode_payload(request)
        or binding["plan_digest"] != payload_digest(request)
        or (approval_id is not None and binding["approval_id"] != str(approval_id))
    ):
        raise OperationRefused(
            "cleanup already binds a different original request or approval"
        )


async def validate(
    connection, source, request, *, approval_id=None, require_binding=False
):
    graph = await validated_graph(connection, source, request)
    require_original_request(source, request, graph)
    binding = await existing(connection, source)
    claim = await eligibility(connection, source, binding=binding)
    if binding is not None:
        matches(binding, source, request, approval_id=approval_id)
    elif require_binding and claim is not None:
        raise OperationRefused(
            "cancelled-source cleanup admission binding is unavailable"
        )
    return claim


async def bind(connection, source, request, *, approval_id):
    """Commit this intent before external ledger admission; never lock source I/O."""
    graph = await validated_graph(connection, source, request)
    require_original_request(source, request, graph)
    binding = await existing(connection, source)
    claim = await eligibility(connection, source, binding=binding)
    if binding is not None:
        matches(binding, source, request, approval_id=approval_id)
        return
    if claim is None:
        return
    await connection.execute(
        "INSERT INTO controller_cleanup_bindings "
        "(source_operation_id,org_id,workspace_id,deployment_id,allocation_id,source_plan_digest,"
        "source_fence,claim_holder,claim_attempt_id,claim_subject,cancel_requested_at,cancel_requested_by,"
        "request_id,request_payload,plan_digest,approval_id) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16) ON CONFLICT DO NOTHING",
        source["operation_id"],
        source["org_id"],
        source["workspace_id"],
        request.parameters["controller_deployment_id"],
        request.parameters["allocation_id"],
        source["plan_digest"],
        source["source_fence"],
        claim["holder"],
        claim["attempt_id"],
        claim["subject"],
        source["cancel_requested_at"],
        source["cancel_requested_by"],
        request.idempotency_key,
        encode_payload(request),
        payload_digest(request),
        str(approval_id),
    )
    binding = await existing(connection, source)
    if binding is None:
        raise OperationRefused("allocation already has another cleanup owner")
    matches(binding, source, request, approval_id=approval_id)


async def validated_graph(connection, source, request):
    from .cleanup_graph import PARAMETER, read
    from .cleanup_snapshot import select

    if PARAMETER not in request.parameters:
        return None
    graph = read(request.parameters[PARAMETER])
    await select(connection, source, graph)
    return graph
