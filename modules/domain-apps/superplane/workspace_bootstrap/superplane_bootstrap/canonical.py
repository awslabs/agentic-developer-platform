"""Publish verified bootstrap metadata to the domain's canonical tenant records.

Runs inside the reservation transaction; any conflict rolls back publication and
leaves the claim reserved. Credential references are opaque, never credentials.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from .errors import BootstrapRefused


def publish(store, identity):
    workspace_id = str(UUID(identity["workspace_id"]))
    org_binding = identity["org_id"]
    store.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(:binding, 0))",
        {"binding": "superplane-bootstrap:" + org_binding},
    )
    orgs = store.execute(
        "SELECT id, adp_org_id FROM organizations "
        "WHERE adp_org_id = :org OR CAST(id AS text) = :org FOR UPDATE",
        {"org": org_binding},
    )
    if len(orgs) != 1 or not orgs[0]["adp_org_id"]:
        raise BootstrapRefused(
            "canonical registration requires an explicitly bound organization"
        )
    org_id = str(orgs[0]["id"])
    # Also serialize two organizations naming the same provider cluster.
    store.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(:binding, 0))",
        {"binding": "superplane-cluster:" + identity["cluster_arn"]},
    )
    workspaces = store.execute(
        "SELECT id, org_id, cluster_id, namespace_name FROM workspaces "
        "WHERE id = CAST(:workspace_id AS uuid) FOR UPDATE",
        {"workspace_id": workspace_id},
    )
    workspace = workspaces[0] if workspaces else None
    if workspace and (
        str(workspace["org_id"]) != org_id
        or workspace["namespace_name"] not in (None, identity["namespace"])
    ):
        raise BootstrapRefused(
            "canonical workspace belongs to another tenant or namespace"
        )
    clusters = store.execute(
        "SELECT id, org_id, workspace_id, eks_cluster_arn, endpoint, actual_state_json "
        "FROM clusters WHERE eks_cluster_arn = :arn OR id = CAST(:cluster_id AS uuid) FOR UPDATE",
        {
            "arn": identity["cluster_arn"],
            "cluster_id": str(workspace["cluster_id"])
            if workspace and workspace["cluster_id"]
            else None,
        },
    )
    if len(clusters) > 1:
        raise BootstrapRefused("canonical cluster identity is ambiguous")
    cluster = clusters[0] if clusters else None
    if cluster and (
        str(cluster["org_id"]) != org_id
        or cluster["eks_cluster_arn"] not in (None, identity["cluster_arn"])
        or cluster["endpoint"] not in (None, identity["endpoint"])
        or (cluster["workspace_id"] and str(cluster["workspace_id"]) != workspace_id)
    ):
        raise BootstrapRefused("canonical cluster is bound to another target or tenant")
    cluster_id = str(cluster["id"]) if cluster else str(uuid4())
    metadata = cluster["actual_state_json"] if cluster else {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    metadata = dict(metadata or {})
    prior = metadata.get("workspace_bootstrap")
    if prior and prior != identity:
        raise BootstrapRefused(
            "canonical bootstrap credential or namespace binding differs"
        )
    metadata["workspace_bootstrap"] = dict(identity)
    params = {
        "workspace_id": workspace_id,
        "org_id": org_id,
        "cluster_id": cluster_id,
        "name": identity["cluster_name"],
        "namespace": identity["namespace"],
        "arn": identity["cluster_arn"],
        "endpoint": identity["endpoint"],
        "metadata": json.dumps(metadata),
    }
    if cluster:
        store.execute(
            "UPDATE clusters SET workspace_id = CAST(:workspace_id AS uuid), "
            "eks_cluster_arn = :arn, endpoint = :endpoint, actual_state_json = CAST(:metadata AS jsonb), "
            "status = 'Ready', health_status = 'healthy', updated_at = now() "
            "WHERE id = CAST(:cluster_id AS uuid) AND org_id = CAST(:org_id AS uuid)",
            params,
        )
    else:
        store.execute(
            "INSERT INTO clusters (id, org_id, workspace_id, name, eks_cluster_arn, endpoint, "
            "actual_state_json, status, health_status, cloud_provider, cluster_type) VALUES "
            "(CAST(:cluster_id AS uuid), CAST(:org_id AS uuid), CAST(:workspace_id AS uuid), :name, "
            ":arn, :endpoint, CAST(:metadata AS jsonb), 'Ready', 'healthy', 'aws', 'eks')",
            params,
        )
    if workspace:
        store.execute(
            "UPDATE workspaces SET cluster_id = CAST(:cluster_id AS uuid), namespace_name = :namespace, "
            "status = 'active', updated_at = now() WHERE id = CAST(:workspace_id AS uuid) "
            "AND org_id = CAST(:org_id AS uuid)",
            params,
        )
    else:
        store.execute(
            "INSERT INTO workspaces (id, org_id, name, isolation_mode, cluster_id, namespace_name, "
            "status, is_default) VALUES (CAST(:workspace_id AS uuid), CAST(:org_id AS uuid), :namespace, "
            "'namespace', CAST(:cluster_id AS uuid), :namespace, 'active', false)",
            params,
        )
