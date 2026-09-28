"""The cleanup boundary — Issue #5533 (w6-10), AC-02.

AC-02 verbatim: "BYOC cleanup preserves the cluster and unrelated workloads; managed
mode publishes exact provider-bound target identities."

These tests read a plan rather than observing a teardown, which is the whole reason
`plan_cleanup` returns a description instead of deleting. A function that both decided
and deleted could not be tested for what it does NOT delete — and the dangerous case
here is not "deleted the wrong thing" but "deleted something adjacent".
"""

from __future__ import annotations

import dataclasses

import pytest
from superplane_bootstrap.components import ComponentInstallation, InstalledObject
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.inventory import (
    ADOPTED,
    ADP_CREATED,
    OwnedPrerequisite,
    PrerequisiteInventory,
    adopt_prerequisites,
)
from superplane_bootstrap.retire import plan_cleanup
from superplane_bootstrap.target import verify_target

from .conftest import CLUSTER_ARN, NAMESPACE, WORKSPACE_ID

NAMESPACE_UID = "namespace-uid-0001"


def _target(binding, provider_identity, observed_cluster, expected_target, ownership):
    return verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership=ownership,
        **expected_target,
    )


@pytest.fixture
def managed_target(binding, provider_identity, observed_cluster, expected_target):
    return _target(
        binding, provider_identity, observed_cluster, expected_target, "adp-created"
    )


@pytest.fixture
def byoc_target(binding, provider_identity, observed_cluster, expected_target):
    return _target(
        binding, provider_identity, observed_cluster, expected_target, "adopted"
    )


def _installation(*, namespace_owned: bool, uid: str = NAMESPACE_UID):
    return ComponentInstallation(
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
        namespace=NAMESPACE,
        namespace_uid=uid,
        namespace_owned=namespace_owned,
        objects=(
            InstalledObject(
                kind="Namespace", name=NAMESPACE, owned=namespace_owned, uid=uid
            ),
            InstalledObject(
                kind="CustomResourceDefinition",
                name="nodepools.superplane.ai",
                owned=False,
                shared=True,
            ),
        ),
    )


def _plan(*, target, installation, inventory=None):
    """Plan cleanup, defaulting the now-mandatory inventory to a minimal real one.

    `inventory` became REQUIRED with the F4 repair: it used to default to None, which
    meant a plan could be produced for a workspace whose out-of-Terraform access
    prerequisites were recorded nowhere — and a plan that omits them leaves an access
    path behind permanently, since nothing else in the system knows those objects exist.

    The default is a single ADOPTED access entry rather than an empty inventory, for two
    reasons. `adopt_prerequisites` refuses an empty one on purpose — bootstrap always
    establishes at least a scoped access entry, so "empty" means the record was not
    built, not that nothing was created. And ADOPTED is the default that cannot mask a
    bug in the tests below: an adopted prerequisite is never removable, so it contributes
    nothing to `remove_prerequisites`, and an assertion about what a plan deletes stays
    an assertion about the namespace. The prerequisite-specific tests further down pass
    their own inventory.
    """
    if inventory is None:
        # Keyed on the TARGET's workspace, not the installation's. The inventory is what
        # the prerequisite gate verified for the bound workspace, so it follows the
        # binding — and the mismatch tests below pass a deliberately foreign
        # installation, which would make an installation-keyed default refuse inside this
        # helper instead of inside `plan_cleanup`, hiding the check under test.
        inventory = adopt_prerequisites(
            workspace_id=target.workspace_id,
            prerequisites=[_prerequisite(ADOPTED, "default-access-entry")],
        )
    return plan_cleanup(target=target, installation=installation, inventory=inventory)


# --- BYOC: the cluster and unrelated workloads survive (AC-02) -------------------


def test_byoc_cleanup_never_removes_the_supplied_cluster(byoc_target):
    """ADP never took ownership of its VPC or EKS lifecycle, so it must not delete it."""
    plan = _plan(target=byoc_target, installation=_installation(namespace_owned=False))

    assert plan.preserves_cluster is True
    assert CLUSTER_ARN not in plan.remove_prerequisites
    assert plan.remove_namespace == ""


def test_byoc_cleanup_preserves_an_adopted_namespace(byoc_target):
    """It may hold workloads ADP knows nothing about."""
    plan = _plan(target=byoc_target, installation=_installation(namespace_owned=False))

    assert plan.remove_namespace == ""
    assert any(NAMESPACE in entry and "adopted" in entry for entry in plan.preserved)


