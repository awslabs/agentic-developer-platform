"""`canonical.publish`'s shared-placement branch, against real PostgreSQL — issue #6048.

`test_registry_postgres.py` already proves the dedicated path — including
`test_publication_reuses_selected_cluster_identity` and
`test_finalization_is_discoverable_through_controller_management`, both of which
exercise `canonical.publish` unmodified and still pass after this change (run
them alongside this file to see that). This file is the shared-placement
counterpart: it proves two workspaces land on one already-registered cluster
with distinct namespaces and memberships, and that the refusals named in
`../../executor/ORG-SHARED-CLUSTERS.md`'s acceptance list actually refuse.

Reuses the exact fixtures `test_registry_postgres.py` defines (`server`,
`schema_ddl`, `database`) via its module, so this file needs no new server
lifecycle and stays consistent with that file if the migration chain changes.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.registry import SqlRegistrationStore

from .conftest import ACCOUNT_ID, CLUSTER_ARN, ORG_ID, REGION, WORKSPACE_ID
from .test_registry_postgres import database, loop, schema_ddl, server  # noqa: F401

SECOND_WORKSPACE_ID = "55555555-5555-4555-8555-555555555555"
SHARED_CLUSTER_ID = "66666666-6666-4666-8666-666666666666"
SHARED_NAMESPACE_A = "shared-ws-a"
SHARED_NAMESPACE_B = "shared-ws-b"


class _SharedTarget:
    """A `WorkspaceTarget`-shaped record naming the SHARED cluster placement."""

    workspace_id = WORKSPACE_ID
    org_id = ORG_ID
    account_id = ACCOUNT_ID
    region = REGION
    cluster_name = "shared-cluster"
    cluster_arn = CLUSTER_ARN
    endpoint = "https://shared.example.invalid"
    namespace = SHARED_NAMESPACE_A
    namespace_uid = "namespace-uid-shared-a"
    cluster_ownership = "adp-created"
    credential_reference_id = "workspace-credential-shared-a"
    contract_version = "v1"
    cluster_placement = "shared"


def _second_member_target(**overrides):
    class _Target(_SharedTarget):
        workspace_id = SECOND_WORKSPACE_ID
        namespace = SHARED_NAMESPACE_B
        namespace_uid = "namespace-uid-shared-b"
        credential_reference_id = "workspace-credential-shared-b"

    for key, value in overrides.items():
        setattr(_Target, key, value)
    return _Target()


def _seed_shared_cluster(store, *, sharing_enabled: bool = True) -> None:
    """Insert an already-registered, explicitly shareable cluster and its org."""
    store.fetch(
        "INSERT INTO clusters (id, org_id, name, status, eks_cluster_arn, endpoint, "
        "sharing_enabled) VALUES ($1, $2, 'shared-cluster', 'Ready', $3, $4, $5)",
        UUID(SHARED_CLUSTER_ID),
        UUID(ORG_ID),
        CLUSTER_ARN,
        "https://shared.example.invalid",
        sharing_enabled,
    )


def _seed_second_workspace(store) -> None:
    store.fetch(
        "INSERT INTO workspaces (id, org_id, name, isolation_mode, status, is_default) "
        "VALUES ($1, $2, 'second-member', 'namespace', 'pending', false)",
        UUID(SECOND_WORKSPACE_ID),
        UUID(ORG_ID),
    )


def test_two_workspaces_share_one_cluster_with_distinct_namespaces_and_memberships(
    database,  # noqa: F811
):
    """The acceptance criterion from ORG-SHARED-CLUSTERS.md item 1.

    Two independent `reserve`/`finalize` sequences, each with its own
    `SqlRegistrationStore`, both targeting the SAME already-shareable cluster.
    Both succeed, each gets its own namespace and membership row, and neither
    disturbs the other's.
    """
    store = database()
    _seed_shared_cluster(store)
    registry = SqlRegistrationStore(store=store)

    identity_a = {
        "workspace_id": WORKSPACE_ID,
        "org_id": ORG_ID,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "cluster_name": "shared-cluster",
        "cluster_arn": CLUSTER_ARN,
        "namespace": SHARED_NAMESPACE_A,
    }
    claim_a = registry.reserve(WORKSPACE_ID, identity_a)
    registry.finalize(_SharedTarget(), str(claim_a["attempt_token"]))

    _seed_second_workspace(store)
    identity_b = {**identity_a, "workspace_id": SECOND_WORKSPACE_ID, "namespace": SHARED_NAMESPACE_B}
    claim_b = registry.reserve(SECOND_WORKSPACE_ID, identity_b)
    registry.finalize(_second_member_target(), str(claim_b["attempt_token"]))

    memberships = store.fetch(
        "SELECT workspace_id, cluster_id, namespace, state, credential_reference_id "
        "FROM cluster_memberships ORDER BY namespace"
    )
    assert len(memberships) == 2
    assert memberships[0]["namespace"] == SHARED_NAMESPACE_A
    assert str(memberships[0]["workspace_id"]) == WORKSPACE_ID
    assert str(memberships[0]["cluster_id"]) == SHARED_CLUSTER_ID
    assert memberships[0]["state"] == "active"
    assert memberships[0]["credential_reference_id"] == "workspace-credential-shared-a"

    assert memberships[1]["namespace"] == SHARED_NAMESPACE_B
    assert str(memberships[1]["workspace_id"]) == SECOND_WORKSPACE_ID
    assert str(memberships[1]["cluster_id"]) == SHARED_CLUSTER_ID
    assert memberships[1]["credential_reference_id"] == "workspace-credential-shared-b"

    # Exactly one cluster row exists — sharing does not fork the cluster's own
    # canonical identity; the cluster's `workspace_id` column is untouched by
    # either member (single-owner projection, unchanged by this feature).
    clusters = store.fetch("SELECT id, workspace_id FROM clusters")
    assert len(clusters) == 1
    assert clusters[0]["workspace_id"] is None

    workspaces = store.fetch(
        "SELECT id, status, cluster_id, shared_cluster_id FROM workspaces ORDER BY id"
    )
    assert {str(w["id"]) for w in workspaces} == {WORKSPACE_ID, SECOND_WORKSPACE_ID}
    for w in workspaces:
        assert w["status"] == "active"
        # Neither workspace's dedicated `cluster_id` projection is set by a shared
        # registration — that column remains the dedicated-path projection.
        assert w["cluster_id"] is None


def test_shared_placement_refuses_a_cluster_that_never_opted_into_sharing(database):  # noqa: F811
    """ORG-SHARED-CLUSTERS.md: "adopting an existing cluster alone does not opt
    into sharing." A registered cluster with `sharing_enabled=false` refuses a
    second member exactly as before this change."""
    store = database()
    _seed_shared_cluster(store, sharing_enabled=False)
    registry = SqlRegistrationStore(store=store)

    identity = {
        "workspace_id": WORKSPACE_ID,
        "org_id": ORG_ID,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "cluster_name": "shared-cluster",
        "cluster_arn": CLUSTER_ARN,
        "namespace": SHARED_NAMESPACE_A,
    }
    claim = registry.reserve(WORKSPACE_ID, identity)
    with pytest.raises(BootstrapRefused, match="sharing_enabled"):
        registry.finalize(_SharedTarget(), str(claim["attempt_token"]))


def test_shared_placement_refuses_a_first_member_with_no_existing_cluster(database):  # noqa: F811
    """A first member cannot bootstrap AS shared — there is nothing to share yet."""
    store = database()
    registry = SqlRegistrationStore(store=store)

    identity = {
        "workspace_id": WORKSPACE_ID,
        "org_id": ORG_ID,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "cluster_name": "shared-cluster",
        "cluster_arn": CLUSTER_ARN,
        "namespace": SHARED_NAMESPACE_A,
    }
    claim = registry.reserve(WORKSPACE_ID, identity)
    with pytest.raises(BootstrapRefused, match="already-registered cluster"):
        registry.finalize(_SharedTarget(), str(claim["attempt_token"]))


def test_shared_placement_refuses_a_namespace_already_live_on_the_cluster(database):  # noqa: F811
    """Two members cannot collide on namespace — DESIGN.md's isolation boundary."""
    store = database()
    _seed_shared_cluster(store)
    registry = SqlRegistrationStore(store=store)

    identity_a = {
        "workspace_id": WORKSPACE_ID,
        "org_id": ORG_ID,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "cluster_name": "shared-cluster",
        "cluster_arn": CLUSTER_ARN,
        "namespace": SHARED_NAMESPACE_A,
    }
    claim_a = registry.reserve(WORKSPACE_ID, identity_a)
    registry.finalize(_SharedTarget(), str(claim_a["attempt_token"]))

    _seed_second_workspace(store)
    # Second workspace requests the SAME namespace name the first already holds.
    identity_b = {**identity_a, "workspace_id": SECOND_WORKSPACE_ID}
    claim_b = registry.reserve(SECOND_WORKSPACE_ID, identity_b)
    colliding_target = _second_member_target(namespace=SHARED_NAMESPACE_A)
    with pytest.raises(BootstrapRefused, match="namespace binding differs"):
        registry.finalize(colliding_target, str(claim_b["attempt_token"]))


