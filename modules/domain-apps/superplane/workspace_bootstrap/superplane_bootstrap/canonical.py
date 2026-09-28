"""Publish verified bootstrap metadata to the domain's canonical tenant records.

Runs inside the reservation transaction; any conflict rolls back publication and
leaves the claim reserved. Credential references are opaque, never credentials.

## Shared placement (issue #6048)

`identity["cluster_placement"]` is an OPTIONAL key. Every caller before this
change omits it; `.get(...)` below defaults the absence to ``"dedicated"``, and
the dedicated path is byte-for-byte the original behavior this module always had
— same locks, same reads, same refusals, same writes. This is what keeps this an
additive change rather than a rewrite: nothing about how a dedicated workspace
gets registered is different.

The gap this closes is named explicitly in
`../../executor/ORG-SHARED-CLUSTERS.md`: canonical registration refused ANY
second workspace naming an already-bound cluster, with no way to express "this
one is explicitly shared." The `"shared"` branch below is admitted only onto a
cluster that (a) already exists and is registered and (b) is explicitly marked
`clusters.sharing_enabled` — never onto a cluster this call would otherwise
create (a first member cannot bootstrap as "shared"; sharing requires something
already there to share), and never by inferring eligibility from the cluster's
name or AWS account. A `"dedicated"` identity is refused under EXACTLY the
conditions it always was.

The shared branch never overwrites `clusters.workspace_id` or the cluster's
`actual_state_json["workspace_bootstrap"]` key — both are single-owner-shaped
by the module's original design (see `observations.py`'s docstring on why only
`workspaces.cluster_id`/dedicated ownership can answer "who owns this cluster"),
and a second member's own registration must not corrupt the first member's
replay check. A shared member's registration is durable in its own
`cluster_memberships` row instead — the schema issue #6048 adds specifically
because a single JSON key cannot hold more than one workspace's binding safely.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from .errors import BootstrapRefused
from .membership import registration_membership

_DEDICATED = "dedicated"
_SHARED = "shared"


def _membership_generation(identity: dict) -> str:
    """Retain the approved generation; fingerprint historical unreserved records.

    Distinct from the execution-authority `generation`
    `authority_journal.generation_for` derives from a live attempt token: that one
    changes every attempt by design, so it cannot also be the value a replay of
    the SAME publish call needs to reproduce unchanged. This one only changes
    when the registered identity itself changes.
    """
    approved = registration_membership(identity)
    if approved is not None:
        return approved.generation
    canonical = json.dumps(dict(identity), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def require_active_shared_membership(store, identity):
    """A retained registration is evidence, not authority to revive a removed member."""
    if identity.get("cluster_placement") != _SHARED:
        return
    rows = store.execute(
        "SELECT m.generation,m.namespace,m.namespace_uid,m.credential_reference_id, "
        "c.id AS cluster_id,c.eks_cluster_arn,c.endpoint,w.namespace_name,m.operation_id "
        "FROM cluster_memberships m JOIN workspaces w ON w.id=m.workspace_id "
        "AND w.org_id=m.org_id AND w.cluster_id=m.cluster_id "
        "JOIN clusters c ON c.id=m.cluster_id AND c.org_id=m.org_id "
        "JOIN organizations o ON o.id=m.org_id "
        "WHERE m.workspace_id=CAST(:workspace_id AS uuid) AND m.state='active' "
        "AND (o.adp_org_id=:org OR CAST(o.id AS text)=:org) "
        "AND c.sharing_enabled AND c.status IN ('Ready','Active')",
        {"workspace_id": identity["workspace_id"], "org": identity["org_id"]},
    )
    expected = {
        "generation": _membership_generation(identity),
        "namespace": identity["namespace"],
        "namespace_name": identity["namespace"],
        "namespace_uid": identity["namespace_uid"],
        "credential_reference_id": identity["credential_reference_id"],
        "eks_cluster_arn": identity["cluster_arn"],
        "endpoint": identity["endpoint"],
    }
    approved = registration_membership(identity)
    if approved is not None:
        if len(rows) != 1 or (
            str(rows[0]["cluster_id"]),
            str(rows[0]["operation_id"]),
        ) != (approved.cluster_id, approved.request_id):
            raise BootstrapRefused(
                "shared registration no longer has its approved membership"
            )
    if len(rows) != 1 or any(rows[0][key] != value for key, value in expected.items()):
        raise BootstrapRefused(
            "shared registration no longer has its active membership"
        )


def require_shared_bootstrap_complete(store, identity, claim):
    """Check activation under the reservation lock also used to start recovery."""
    approved = registration_membership(identity)
    if approved is None:
        return  # Historical registrations predate admitted membership bootstrap.
    rows = store.execute(
        "SELECT generation,operation_id,org_id,cluster_arn,plan_json,progress_json,revoked "
        "FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id "
        "AND claim=:claim FOR UPDATE",
        {"workspace_id": identity["workspace_id"], "claim": claim},
    )
    if len(rows) != 1:
        raise BootstrapRefused("shared publication has no unique original authority")
    row = rows[0]
    plan, progress = json.loads(row["plan_json"]), json.loads(row["progress_json"])
    generation = hashlib.sha256(
        (row["operation_id"] + ":" + claim).encode()
    ).hexdigest()
    if (
        row["generation"] != generation
        or row["org_id"] != identity["org_id"]
        or row["cluster_arn"] != identity["cluster_arn"]
        or row["revoked"] is not True
        or plan.get("mode") != "shared-namespace"
        or plan.get("membership") != approved.encode()
        or progress.get("member_recovery_started")
        or progress.get("phase") != "revoked"
        or progress.get("complete") is not True
        or progress.get("retain_workspace") is not True
        or progress.get("component_inventory_complete") is not True
        or progress.get("component_inventory_mode") != "shared-namespace"
        or progress.get("member_gate") != "open"
        or progress.get("member_gate_intent") is not None
        or progress.get("workspace-namespace", {}).get("identity", {}).get("uid")
        != identity["namespace_uid"]
        or progress.get("member_credential_reference")
        != identity["credential_reference_id"]
    ):
        raise BootstrapRefused(
            "shared publication authority is incomplete or recovered"
        )


def publish(store, identity):
    workspace_id = str(UUID(identity["workspace_id"]))
    org_binding = identity["org_id"]
    cluster_placement = identity.get("cluster_placement") or _DEDICATED
    if cluster_placement not in (_DEDICATED, _SHARED):
        raise BootstrapRefused(
            f"unknown cluster_placement {cluster_placement!r}; expected "
            f"{_DEDICATED!r} or {_SHARED!r}"
        )
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
        "SELECT id, org_id, workspace_id, eks_cluster_arn, endpoint, actual_state_json, "
        "sharing_enabled, status FROM clusters "
        "WHERE eks_cluster_arn = :arn OR id = CAST(:cluster_id AS uuid) FOR UPDATE",
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

    if cluster_placement == _SHARED:
        approved = registration_membership(identity)
        if cluster is None:
            raise BootstrapRefused(
                "shared cluster placement requires an already-registered cluster; "
                "a first member cannot bootstrap a cluster as shared"
            )
        if approved is not None and (
            approved.org_id,
            approved.workspace_id,
            approved.cluster_id,
        ) != (org_id, workspace_id, str(cluster["id"])):
            raise BootstrapRefused(
                "approved membership names another canonical cluster"
            )
        if str(cluster["org_id"]) != org_id:
            raise BootstrapRefused(
                "canonical cluster is bound to another target or tenant"
            )
        if cluster["status"] not in {"Ready", "Active"}:
            raise BootstrapRefused("shared cluster is no longer ready for membership")
        if not cluster["sharing_enabled"]:
            raise BootstrapRefused(
                "cluster is not explicitly marked sharing_enabled; shared "
                "placement is never inferred from adoption or dedicated use"
            )
        if (
            cluster["eks_cluster_arn"] != identity["cluster_arn"]
            or cluster["endpoint"] != identity["endpoint"]
        ):
            raise BootstrapRefused(
                "shared cluster identity does not match the reviewed target"
            )
        # The cluster row itself is untouched: `workspace_id` and
        # `actual_state_json["workspace_bootstrap"]` remain whichever workspace's
        # dedicated/first registration set them, per the module docstring. This
        # workspace's own binding is recorded in `cluster_memberships` instead.
        #
        # `SELECT ... FOR UPDATE` first, then branch, rather than `INSERT ...
        # ON CONFLICT` — matching this function's existing idiom above, and so a
        # conflicting case (this workspace already bound elsewhere; another
        # workspace already holds this namespace) is a clean `BootstrapRefused`
        # rather than a raw database integrity error surfacing from a commit.
        removed = store.execute(
            "SELECT id FROM cluster_memberships WHERE org_id=CAST(:org_id AS uuid) "
            "AND workspace_id=CAST(:workspace_id AS uuid) AND state='removed' "
            "AND generation=:generation FOR UPDATE",
            {
                "org_id": org_id,
                "workspace_id": workspace_id,
                "generation": _membership_generation(identity),
            },
        )
        if removed:
            raise BootstrapRefused("removed membership cannot be reactivated by replay")
        existing_memberships = store.execute(
            "SELECT id, cluster_id, workspace_id, namespace, namespace_uid, generation, credential_reference_id, state, operation_id FROM cluster_memberships "
            "WHERE org_id = CAST(:org_id AS uuid) "
            "AND (workspace_id = CAST(:workspace_id AS uuid) "
            "OR (cluster_id = CAST(:cluster_id AS uuid) AND namespace = :namespace)) "
            "AND state <> 'removed' FOR UPDATE",
            {
                "org_id": org_id,
                "workspace_id": workspace_id,
                "cluster_id": str(cluster["id"]),
                "namespace": identity["namespace"],
            },
        )
        own_membership = next(
            (
                row
                for row in existing_memberships
                if str(row["workspace_id"]) == workspace_id
            ),
            None,
        )
        for row in existing_memberships:
            if str(row["workspace_id"]) == workspace_id:
                if str(row["cluster_id"]) != str(cluster["id"]):
                    raise BootstrapRefused(
                        "this workspace already holds a live membership on a "
                        "different cluster; cluster migration is a separate "
                        "explicit operation, not an implicit rebind"
                    )
            elif row["namespace"] == identity["namespace"]:
                raise BootstrapRefused(
                    "canonical bootstrap credential or namespace binding differs"
                )
        if approved is not None:
            if own_membership is None or (
                own_membership["generation"],
                own_membership["namespace"],
                str(own_membership["operation_id"]),
            ) != (approved.generation, approved.namespace, approved.request_id):
                raise BootstrapRefused(
                    "approved shared membership reservation is missing or changed"
                )
        elif own_membership and own_membership["state"] == "reserved":
            raise BootstrapRefused(
                "reserved shared membership requires its approved identity"
            )
        if own_membership and own_membership["state"] == "active":
            expected = {
                "namespace": identity["namespace"],
                "namespace_uid": identity["namespace_uid"],
                "credential_reference_id": identity["credential_reference_id"],
                "generation": _membership_generation(identity),
            }
            if any(own_membership[key] != value for key, value in expected.items()):
                raise BootstrapRefused("active membership immutable identity changed")
        # The workspace row must exist BEFORE the membership row: `cluster_memberships
        # .workspace_id` is a foreign key into `workspaces`, and a workspace created by
        # this same bootstrap run (the "no prior admitted workspace row" case) does not
        # exist yet until this statement runs.
        if workspace:
            store.execute(
                "UPDATE workspaces SET status = 'active', cluster_id=CAST(:cluster_id AS uuid), "
                "shared_cluster_id=CAST(:cluster_id AS uuid), namespace_name=:namespace, updated_at = now() "
                "WHERE id = CAST(:workspace_id AS uuid) AND org_id = CAST(:org_id AS uuid)",
                {
                    "workspace_id": workspace_id,
                    "org_id": org_id,
                    "cluster_id": str(cluster["id"]),
                    "namespace": identity["namespace"],
                },
            )
        else:
            store.execute(
                "INSERT INTO workspaces (id, org_id, name, isolation_mode, "
                "cluster_id, shared_cluster_id, namespace_name, status, is_default) VALUES "
                "(CAST(:workspace_id AS uuid), CAST(:org_id AS uuid), :namespace, "
                "'namespace', CAST(:cluster_id AS uuid), CAST(:cluster_id AS uuid), :namespace, 'active', false)",
                {
                    "workspace_id": workspace_id,
                    "org_id": org_id,
                    "cluster_id": str(cluster["id"]),
                    "namespace": identity["namespace"],
                },
            )
        membership_params = {
            "id": str(own_membership["id"]) if own_membership else str(uuid4()),
            "org_id": org_id,
            "workspace_id": workspace_id,
            "cluster_id": str(cluster["id"]),
            "generation": _membership_generation(identity),
            "namespace": identity["namespace"],
            "namespace_uid": identity.get("namespace_uid") or None,
            "credential_reference_id": identity.get("credential_reference_id") or None,
        }
        if own_membership:
            store.execute(
                "UPDATE cluster_memberships SET generation = :generation, "
                "namespace = :namespace, namespace_uid = :namespace_uid, "
                "state = 'active', credential_reference_id = :credential_reference_id, "
                "updated_at = now() WHERE id = CAST(:id AS uuid)",
                membership_params,
            )
        else:
            store.execute(
                "INSERT INTO cluster_memberships "
                "(id, org_id, workspace_id, cluster_id, generation, namespace, "
                "namespace_uid, state, credential_reference_id) "
                "VALUES (CAST(:id AS uuid), CAST(:org_id AS uuid), "
                "CAST(:workspace_id AS uuid), CAST(:cluster_id AS uuid), :generation, "
                ":namespace, :namespace_uid, 'active', :credential_reference_id)",
                membership_params,
            )
        return

    # --- dedicated placement: the module's original behavior, unchanged ---
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