def test_byoc_cleanup_states_that_unrelated_workloads_are_untouched(byoc_target):
    """The preservation is the evidence.

    An empty `preserved` list with a correct `remove` list would satisfy the letter of
    AC-02 while giving an operator no way to confirm the supplied cluster was
    considered and spared.
    """
    plan = _plan(target=byoc_target, installation=_installation(namespace_owned=False))

    assert any("outside" in entry and NAMESPACE in entry for entry in plan.preserved)


def test_byoc_cleanup_retains_an_owned_namespace_without_fenced_content_inventory(
    byoc_target,
):
    """Creating a namespace never establishes ownership of every object inside it."""
    plan = _plan(target=byoc_target, installation=_installation(namespace_owned=True))

    assert plan.remove_namespace == ""
    assert plan.retained_namespace == NAMESPACE
    assert plan.retained_namespace_uid == NAMESPACE_UID
    assert plan.preserves_cluster is True


# --- Managed mode ----------------------------------------------------------------


def test_managed_cleanup_leaves_the_cluster_to_terraform(managed_target):
    """An ADP-created cluster's lifecycle belongs to `infra/workspaces/`, not here."""
    plan = _plan(
        target=managed_target, installation=_installation(namespace_owned=True)
    )

    assert plan.preserves_cluster is True
    assert any("Terraform" in entry for entry in plan.preserved)


def test_managed_cleanup_keeps_the_exact_namespace_as_an_outstanding_obligation(
    managed_target,
):
    plan = _plan(
        target=managed_target, installation=_installation(namespace_owned=True)
    )

    assert plan.remove_namespace == ""
    assert plan.retained_namespace == NAMESPACE
    assert plan.retained_namespace_uid == NAMESPACE_UID
    assert any("cascading" in reason for reason in plan.preserved)


def test_the_plan_publishes_the_exact_provider_bound_identities(managed_target):
    """AC-02's second half: managed mode publishes exact provider-bound identities."""
    plan = _plan(
        target=managed_target, installation=_installation(namespace_owned=True)
    )

    assert plan.workspace_id == WORKSPACE_ID
    assert plan.cluster_arn == CLUSTER_ARN
    assert plan.cluster_ownership == "adp-created"


# --- CRDs are never deleted in either mode --------------------------------------


@pytest.mark.parametrize("ownership", ["adp-created", "adopted"])
def test_crds_are_never_in_a_cleanup_plan(
    binding, provider_identity, observed_cluster, expected_target, ownership
):
    """Cluster-scoped and shared. Deleting `nodepools.superplane.ai` removes every
    NodePool cluster-wide, including other workspaces' — unbounded harm, while
    leaving it costs nothing."""
    target = _target(
        binding, provider_identity, observed_cluster, expected_target, ownership
    )

    plan = _plan(target=target, installation=_installation(namespace_owned=True))

    assert "nodepools.superplane.ai" not in str(plan.remove_prerequisites)
    assert plan.remove_namespace == ""
    assert plan.retained_namespace == NAMESPACE
    assert any("nodepools.superplane.ai" in entry for entry in plan.preserved)


# --- uid preconditions -----------------------------------------------------------


def test_an_owned_namespace_without_a_recorded_uid_refuses_to_plan(managed_target):
    """A delete identified only by name could hit a different object — a namespace
    deleted and recreated by somebody else between runs. That is exactly the
    unrelated workload AC-02 protects."""
    with pytest.raises(BootstrapRefused, match="no recorded uid"):
        _plan(
            target=managed_target,
            installation=_installation(namespace_owned=True, uid=""),
        )


# --- Partial bootstrap is the normal input --------------------------------------


def test_a_bootstrap_that_created_nothing_plans_no_deletions(byoc_target):
    """Cleanup most often runs after a bootstrap that failed halfway."""
    plan = _plan(target=byoc_target, installation=_installation(namespace_owned=False))

    assert plan.deletes_nothing is True


def test_a_plan_from_a_mismatched_workspace_is_refused(managed_target):
    """A plan built from a mismatched pair could name one workspace's namespace
    under another's authority."""
    foreign = dataclasses.replace(
        _installation(namespace_owned=True), workspace_id="another-workspace"
    )

    with pytest.raises(BootstrapRefused, match="different workspaces"):
        _plan(target=managed_target, installation=foreign)


def test_a_plan_from_a_mismatched_cluster_is_refused(managed_target):
    foreign = dataclasses.replace(
        _installation(namespace_owned=True),
        cluster_arn=CLUSTER_ARN.replace("cluster/", "cluster/other-"),
    )

    with pytest.raises(BootstrapRefused, match="different cluster"):
        _plan(target=managed_target, installation=foreign)


# --- Prerequisite inventory ------------------------------------------------------


