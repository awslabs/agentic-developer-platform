"""Read current shared member credential identity from canonical domain storage."""

from datetime import UTC

from harness_jobs.identity import OperationRefused


async def credential_target(connection, *, workspace_id, org_id, scope):
    if scope not in {"reader", "mutator"}:
        raise OperationRefused("workspace credential scope is invalid")
    row = await connection.fetchrow(
        "SELECT m.org_id::text,m.workspace_id::text,m.cluster_id::text,c.eks_cluster_arn AS cluster_arn,"
        "m.generation,m.namespace,m.namespace_uid,k.service_account_uid,k.revision,k.expires_at,k.scope,c.platform_eligible "
        "FROM cluster_memberships m JOIN workspaces w ON w.id=m.workspace_id AND w.org_id=m.org_id "
        "AND w.cluster_id=m.cluster_id AND w.shared_cluster_id=m.cluster_id AND w.namespace_name=m.namespace "
        "JOIN clusters c ON c.id=m.cluster_id AND c.org_id=m.org_id "
        "JOIN membership_credentials k ON k.membership_id=m.id AND k.namespace_uid=m.namespace_uid "
        "WHERE m.workspace_id::text=$1 AND m.org_id::text=$2 AND m.state='active' "
        "AND c.sharing_enabled AND c.status IN ('Ready','Active') "
        "AND k.scope=$3 AND k.state='active' AND k.expires_at>clock_timestamp()",
        workspace_id,
        org_id,
        scope,
    )
    if row is None:
        raise OperationRefused("shared membership or scoped credential is unavailable")
    result = dict(row)
    eligible = result.pop("platform_eligible")
    result["expires_at"] = result["expires_at"].astimezone(UTC).isoformat()
    return result, eligible
