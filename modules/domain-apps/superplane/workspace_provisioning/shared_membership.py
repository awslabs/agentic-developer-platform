"""Domain reservation and fresh checks for an approved shared-cluster membership."""

from uuid import UUID, uuid5

from superplane_bootstrap.membership import SharedMembership

from .runtime_config import LifecycleRefused

MEMBERSHIP_NAMESPACE = UUID("e5daf236-4cde-4b64-bd8f-0d5fdc8f020b")


async def reserve(connection, binding: SharedMembership):
    """Caller owns the workspace-insertion transaction; no provider effect runs here."""
    cluster = await connection.fetchrow(
        "SELECT id,org_id,eks_cluster_arn,endpoint,sharing_enabled,status FROM clusters "
        "WHERE id::text=$1 AND org_id::text=$2 FOR UPDATE",
        binding.cluster_id,
        binding.org_id,
    )
    if (
        cluster is None
        or not cluster["sharing_enabled"]
        or cluster["status"] not in {"Ready", "Active"}
        or cluster["eks_cluster_arn"] != binding.cluster_arn
        or cluster["endpoint"] != binding.endpoint
    ):
        raise LifecycleRefused("approved shared cluster is no longer eligible")
    workspace = await connection.fetchrow(
        "SELECT cluster_id::text,namespace_name FROM workspaces WHERE id::text=$1 AND org_id::text=$2 FOR UPDATE",
        binding.workspace_id,
        binding.org_id,
    )
    if (
        workspace is None
        or workspace["cluster_id"] not in {None, binding.cluster_id}
        or workspace["namespace_name"] not in {None, binding.namespace}
    ):
        raise LifecycleRefused("workspace already has another cluster or namespace")
    member_id = str(uuid5(MEMBERSHIP_NAMESPACE, binding.generation))
    await connection.execute(
        "INSERT INTO cluster_memberships(id,org_id,workspace_id,cluster_id,generation,namespace,state,operation_id) "
        "VALUES($1::text::uuid,$2::text::uuid,$3::text::uuid,$4::text::uuid,$5,$6,'reserved',$7::text::uuid) "
        "ON CONFLICT(id) DO NOTHING",
        member_id,
        binding.org_id,
        binding.workspace_id,
        binding.cluster_id,
        binding.generation,
        binding.namespace,
        binding.request_id,
    )
    await connection.execute(
        "UPDATE workspaces SET cluster_id=$3::text::uuid,shared_cluster_id=$3::text::uuid,namespace_name=$4 "
        "WHERE id::text=$1 AND org_id::text=$2",
        binding.workspace_id,
        binding.org_id,
        binding.cluster_id,
        binding.namespace,
    )
    await verify(connection, binding, states={"reserved", "active"})


async def verify(connection, binding: SharedMembership, *, states):
    row = await connection.fetchrow(
        "SELECT m.generation,m.namespace,m.state,m.operation_id::text,c.eks_cluster_arn,c.endpoint,w.namespace_name "
        "FROM cluster_memberships m JOIN clusters c ON c.id=m.cluster_id AND c.org_id=m.org_id "
        "JOIN workspaces w ON w.id=m.workspace_id AND w.org_id=m.org_id AND w.cluster_id=m.cluster_id "
        "WHERE m.workspace_id::text=$1 AND m.org_id::text=$2 AND m.cluster_id::text=$3 AND m.generation=$4",
        binding.workspace_id,
        binding.org_id,
        binding.cluster_id,
        binding.generation,
    )
    if (
        row is None
        or row["state"] not in states
        or any(
            row[key] != value
            for key, value in {
                "namespace": binding.namespace,
                "namespace_name": binding.namespace,
                "operation_id": binding.request_id,
                "eks_cluster_arn": binding.cluster_arn,
                "endpoint": binding.endpoint,
            }.items()
        )
    ):
        raise LifecycleRefused("shared workspace membership changed or was withdrawn")
    return row