def _prerequisite(ownership: str, identifier: str = "access-entry-1"):
    return OwnedPrerequisite(
        kind="EksAccessEntry",
        identifier=identifier,
        workspace_id=WORKSPACE_ID,
        ownership=ownership,
        reason="scoped workspace access established outside Terraform",
    )


def test_adp_created_prerequisites_are_planned_for_removal(managed_target):
    """An object created outside Terraform and not recorded becomes permanent by
    accident: nobody deletes it because nobody knows it exists."""
    inventory = adopt_prerequisites(
        workspace_id=WORKSPACE_ID, prerequisites=[_prerequisite(ADP_CREATED)]
    )

    plan = plan_cleanup(
        target=managed_target,
        installation=_installation(namespace_owned=True),
        inventory=inventory,
    )

    assert "EksAccessEntry/access-entry-1" in plan.remove_prerequisites


def test_adopted_prerequisites_are_recorded_but_never_removed(byoc_target):
    """Something else created it and may still depend on it. Removing a pre-existing
    rule on a supplied cluster is the same class of harm as deleting its namespace."""
    inventory = adopt_prerequisites(
        workspace_id=WORKSPACE_ID, prerequisites=[_prerequisite(ADOPTED)]
    )

    plan = plan_cleanup(
        target=byoc_target,
        installation=_installation(namespace_owned=False),
        inventory=inventory,
    )

    assert plan.remove_prerequisites == ()
    assert any("access-entry-1" in entry for entry in plan.preserved)


def test_an_adopted_prerequisite_cannot_be_recorded_as_removable():
    """`removable` is computed from ownership, not stored, so it cannot be set wrong."""
    assert _prerequisite(ADOPTED).removable is False
    assert _prerequisite(ADP_CREATED).removable is True


def test_an_inventory_for_another_workspace_is_refused(managed_target):
    """A mixed inventory would let one workspace's cleanup remove another's access."""
    foreign = PrerequisiteInventory(
        workspace_id="another-workspace",
        prerequisites=(
            OwnedPrerequisite(
                kind="EksAccessEntry",
                identifier="x",
                workspace_id="another-workspace",
                ownership=ADP_CREATED,
                reason="r",
            ),
        ),
    )

    # The refusal names BOTH workspaces, so the regex pins that rather than the older
    # generic "different workspace" wording: an operator reading this needs to know which
    # inventory arrived and which one was expected, and a message that says only "a
    # different workspace" sends them looking through both.
    with pytest.raises(
        BootstrapRefused, match="belongs to workspace 'another-workspace'"
    ):
        plan_cleanup(
            target=managed_target,
            installation=_installation(namespace_owned=True),
            inventory=foreign,
        )


def test_an_inventory_mixing_workspaces_cannot_be_constructed():
    with pytest.raises(BootstrapRefused, match="different workspace"):
        PrerequisiteInventory(
            workspace_id=WORKSPACE_ID,
            prerequisites=(
                OwnedPrerequisite(
                    kind="EksAccessEntry",
                    identifier="x",
                    workspace_id="someone-else",
                    ownership=ADP_CREATED,
                    reason="r",
                ),
            ),
        )


def test_a_duplicated_prerequisite_is_refused():
    """A duplicate can carry two different ownerships, making removability ambiguous."""
    with pytest.raises(BootstrapRefused, match="recorded twice"):
        PrerequisiteInventory(
            workspace_id=WORKSPACE_ID,
            prerequisites=(
                _prerequisite(ADP_CREATED),
                _prerequisite(ADOPTED),
            ),
        )


def test_an_unattributed_prerequisite_cannot_be_constructed():
    """An unattributed prerequisite is one nobody will know to remove."""
    with pytest.raises(BootstrapRefused, match="reason is required"):
        OwnedPrerequisite(
            kind="EksAccessEntry",
            identifier="x",
            workspace_id=WORKSPACE_ID,
            ownership=ADP_CREATED,
            reason="",
        )


def test_an_unknown_prerequisite_ownership_is_refused():
    with pytest.raises(BootstrapRefused, match="unknown prerequisite ownership"):
        OwnedPrerequisite(
            kind="EksAccessEntry",
            identifier="x",
            workspace_id=WORKSPACE_ID,
            ownership="probably-ours",
            reason="r",
        )


def test_an_empty_inventory_is_refused_where_one_is_expected():
    """Bootstrap establishes at least a scoped access entry outside Terraform, so an
    empty inventory means the record was not built rather than that nothing exists."""
    with pytest.raises(BootstrapRefused, match="no prerequisites were recorded"):
        adopt_prerequisites(workspace_id=WORKSPACE_ID, prerequisites=[])
