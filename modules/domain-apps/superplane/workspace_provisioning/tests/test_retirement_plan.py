"""The composed plan is ordered, narrow, and refuses rather than truncating.

The first test composes from a REAL bootstrap's durable ownership (the `runtime`
fixture runs it against disposable PostgreSQL), so the plan under assertion is built
from records a bootstrap actually wrote rather than from a hand-built inventory. The
remaining tests build inventories directly to reach the refusal and preservation
branches a successful bootstrap does not produce.
"""

import json
from dataclasses import replace

import pytest

# The AUTHORITATIVE parser and bounds, not this package's structural copy — see
# `tests/__init__.py`. Asserting the composed plan re-parses through the real validator
# is what makes "approval covers exactly what execution reads back" evidence rather
# than a claim checked against a second implementation of the same rules.
from harness_jobs.execution_descriptors import (
    MAX_DESCRIPTOR_VALUE_LENGTH,
    parse_execution_steps,
)
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.inventory import OwnedPrerequisite

from workspace_provisioning.retirement_inventory import (
    ComponentOwnership,
    OwnedGrant,
    RetirementInventory,
)
from workspace_provisioning.retirement_plan import (
    BLOCK_ADMISSION,
    DELETE_COMPONENT,
    DELETE_NAMESPACE,
    DRAIN_WORKLOADS,
    REVOKE_GRANT,
    REVOKE_PREREQUISITE,
    UNREGISTER,
    VERIFY_RESOURCES,
    compose_retirement_plan,
)

from .test_retirement_inventory import load


def component(name, *, kind="ServiceAccount", owned=True, namespace="ws"):
    return ComponentOwnership(
        {"kind": kind, "metadata": {"name": name, "namespace": namespace}},
        {"uid": f"uid-{name}", "digest": "a" * 64},
        owned,
    )


def inventory(**overrides):
    base = {
        "workspace_id": "18563dce-15e9-4c58-8824-ff78744085e4",
        "org_id": "92d4ae6b-c212-4d1f-a50a-7d5a0c7d8870",
        "cluster_arn": "arn:aws:eks:us-east-1:879318057152:cluster/spw",
        "cluster_ownership": "adp-created",
        "namespace": "ws",
        "namespace_uid": "ns-uid",
        "remove_namespace": True,
        "grants": (),
        "prerequisites": (),
        "components": (),
        "components_complete": True,
    }
    base.update(overrides)
    return RetirementInventory(**base)


def kinds(plan):
    return [step.operation_kind for step in plan.steps]


def descriptors(steps):
    """Steps as plain field tuples, so the two descriptor classes can be compared.

    `parse_execution_steps` returns the AUTHORITATIVE `ExecutionStep`, while the plan
    holds this package's structural copy (see `execution_contract.py`). They are
    distinct classes, so dataclass equality is always False between them regardless of
    content — comparing field values is what actually asserts the round trip.
    """
    return [
        (step.step_id, step.provider, step.operation_kind, step.target)
        for step in steps
    ]


def test_plan_from_a_real_bootstrap_orders_drain_and_unregister_before_deletion(
    runtime,
):
    assert runtime.run().ready
    plan = compose_retirement_plan(load(runtime))

    # The gate, the drain and the withdrawal precede every deletion. Asserted as
    # positions rather than membership: a plan containing them in the wrong order
    # would satisfy a membership check and still delete under live admission.
    assert kinds(plan)[:3] == [BLOCK_ADMISSION, DRAIN_WORKLOADS, UNREGISTER]
    assert kinds(plan)[-1] == VERIFY_RESOURCES
    deletions = [plan.steps.index(step) for step in plan.deletion_steps()]
    assert deletions and min(deletions) > kinds(plan).index(UNREGISTER)

    # Access revocation precedes network revocation. Namespace deletion is not
    # authorized by this journal: unenumerated/adopted contents must survive.
    order = [step.operation_kind for step in plan.deletion_steps()]
    assert DELETE_NAMESPACE not in order
    assert min(
        index for index, kind in enumerate(order) if kind == REVOKE_PREREQUISITE
    ) > max(index for index, kind in enumerate(order) if kind == REVOKE_GRANT)

    # This bootstrap fixture records no component journal, so no component deletion
    # is planned and the plan says so rather than implying a complete teardown.
    assert DELETE_COMPONENT not in kinds(plan)
    assert not plan.completes_teardown
    assert any("cannot be reported" in entry for entry in plan.preserved)

    # Every step is uniquely identified and re-parses from the approved encoding, so
    # what approval covers is exactly what execution will read back.
    assert len({step.step_id for step in plan.steps}) == len(plan.steps)
    assert descriptors(parse_execution_steps(plan.encode())) == descriptors(plan.steps)


def test_supplied_cluster_and_network_are_preserved_and_stated(runtime):
    assert runtime.run().ready
    adopted = replace(load(runtime), cluster_ownership="adopted")
    plan = compose_retirement_plan(adopted)
    assert plan.preserves_cluster
    assert any(
        adopted.cluster_arn in entry and "never" in entry for entry in plan.preserved
    )
    # The cluster is never itself the resource a step removes. Checked on the parsed
    # descriptor rather than by substring, because a grant legitimately names the
    # cluster it was scoped to — "mentions the cluster" and "deletes the cluster" are
    # different facts and only the second one is forbidden.
    targets = [json.loads(step.target) for step in plan.deletion_steps()]
    assert targets
    assert not any(
        target.get("kind", "").lower() in {"cluster", "eks-cluster", "vpc"}
        or target.get("identifier", "") == adopted.cluster_arn
        for target in targets
    )
    # Account closure is stated as out of scope rather than silently absent.
    assert any(entry.startswith("Account (") for entry in plan.preserved)


