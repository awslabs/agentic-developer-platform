"""Anchor completed canonical registration and authority bytes in a result artifact."""

import json

from .artifacts import digest
from .runtime_config import LifecycleRefused


async def read_bootstrap_anchor(
    context, *, operation_id, org_id, workspace_id, registration, claim
):
    """Read-only verification; the caller resolves execution or recovery authority."""
    from superplane_bootstrap.registry import _LOCK_PREFIX
    from superplane_bootstrap.membership import registration_membership
    from superplane_bootstrap.errors import BootstrapRefused

    placement = registration.get("cluster_placement", "dedicated")
    if placement not in {"dedicated", "shared"}:
        raise LifecycleRefused("bootstrap registration placement is invalid")
    try:
        membership = registration_membership(registration)
    except (BootstrapRefused, KeyError, TypeError, ValueError):
        raise LifecycleRefused("bootstrap membership identity is invalid") from None
    if placement == "shared" and membership is None:
        raise LifecycleRefused(
            "shared bootstrap result requires its approved membership"
        )

    if (registration.get("org_id"), registration.get("workspace_id")) != (
        org_id,
        workspace_id,
    ):
        raise LifecycleRefused("bootstrap registration names a different workspace")
    async with context.domain_connect() as connection, connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            _LOCK_PREFIX + workspace_id,
        )
        recorded = await connection.fetchval(
            "SELECT identity_json FROM workspace_bootstrap_reservations WHERE workspace_id=$1 AND state='registered' FOR UPDATE",
            workspace_id,
        )
        if recorded is None or json.loads(recorded) != registration:
            raise LifecycleRefused("completed bootstrap registration changed")
        canonical = await connection.fetchrow(
            "SELECT w.namespace_name,c.eks_cluster_arn,c.endpoint,c.actual_state_json "
            "FROM workspaces w JOIN clusters c ON c.id=w.cluster_id AND ($3 OR c.workspace_id=w.id) AND c.org_id=w.org_id "
            "JOIN organizations o ON o.id=w.org_id WHERE w.id::text=$1 AND (o.id::text=$2 OR o.adp_org_id=$2) FOR UPDATE OF w,c",
            workspace_id,
            org_id,
            placement == "shared",
        )
        if canonical is None or (
            canonical["namespace_name"],
            canonical["eks_cluster_arn"],
            canonical["endpoint"],
        ) != (
            registration["namespace"],
            registration["cluster_arn"],
            registration["endpoint"],
        ):
            raise LifecycleRefused("canonical bootstrap target changed")
        if membership is not None:
            from .shared_membership import verify

            await verify(connection, membership, states={"active"})
            member = await connection.fetchrow(
                "SELECT namespace_uid,credential_reference_id FROM cluster_memberships "
                "WHERE workspace_id::text=$1 AND org_id::text=$2 AND cluster_id::text=$3 "
                "AND generation=$4 AND state='active' FOR UPDATE",
                workspace_id,
                membership.org_id,
                membership.cluster_id,
                membership.generation,
            )
            if member is None or (
                member["namespace_uid"],
                member["credential_reference_id"],
            ) != (
                registration["namespace_uid"],
                registration["credential_reference_id"],
            ):
                raise LifecycleRefused("canonical bootstrap membership changed")
        else:
            actual = canonical["actual_state_json"]
            if isinstance(actual, str):
                actual = json.loads(actual)
            if (
                not isinstance(actual, dict)
                or actual.get("workspace_bootstrap") != registration
            ):
                raise LifecycleRefused("canonical bootstrap metadata changed")
        rows = await connection.fetch(
            "SELECT generation,operation_id,org_id,cluster_arn,claim,plan_json,progress_json,revoked "
            "FROM workspace_bootstrap_authority WHERE workspace_id=$1 ORDER BY generation LIMIT 129 FOR UPDATE",
            workspace_id,
        )
        if not rows or len(rows) > 128:
            raise LifecycleRefused(
                "completed bootstrap authority inventory is incomplete"
            )
        anchors, current = [], []
        for row in rows:
            progress = json.loads(row["progress_json"])
            if (row["org_id"], row["cluster_arn"]) != (
                org_id,
                registration["cluster_arn"],
            ) or (
                row["revoked"] is not True
                or progress.get("phase") != "revoked"
                or progress.get("complete") is not True
            ):
                raise LifecycleRefused(
                    "bootstrap authority recovery is still outstanding"
                )
            anchor = {
                "generation": row["generation"],
                "operation_id": row["operation_id"],
                "claim": row["claim"],
                "plan_sha256": digest(row["plan_json"]),
                "progress_sha256": digest(row["progress_json"]),
            }
            anchors.append(anchor)
            if row["operation_id"] == operation_id and row["claim"] == claim:
                if (
                    progress.get("retain_workspace") is not True
                    or progress.get("component_inventory_complete") is not True
                ):
                    raise LifecycleRefused(
                        "bootstrap result lacks completed retained component ownership"
                    )
                current.append(anchor)
        if len(current) != 1:
            raise LifecycleRefused(
                "bootstrap result has no unique original authority generation"
            )
    return {
        "registration": registration,
        "authority": anchors,
        "current_generation": current[0]["generation"],
    }


async def bootstrap_result_anchor(operation, context, outcome):
    """Capture only a successful canonical result before publishing its artifact."""
    from superplane_bootstrap.registry import _target_mapping
    from superplane_bootstrap.state import claim_fingerprint

    if not outcome.ready or outcome.reservation is None:
        raise LifecycleRefused("only canonical bootstrap readiness can anchor a result")
    lease = operation.grant.lease
    return await read_bootstrap_anchor(
        context,
        operation_id=lease.operation_id,
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        registration=_target_mapping(outcome.registration.target),
        claim=claim_fingerprint(outcome.reservation.attempt_token),
    )