def test_shared_placement_refuses_a_foreign_organizations_cluster(database):  # noqa: F811
    """Cross-organization selection is refused even when the cluster is Ready and shareable."""
    store = database()
    _seed_shared_cluster(store)
    store.fetch(
        "INSERT INTO organizations (id, name, adp_org_id, billing_plan) "
        "VALUES ($1, 'other-org', 'other-org', 'free')",
        UUID("77777777-7777-4777-8777-777777777777"),
    )
    store.fetch(
        "INSERT INTO workspaces (id, org_id, name, isolation_mode, status, is_default) "
        "VALUES ($1, $2, 'foreign', 'namespace', 'pending', false)",
        UUID(SECOND_WORKSPACE_ID),
        UUID("77777777-7777-4777-8777-777777777777"),
    )
    registry = SqlRegistrationStore(store=store)

    identity = {
        "workspace_id": SECOND_WORKSPACE_ID,
        "org_id": "77777777-7777-4777-8777-777777777777",
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "cluster_name": "shared-cluster",
        "cluster_arn": CLUSTER_ARN,
        "namespace": SHARED_NAMESPACE_B,
    }
    claim = registry.reserve(SECOND_WORKSPACE_ID, identity)
    foreign_target = _second_member_target(
        org_id="77777777-7777-4777-8777-777777777777"
    )
    with pytest.raises(BootstrapRefused, match="another target or tenant"):
        registry.finalize(foreign_target, str(claim["attempt_token"]))


def test_dedicated_placement_is_unaffected_by_the_shared_branch(database):  # noqa: F811
    """The default (omitted `cluster_placement`) still refuses a second workspace
    binding to an already dedicated-bound cluster — the original F5 behavior,
    proving this change is additive rather than a loosening."""
    from .test_registry_postgres import RESERVATION_IDENTITY, _Target

    store = database()
    registry = SqlRegistrationStore(store=store)
    claim_a = registry.reserve(WORKSPACE_ID, RESERVATION_IDENTITY)
    registry.finalize(_Target(), str(claim_a["attempt_token"]))

    _seed_second_workspace(store)
    identity_b = {**RESERVATION_IDENTITY, "workspace_id": SECOND_WORKSPACE_ID}
    claim_b = registry.reserve(SECOND_WORKSPACE_ID, identity_b)

    class _DedicatedSecond(_Target):
        workspace_id = SECOND_WORKSPACE_ID

    with pytest.raises(BootstrapRefused, match="bound to another target or tenant"):
        registry.finalize(_DedicatedSecond(), str(claim_b["attempt_token"]))