def test_an_adopted_namespace_and_component_are_never_deleted():
    plan = compose_retirement_plan(
        inventory(
            remove_namespace=False,
            components=(component("kept", owned=False), component("ours")),
        )
    )
    assert DELETE_NAMESPACE not in kinds(plan)
    assert [step.target for step in plan.deletion_steps()] == [
        step.target for step in plan.steps if step.operation_kind == DELETE_COMPONENT
    ]
    assert "uid-ours" in plan.deletion_steps()[0].target
    assert any(
        "adopted" in entry and "ServiceAccount/kept" in entry
        for entry in plan.preserved
    )
    assert any("Namespace/ws" in entry for entry in plan.preserved)


@pytest.mark.parametrize("complete", [False, True])
def test_owned_namespace_cannot_cascade_into_adopted_or_unrecorded_contents(complete):
    plan = compose_retirement_plan(
        inventory(
            components=(component("tenant-owned", owned=False), component("ours")),
            components_complete=complete,
        )
    )
    assert DELETE_NAMESPACE not in kinds(plan)
    assert not plan.completes_teardown
    assert any("cascading" in reason for reason in plan.preserved)
    if complete:
        assert len([s for s in plan.steps if s.operation_kind == DELETE_COMPONENT]) == 1
    else:
        assert DELETE_COMPONENT not in kinds(plan)


def test_component_completion_does_not_retire_managed_infrastructure():
    plan = compose_retirement_plan(inventory(remove_namespace=False))
    assert plan.components_authorized
    assert not plan.completes_teardown
    assert any("Terraform" in reason for reason in plan.preserved)


def test_preserved_adopted_resources_are_not_owned_teardown_obligations():
    plan = compose_retirement_plan(
        inventory(cluster_ownership="adopted", remove_namespace=False)
    )
    assert plan.completes_teardown


def test_shared_cluster_scoped_grants_are_preserved_not_revoked():
    shared = OwnedGrant(
        {
            "kind": "kubernetes",
            "body": {"kind": "ClusterRoleBinding", "metadata": {"name": "shared"}},
        },
        {"uid": "crb-uid"},
    )
    scoped = OwnedGrant(
        {
            "kind": "kubernetes",
            "body": {
                "kind": "RoleBinding",
                "metadata": {"name": "scoped", "namespace": "ws"},
            },
        },
        {"uid": "rb-uid"},
    )
    plan = compose_retirement_plan(inventory(grants=(shared, scoped)))
    revocations = [step for step in plan.steps if step.operation_kind == REVOKE_GRANT]
    assert len(revocations) == 1
    assert "rb-uid" in revocations[0].target
    assert any(
        "shared" in entry and "other workspaces" in entry for entry in plan.preserved
    )


def test_an_adopted_prerequisite_is_preserved_and_an_owned_one_is_revoked():
    plan = compose_retirement_plan(
        inventory(
            prerequisites=(
                OwnedPrerequisite(
                    kind="SecurityGroupRule/private-sts",
                    identifier="sgr-shared",
                    workspace_id="18563dce-15e9-4c58-8824-ff78744085e4",
                    ownership="adopted",
                    reason="shared platform endpoint",
                ),
                OwnedPrerequisite(
                    kind="SecurityGroupRule/cluster-endpoint",
                    identifier="sgr-ours",
                    workspace_id="18563dce-15e9-4c58-8824-ff78744085e4",
                    ownership="adp-created",
                    reason="created for this workspace",
                ),
            )
        )
    )
    revocations = [
        step for step in plan.steps if step.operation_kind == REVOKE_PREREQUISITE
    ]
    assert len(revocations) == 1
    assert "sgr-ours" in revocations[0].target
    assert any("sgr-shared" in entry for entry in plan.preserved)


@pytest.mark.parametrize(
    "broken,match",
    [
        ({"namespace_uid": "  "}, "identified only by name"),
        (
            {
                "grants": (
                    OwnedGrant(
                        {
                            "kind": "kubernetes",
                            "body": {
                                "kind": "Role",
                                "metadata": {"name": "r", "namespace": "ws"},
                            },
                        },
                        {},
                    ),
                )
            },
            "immutable identity",
        ),
    ],
)
def test_unresolved_ownership_never_becomes_a_deletion_plan(broken, match):
    with pytest.raises(BootstrapRefused, match=match):
        compose_retirement_plan(inventory(**broken))


def test_ownership_beyond_the_approved_plan_bounds_is_refused_not_truncated():
    # A plan that cannot be expressed within the approved bounds must refuse: a
    # truncated deletion plan is precisely a teardown that reports completion while
    # leaving owned resources and their cost behind.
    with pytest.raises(BootstrapRefused, match="refusing to truncate"):
        compose_retirement_plan(
            inventory(components=tuple(component(f"c{i:03d}") for i in range(80)))
        )
    with pytest.raises(BootstrapRefused, match="refusing to truncate"):
        compose_retirement_plan(
            inventory(components=(component("x" * (MAX_DESCRIPTOR_VALUE_LENGTH + 1)),))
        )


def test_composing_requires_a_durable_inventory():
    with pytest.raises(BootstrapRefused, match="durable ownership inventory"):
        compose_retirement_plan({"workspace_id": "ws", "org_id": "org"})
