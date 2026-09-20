"""Cleanup stays inside owned resources, and never closes an account implicitly.

Issue #5530 (w6-07). Covers AC-02's **cleanup outside owned resources** case and Design item
4's closing requirement: *account closure is never implicit in custom-resource or workspace
deletion.*

## The two legacy behaviours these tests invert

`06-teardown-account.sh` ran one `kubectl delete fullaccountinfrastructure <name>`. Because the
vendored graph owns the `Account` resource, that single command cascaded into deleting the AWS
account — and the script printed the 90-day-suspension consequence AFTER issuing the delete.
`test_a_workspace_cleanup_never_closes_an_account` and
`test_the_cascading_custom_resource_is_retained_by_name` are that behaviour, inverted.

`07-teardown-capabilities.sh` deleted shared CRDs, uninstalled the shared kro and ACK
controllers, deleted shared IAM roles, and removed an EKS access entry belonging to core ADP's
`github-runner-org` ARC runner role — with `2>/dev/null || true` on nearly every destructive
step, so a failed delete was reported as completion. The `_guard_action` tests are that
behaviour, inverted: out-of-scope resources raise, and there is no skip-with-warning path.

## What these tests deliberately do NOT establish

No delete is executed anywhere in this file. `plan` returns a plan as DATA; executing it is a
separate authorized operation. So these tests establish what the plan CONTAINS and what it
refuses to contain — not that any resource was removed, nor that a real cluster's state matches
a plan. Verifying an actual teardown is a live operation under the Wave 6 operations evaluator.
"""

from __future__ import annotations

import pytest
from account_factory.cleanup import (
    CleanupError,
    CleanupScope,
    DeleteAction,
    OwnershipEvidence,
    ProvisionedAccountRecord,
    closure_request,
    plan,
)
from account_factory.cleanup import _DECLARED_BY as DECLARED_BY
from account_factory.modes import ModeError, OwnershipMode
from account_factory.render import MANAGED_BY

from .conftest import (
    BUILDERS,
    FIXTURE_ORG_ID,
    FIXTURE_TARGET_ACCOUNT,
    FIXTURE_WORKSPACE,
    bring_existing_cluster_request,
    existing_account_request,
    matching_authorization,
    new_account_request,
)

# The account id a new-account-managed run is recorded as having created. Distinct from
# FIXTURE_TARGET_ACCOUNT, which is an ADOPTED account — keeping them different is what lets
# the closure tests tell "the recorded account" apart from "some other valid account".
FIXTURE_CREATED_ACCOUNT = "000000000003"


def owned_evidence(uid: str = "uid-fixture-0001", **overrides) -> OwnershipEvidence:
    """Evidence as a genuinely owned resource would report it."""
    fields = {
        "managed_by": MANAGED_BY,
        "workspace_label": FIXTURE_WORKSPACE,
        "uid": uid,
    }
    fields.update(overrides)
    return OwnershipEvidence(**fields)


def provisioned_record(**overrides) -> ProvisionedAccountRecord:
    """What provisioning recorded about the account it created."""
    fields = {
        "account_id": FIXTURE_CREATED_ACCOUNT,
        "workspace_id": FIXTURE_WORKSPACE,
        "organization_id": FIXTURE_ORG_ID,
    }
    fields.update(overrides)
    return ProvisionedAccountRecord(**fields)


def deleted_kinds(cleanup_plan) -> list[str]:
    return [action.kind for action in cleanup_plan.actions]


# ── A workspace cleanup never closes an account ──────────────────────────────────────


def test_a_workspace_cleanup_never_closes_an_account(any_mode_request):
    """True in every mode, including the one where ADP created the account."""
    cleanup_plan = plan(any_mode_request)
    assert cleanup_plan.scope is CleanupScope.WORKSPACE_RESOURCES
    assert cleanup_plan.closes_account is False
    assert cleanup_plan.describes_account_closure() is False


def test_the_account_owning_custom_resource_is_retained_by_name():
    """`AccountOwnership` owns the `Account`, so deleting it would close the account.

    The plan deletes the infrastructure root instead, and says why the account root is left.
    """
    cleanup_plan = plan(new_account_request())
    assert "AccountOwnership" not in deleted_kinds(cleanup_plan)
    assert "AccountOwnership" in cleanup_plan.retained_roots
    assert any("AccountOwnership" in retained for retained in cleanup_plan.retained)
    assert any("90-day" in retained for retained in cleanup_plan.retained)


def test_no_account_object_is_ever_in_a_delete_plan(any_mode_request):
    assert "Account" not in deleted_kinds(plan(any_mode_request))


def test_a_plan_states_what_it_deliberately_retains(any_mode_request):
    """A plan that silently omits something cannot be distinguished from one that forgot it."""
    cleanup_plan = plan(any_mode_request)
    assert cleanup_plan.retained
    assert all(retained.strip() for retained in cleanup_plan.retained)


# ── Per-mode delete boundary ─────────────────────────────────────────────────────────


def test_adp_created_infrastructure_is_deleted_in_the_managed_modes():
    """The infrastructure ROOT is deleted, which is what removes the VPC and cluster.

    Deleting the root rather than the two child stacks is the AF-002 repair: the stacks are
    DECLARED by the root, so removing them while it survives is a request that kro rebuild
    them, not a deletion. The convergence tests below assert the general property.
    """
    for build in (new_account_request, existing_account_request):
        kinds = deleted_kinds(plan(build()))
        assert "WorkspaceInfrastructure" in kinds
        # And specifically not the children, whose deletion would not converge.
        assert "EKSClusterStack" not in kinds
        assert "NetworkStack" not in kinds


# ── AF-002: a plan has to converge ───────────────────────────────────────────────────
#
# These lock a specific regression. Before the repair, the new-account plan deleted
# `NetworkStack` and `EKSClusterStack` while retaining the `FullAccountInfrastructure` that
# declares both. Executing it would have deleted the VPC and the cluster and then had a
# controller rebuild them — a teardown that reports success and then recreates the spend it
# removed, with no error anywhere to say so.


def test_no_plan_deletes_anything_a_retained_root_still_declares(any_mode_request):
    """The property, asserted directly, for every mode.

    Stated as "nothing deleted is declared by anything retained" rather than as an expected
    list of kinds, so an edit that retains a declaring root fails here even though no test
    named that particular combination.
    """
    cleanup_plan = plan(any_mode_request)
    deleted = set(deleted_kinds(cleanup_plan))
    for root in cleanup_plan.retained_roots:
        reconciled_back = DECLARED_BY[root] & deleted
        assert not reconciled_back, (
            f"{any_mode_request.mode.value}: deleting {sorted(reconciled_back)} while "
            f"retaining {root}, which declares them, would have them rebuilt"
        )


def test_a_non_converging_plan_is_refused():
    """The guard itself, driven with the exact pre-repair combination.

    Without this, `_check_convergence` could be deleted and every other test here would still
    pass — the plans it protects happen to be correct now.
    """
    from account_factory.cleanup import _check_convergence

    with pytest.raises(CleanupError) as raised:
        _check_convergence(
            [
                DeleteAction(
                    kind="NetworkStack",
                    name="ws",
                    namespace=FIXTURE_WORKSPACE,
                    reason="the pre-repair plan deleted this directly",
                ),
                DeleteAction(
                    kind="EKSClusterStack",
                    name="ws",
                    namespace=FIXTURE_WORKSPACE,
                    reason="the pre-repair plan deleted this directly",
                ),
            ],
            frozenset({"FullAccountInfrastructure"}),
        )
    message = str(raised.value)
    assert "cannot converge" in message
    # The refusal has to explain the reconciliation fact, not just report a rule violation.
    assert "recreate" in message or "rebuild" in message


def test_the_retained_roots_are_data_not_prose():
    """The convergence check must not read its decision out of an English explanation.

    An earlier draft derived retained roots by substring-matching the human-readable
    `retained` strings, and it misfired at once: the existing-account explanation contains the
    phrase "no AccountOwnership object was ever created for it", which a substring match read
    as a RETAINED `AccountOwnership` and used to refuse a valid plan. This test pins the
    separation — the existing-account plan mentions the kind in prose and retains no roots.
    """
    cleanup_plan = plan(existing_account_request())
    assert cleanup_plan.retained_roots == ()
    assert any("AccountOwnership" in retained for retained in cleanup_plan.retained)


def test_the_account_access_path_is_retained_with_the_account():
    """In new-account mode the role selector belongs to the retained account root.

    Its ARN is built from the account's own status, so it lives inside `AccountOwnership`.
    Deleting it while that root survives would not converge — and retaining the account
    together with its access path is the coherent state for "the account stays, its
    infrastructure goes", so nothing is orphaned.
    """
    assert "IAMRoleSelector" not in deleted_kinds(plan(new_account_request()))
    # Where it IS a standalone object, it is deleted.
    assert "IAMRoleSelector" in deleted_kinds(plan(existing_account_request()))


def test_an_adopted_cluster_is_never_deleted():
    """ADP did not create it, so removing the workspace must not remove the cluster."""
    cleanup_plan = plan(bring_existing_cluster_request())
    assert "EKSClusterStack" not in deleted_kinds(cleanup_plan)
    assert "NetworkStack" not in deleted_kinds(cleanup_plan)
    assert any(
        "fixture-adopted-cluster" in retained and "must not delete" in retained
        for retained in cleanup_plan.retained
    )


def test_an_adopted_cluster_plan_removes_only_the_workspaces_own_records():
    cleanup_plan = plan(bring_existing_cluster_request())
    assert deleted_kinds(cleanup_plan) == ["IAMRoleSelector", "ConfigMap"]


def test_an_adopted_account_is_retained_in_existing_account_managed():
    """ADP does not close an account it did not open."""
    cleanup_plan = plan(existing_account_request())
    assert any(
        "existed before this workspace" in retained or "adopted" in retained
        for retained in cleanup_plan.retained
    )


def test_every_delete_is_scoped_to_the_workspace_namespace(any_mode_request):
    for action in plan(any_mode_request).actions:
        assert action.namespace == any_mode_request.workspace_id


def test_every_delete_states_a_reason(any_mode_request):
    """A plan is reviewed, so each entry has to say why it is there."""
    for action in plan(any_mode_request).actions:
        assert action.reason.strip()


# ── AC-02: cleanup outside owned resources is refused ────────────────────────────────


def test_another_workspaces_namespace_is_refused():
    """The tenant-boundary case: workspace A's cleanup cannot reach workspace B."""
    with pytest.raises(CleanupError) as raised:
        plan(
            new_account_request(),
            extra_actions=(
                DeleteAction(
                    kind="ConfigMap",
                    name="victim",
                    namespace="ws-someone-else",
                    reason="discovered on the cluster",
                ),
            ),
        )
    message = str(raised.value)
    assert "ws-someone-else" in message
    assert "owns only its own namespace" in message


@pytest.mark.parametrize(
    "namespace",
    [
        "adp",
        "adp-gateway",
        "adp-agent-factory",
        "bedrockgw",
        "kube-system",
        "arc-runners",
    ],
)
def test_a_core_adp_namespace_is_refused(namespace):
    """Preserving unrelated core ADP resources is a hard requirement of this issue."""
    with pytest.raises(CleanupError) as raised:
        plan(
            new_account_request(),
            extra_actions=(
                DeleteAction(
                    kind="Deployment",
                    name="bedrockgateway",
                    namespace=namespace,
                    reason="discovered on the cluster",
                ),
            ),
        )
    assert "core ADP" in str(raised.value)


@pytest.mark.parametrize("namespace", ["kro-system", "ack-system"])
def test_the_shared_controller_namespaces_are_refused(namespace):
    """Deleting these removes the control plane every workspace depends on."""
    with pytest.raises(CleanupError):
        plan(
            new_account_request(),
            extra_actions=(
                DeleteAction(
                    kind="Deployment",
                    name="kro",
                    namespace=namespace,
                    reason="discovered on the cluster",
                ),
            ),
        )


@pytest.mark.parametrize(
    "kind,name",
    [
        ("CustomResourceDefinition", "fullaccountinfrastructures.kro.run"),
        ("CustomResourceDefinition", "eksclusterstacks.kro.run"),
        ("CustomResourceDefinition", "networkstacks.kro.run"),
        ("ClusterRole", "kro-manager"),
        ("ClusterRoleBinding", "kro-manager-binding"),
        ("ResourceGraphDefinition", "full-account-infrastructure"),
    ],
)
def test_the_exact_shared_resources_the_legacy_teardown_deleted_are_refused(kind, name):
    """These are the resources `07-teardown-capabilities.sh` removed, by name.

    Without naming them, this test would only prove that some cluster-scoped kind is
    refused — not that the specific regression is closed.
    """
    with pytest.raises(CleanupError) as raised:
        plan(
            new_account_request(),
            extra_actions=(
                DeleteAction(
                    kind=kind,
                    name=name,
                    namespace=FIXTURE_WORKSPACE,
                    reason="legacy teardown deleted this",
                ),
            ),
        )
    message = str(raised.value)
    assert "cluster-scoped" in message
    assert "separate authorization" in message


def test_a_resource_discovered_on_the_cluster_is_not_trusted_because_it_was_found():
    """The legacy `|| true` treated whatever it found as fair game.

    `extra_actions` models resources discovered from live state, and they go through the
    same guard as generated ones rather than bypassing it.
    """
    with pytest.raises(CleanupError):
        plan(
            bring_existing_cluster_request(),
            extra_actions=(
                DeleteAction(
                    kind="Secret",
                    name="gateway-secrets",
                    namespace="adp-gateway",
                    reason="found while listing the cluster",
                ),
            ),
        )


def test_a_provably_owned_discovered_resource_is_accepted():
    """The positive control: the guard is a boundary, not a blanket refusal.

    Owned kind, this module's managed-by label, this workspace's label, and a live uid equal
    to the one provisioning recorded.
    """
    cleanup_plan = plan(
        bring_existing_cluster_request(),
        extra_actions=(
            DeleteAction(
                kind="ConfigMap",
                name="ws-fixture-adopted-cluster-legacy",
                namespace=FIXTURE_WORKSPACE,
                reason="an earlier adoption record this workspace left behind",
                evidence=owned_evidence(),
                recorded_uid="uid-fixture-0001",
            ),
        ),
    )
    assert "ConfigMap" in deleted_kinds(cleanup_plan)


# ── AF-005: being in the namespace is not ownership ───────────────────────────────────
#
# These lock a specific regression. The previous guard rejected shared kinds and core
# namespaces and accepted EVERYTHING ELSE in the workspace namespace, with no ownership
# evidence at all. An application's Secret, Deployment or PVC that happened to share the
# namespace was therefore deletable by an infrastructure teardown.


def test_a_kind_this_module_never_creates_is_refused_however_it_is_labelled():
    """An application Secret in the workspace's own namespace, labelled to look owned.

    The kind check runs first for exactly this case: Account Factory does not create Secrets,
    so no amount of correct-looking labelling makes one its to delete.
    """
    with pytest.raises(CleanupError) as raised:
        plan(
            bring_existing_cluster_request(),
            extra_actions=(
                DeleteAction(
                    kind="Secret",
                    name="application-database-credentials",
                    namespace=FIXTURE_WORKSPACE,
                    reason="found while listing the namespace",
                    evidence=owned_evidence(),
                    recorded_uid="uid-fixture-0001",
                ),
            ),
        )
    message = str(raised.value)
    assert "does not create Secret resources" in message
    assert "sharing namespace" in message


@pytest.mark.parametrize(
    "kind", ["Deployment", "PersistentVolumeClaim", "Service", "CronJob", "Secret"]
)
def test_no_application_workload_kind_can_be_swept_up(kind):
    """Parametrised over the kinds an application would own in a shared namespace."""
    with pytest.raises(CleanupError, match="does not create"):
        plan(
            bring_existing_cluster_request(),
            extra_actions=(
                DeleteAction(
                    kind=kind,
                    name="application-owned",
                    namespace=FIXTURE_WORKSPACE,
                    reason="found while listing the namespace",
                    evidence=owned_evidence(),
                    recorded_uid="uid-fixture-0001",
                ),
            ),
        )


def test_a_discovered_resource_with_no_evidence_is_refused():
    """The pre-repair input: an owned KIND, in the right namespace, and nothing more.

    This is the exact shape the previous guard accepted.
    """
    with pytest.raises(CleanupError) as raised:
        plan(
            bring_existing_cluster_request(),
            extra_actions=(
                DeleteAction(
                    kind="ConfigMap",
                    name="someone-elses-config",
                    namespace=FIXTURE_WORKSPACE,
                    reason="found while listing the namespace",
                ),
            ),
        )
    assert "no ownership evidence" in str(raised.value)


def test_a_resource_managed_by_something_else_is_refused():
    """Kind collisions are the common case — a ConfigMap created by Helm, say."""
    with pytest.raises(CleanupError) as raised:
        plan(
            bring_existing_cluster_request(),
            extra_actions=(
                DeleteAction(
                    kind="ConfigMap",
                    name="helm-release-config",
                    namespace=FIXTURE_WORKSPACE,
                    reason="found while listing the namespace",
                    evidence=owned_evidence(managed_by="Helm"),
                    recorded_uid="uid-fixture-0001",
                ),
            ),
        )
    assert "created by something else" in str(raised.value)


def test_a_resource_labelled_for_another_workspace_is_refused():
    """Same namespace is not enough even between two Account Factory workspaces."""
    with pytest.raises(CleanupError) as raised:
        plan(
            bring_existing_cluster_request(),
            extra_actions=(
                DeleteAction(
                    kind="ConfigMap",
                    name="other-workspace-config",
                    namespace=FIXTURE_WORKSPACE,
                    reason="found while listing the namespace",
                    evidence=owned_evidence(workspace_label="ws-other-tenant"),
                    recorded_uid="uid-fixture-0001",
                ),
            ),
        )
    assert "labelled for workspace" in str(raised.value)


def test_a_uid_mismatch_is_refused_even_when_every_label_matches():
    """Labels are mutable; a uid is not.

    This is the case a label-only check cannot catch: a resource whose labels were edited to
    look owned, or — more likely than malice — a resource deleted and recreated under the same
    name, which is a DIFFERENT object that a predecessor's record must not authorize deleting.
    """
    with pytest.raises(CleanupError) as raised:
        plan(
            bring_existing_cluster_request(),
            extra_actions=(
                DeleteAction(
                    kind="ConfigMap",
                    name="ws-fixture-adopted-cluster",
                    namespace=FIXTURE_WORKSPACE,
                    reason="found while listing the namespace",
                    evidence=owned_evidence(uid="uid-recreated-9999"),
                    recorded_uid="uid-fixture-0001",
                ),
            ),
        )
    assert "uid" in str(raised.value)


def test_a_resource_with_no_recorded_uid_is_refused():
    """Absent evidence is not favourable evidence.

    If provisioning recorded no uid, there is nothing immutable to match, so the label match
    alone would be the whole check — and a label match can be manufactured.
    """
    with pytest.raises(CleanupError) as raised:
        plan(
            bring_existing_cluster_request(),
            extra_actions=(
                DeleteAction(
                    kind="ConfigMap",
                    name="ws-fixture-adopted-cluster",
                    namespace=FIXTURE_WORKSPACE,
                    reason="found while listing the namespace",
                    evidence=owned_evidence(),
                ),
            ),
        )
    assert "recorded no uid" in str(raised.value)


def test_the_namespace_itself_is_never_a_deletable_kind():
    """Deleting the namespace would delete everything in it, checking nothing.

    It is the single most destructive action available and it bypasses every per-resource
    ownership check by never examining those resources — so `Namespace` is absent from the
    owned-kind allowlist even though this module does create one.
    """
    with pytest.raises(CleanupError, match="does not create Namespace"):
        plan(
            bring_existing_cluster_request(),
            extra_actions=(
                DeleteAction(
                    kind="Namespace",
                    name=FIXTURE_WORKSPACE,
                    namespace=FIXTURE_WORKSPACE,
                    reason="the workspace namespace",
                    evidence=owned_evidence(),
                    recorded_uid="uid-fixture-0001",
                ),
            ),
        )


def test_ownership_evidence_is_read_from_the_live_object_not_asserted():
    """Evidence comes from the resource as the cluster reports it.

    A caller who could assert ownership could assert it about anything, so the constructor
    that matters reads labels and the server-assigned uid out of a fetched object.
    """
    evidence = OwnershipEvidence.from_live_object(
        {
            "metadata": {
                "uid": "uid-from-api-server",
                "labels": {
                    "app.kubernetes.io/managed-by": MANAGED_BY,
                    "adp.aws.dev/workspace": FIXTURE_WORKSPACE,
                },
            }
        }
    )
    assert evidence.uid == "uid-from-api-server"
    assert evidence.managed_by == MANAGED_BY
    assert evidence.workspace_label == FIXTURE_WORKSPACE


def test_evidence_from_an_unlabelled_object_is_empty_rather_than_an_error():
    """Absent labels are the ordinary case for a resource this module did not create.

    Read as empty and refused by the guard, rather than raising during discovery — a crash
    there would stop a cleanup from being planned at all because an unrelated resource existed.
    """
    evidence = OwnershipEvidence.from_live_object({"metadata": {}})
    assert evidence == OwnershipEvidence(managed_by="", workspace_label="", uid="")


def test_a_refusal_returns_no_partial_plan():
    """There is no plan object to proceed with after a refusal."""
    with pytest.raises(CleanupError):
        plan(
            new_account_request(),
            extra_actions=(
                DeleteAction(
                    kind="Deployment", name="x", namespace="adp", reason="out of scope"
                ),
            ),
        )


def test_no_delete_action_can_express_a_cluster_scoped_target():
    """Structural, not just validated: `DeleteAction` requires a namespace.

    A type that cannot represent "cluster-wide delete" is a stronger guarantee than a check
    that rejects one.
    """
    with pytest.raises(TypeError):
        DeleteAction(
            kind="CustomResourceDefinition", name="x", reason="y"
        )  # no namespace


def test_the_shared_control_plane_is_named_as_retained(any_mode_request):
    cleanup_plan = plan(any_mode_request)
    assert any(
        "shared kro and ACK controllers" in retained
        for retained in cleanup_plan.retained
    )


def test_an_invalid_request_produces_no_plan():
    """A plan built from an unvalidated request could name a workspace the caller lacks."""
    with pytest.raises(ModeError):
        plan(new_account_request(workspace_id="kube-system"))


def test_an_unauthorized_request_produces_no_plan():
    request = new_account_request()
    authorization = matching_authorization(request, organization_id="o-otherorg999")
    with pytest.raises(ModeError):
        plan(request, authorization)


# ── Account closure must be asked for by name ────────────────────────────────────────


def test_closure_requires_an_explicit_acknowledgement():
    """The legacy equivalent was a note printed after the delete had been issued."""
    with pytest.raises(CleanupError) as raised:
        closure_request(
            new_account_request(),
            account_id=FIXTURE_CREATED_ACCOUNT,
            reason="workspace decommissioned after review",
            acknowledged_irreversible=False,
            provisioned=provisioned_record(),
        )
    assert "without an explicit acknowledgement" in str(raised.value)


def test_closure_is_refused_for_an_account_adp_did_not_create():
    """ADP does not close an account it adopted."""
    for build in (existing_account_request, bring_existing_cluster_request):
        with pytest.raises(CleanupError) as raised:
            closure_request(
                build(),
                account_id=FIXTURE_TARGET_ACCOUNT,
                reason="decommissioned",
                acknowledged_irreversible=True,
                provisioned=provisioned_record(account_id=FIXTURE_TARGET_ACCOUNT),
            )
        assert "adopted this account rather than creating it" in str(raised.value)


def test_closure_requires_a_reason():
    with pytest.raises(CleanupError, match="must state a reason"):
        closure_request(
            new_account_request(),
            account_id=FIXTURE_CREATED_ACCOUNT,
            reason="   ",
            acknowledged_irreversible=True,
            provisioned=provisioned_record(),
        )


def test_a_fully_acknowledged_closure_request_is_built_and_states_its_consequence():
    """The positive control, and the consequence is stated BEFORE anything is executed."""
    request = closure_request(
        new_account_request(),
        account_id=FIXTURE_CREATED_ACCOUNT,
        reason="workspace decommissioned after review",
        acknowledged_irreversible=True,
        provisioned=provisioned_record(),
    )
    assert request.scope is CleanupScope.ACCOUNT_CLOSURE
    assert request.account_id == FIXTURE_CREATED_ACCOUNT
    assert request.workspace_id == FIXTURE_WORKSPACE
    assert "90-day suspended state" in request.consequence
    assert "not" in request.consequence and "reversible" in request.consequence


# ── AF-004: closure acts only on the account this workspace actually created ───────────
#
# These lock a specific regression. Closure previously checked only that `account_id` was
# non-empty, so it accepted the string 'not-an-account' AND any other well-formed 12-digit id
# — including a real, unrelated production account. The action it authorizes is an irreversible
# 90-day suspension, so "wrong account" is not a recoverable error.


def test_closure_requires_the_account_id_to_be_stated():
    """No default, and not derived from the workspace — it must be named."""
    with pytest.raises(CleanupError) as raised:
        closure_request(
            new_account_request(),
            account_id="",
            reason="decommissioned",
            acknowledged_irreversible=True,
            provisioned=provisioned_record(),
        )
    assert "must state the account id" in str(raised.value)


@pytest.mark.parametrize(
    "malformed",
    [
        "not-an-account",
        "00000000000",  # 11 digits
        "0000000000012",  # 13 digits
        "0000000000o3",  # letter in place of a digit
        "0000000 00003",  # internal space
        "arn:aws:organizations::000000000003:account/o-x/000000000003",
        "000000000003/",
        "000000000003,000000000777",  # two ids, one of them someone else's
    ],
)
def test_a_malformed_account_id_is_refused(malformed):
    """`'not-an-account'` was accepted before the repair. The shape is checked first."""
    with pytest.raises(CleanupError) as raised:
        closure_request(
            new_account_request(),
            account_id=malformed,
            reason="decommissioned",
            acknowledged_irreversible=True,
            provisioned=provisioned_record(),
        )
    assert "12-digit" in str(raised.value)


def test_surrounding_whitespace_is_tolerated_and_the_id_still_has_to_match():
    """Trimmed, deliberately — and trimming is not a loophole.

    An account id copied from a console arrives with a trailing newline, and refusing that
    would push an operator towards retyping a 12-digit number by hand, which is a worse risk
    than the one it avoids. It is safe only because trimming cannot turn one account into
    another: the result must still equal the recorded id, as the second half of this test
    shows. Internal whitespace is NOT tolerated (see the malformed cases above), because it
    cannot come from a copy-paste of a single id.
    """
    request = closure_request(
        new_account_request(),
        account_id=f"  {FIXTURE_CREATED_ACCOUNT}\n",
        reason="workspace decommissioned after review",
        acknowledged_irreversible=True,
        provisioned=provisioned_record(),
    )
    assert request.account_id == FIXTURE_CREATED_ACCOUNT

    with pytest.raises(CleanupError):
        closure_request(
            new_account_request(),
            account_id="  000000000777\n",
            reason="workspace decommissioned after review",
            acknowledged_irreversible=True,
            provisioned=provisioned_record(),
        )


def test_a_different_but_well_formed_account_id_is_refused():
    """The dangerous case: a real account id that is simply not this workspace's.

    A shape check alone would pass this, which is why the comparison against the recorded
    account is the substantive guard.
    """
    with pytest.raises(CleanupError) as raised:
        closure_request(
            new_account_request(),
            account_id="000000000777",
            reason="decommissioned",
            acknowledged_irreversible=True,
            provisioned=provisioned_record(),
        )
    message = str(raised.value)
    assert "000000000777" in message
    assert FIXTURE_CREATED_ACCOUNT in message


def test_closure_requires_a_record_of_what_was_provisioned():
    """Without a record there is nothing to compare the id against.

    Refused rather than falling back to trusting the caller's id — that fallback IS the
    finding, and it would look like a check.
    """
    with pytest.raises(CleanupError) as raised:
        closure_request(
            new_account_request(),
            account_id=FIXTURE_CREATED_ACCOUNT,
            reason="decommissioned",
            acknowledged_irreversible=True,
            provisioned=None,
        )
    assert "record" in str(raised.value)


def test_a_record_belonging_to_another_workspace_is_refused():
    """A record is only evidence for the workspace it was recorded against."""
    with pytest.raises(CleanupError) as raised:
        closure_request(
            new_account_request(),
            account_id=FIXTURE_CREATED_ACCOUNT,
            reason="decommissioned",
            acknowledged_irreversible=True,
            provisioned=provisioned_record(workspace_id="ws-other-tenant"),
        )
    assert "ws-other-tenant" in str(raised.value)


def test_a_record_from_another_organization_is_refused():
    """Two organizations can each hold an account; the id alone does not distinguish them."""
    with pytest.raises(CleanupError) as raised:
        closure_request(
            new_account_request(),
            account_id=FIXTURE_CREATED_ACCOUNT,
            reason="decommissioned",
            acknowledged_irreversible=True,
            provisioned=provisioned_record(organization_id="o-otherorg999"),
        )
    assert "organization" in str(raised.value)


def test_an_unauthorized_closure_request_is_refused():
    """Closure validates the request against its authorization like everything else.

    Otherwise the most destructive operation in the module would be the one with the weakest
    check — and AF-003's workspace comparison would not apply to it.
    """
    request = new_account_request()
    with pytest.raises(ModeError):
        closure_request(
            request,
            account_id=FIXTURE_CREATED_ACCOUNT,
            reason="decommissioned",
            acknowledged_irreversible=True,
            provisioned=provisioned_record(),
            authorization=matching_authorization(
                request, workspace_id="ws-other-tenant"
            ),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "not-an-account"),
        ("account_id", ""),
        ("workspace_id", "  "),
        ("organization_id", ""),
    ],
)
def test_a_provisioning_record_validates_its_own_contents(field, value):
    """An unusable record must fail where it is constructed, not where it is trusted.

    A record carrying an empty workspace would otherwise compare equal to nothing and refuse
    everything, or worse, be built from whatever a caller happened to have.
    """
    with pytest.raises(CleanupError):
        provisioned_record(**{field: value})


def test_a_closure_request_cannot_be_reached_from_a_cleanup_plan():
    """The two scopes are separate requests, and the second is not derivable from the first.

    `plan` returns a `CleanupPlan`; nothing on it produces a `ClosureRequest`, so there is no
    route from "remove this workspace" to "close this account".
    """
    cleanup_plan = plan(new_account_request())
    assert not [
        attribute
        for attribute in dir(cleanup_plan)
        if "closure" in attribute.lower() and attribute != "describes_account_closure"
    ]
    assert cleanup_plan.describes_account_closure() is False


def test_the_two_scopes_are_distinct_values():
    assert CleanupScope.WORKSPACE_RESOURCES is not CleanupScope.ACCOUNT_CLOSURE
    assert {scope.value for scope in CleanupScope} == {
        "workspace-resources",
        "account-closure",
    }


def test_only_the_account_creating_mode_can_reach_a_closure_request():
    """Ties closure eligibility to the mode's ownership, for every mode."""
    for mode, build in BUILDERS.items():
        allowed = mode is OwnershipMode.NEW_ACCOUNT_MANAGED
        try:
            closure_request(
                build(),
                account_id=FIXTURE_CREATED_ACCOUNT,
                reason="decommissioned",
                acknowledged_irreversible=True,
                provisioned=provisioned_record(),
            )
        except CleanupError:
            assert not allowed, f"{mode.value} should have been able to request closure"
        else:
            assert allowed, f"{mode.value} must not be able to request closure"


# ── Discovered resources are governed by the same rules as generated ones ─────────────────
#
# The three tests below cover defects found in review of the repair itself. All three shared
# one root cause: `extra_actions` (resources discovered on the cluster) passed the ownership
# guard but bypassed the mode and convergence reasoning that governs generated actions. A
# discovered resource is the MORE dangerous of the two — it is found rather than derived, so
# nothing about the request constrains what it might be.


def test_a_discovered_child_of_a_retained_root_cannot_be_deleted():
    """Convergence is a property of the executed plan, not of the generated half of it.

    `AccountOwnership` is retained in new-account-managed and declares `IAMRoleSelector`. The
    generated plan therefore omits that delete — but a DISCOVERED `IAMRoleSelector` with
    entirely valid ownership evidence was accepted, producing the exact non-convergent plan
    the retention exists to prevent: kro reconciles the declaration and recreates the resource
    after the teardown reported success.
    """
    with pytest.raises(CleanupError) as raised:
        plan(
            new_account_request(),
            extra_actions=(
                DeleteAction(
                    kind="IAMRoleSelector",
                    name=FIXTURE_WORKSPACE,
                    namespace=FIXTURE_WORKSPACE,
                    reason="found while listing the workspace namespace",
                    evidence=owned_evidence(),
                    recorded_uid="uid-fixture-0001",
                ),
            ),
        )
    message = str(raised.value)
    assert "cannot converge" in message
    assert "AccountOwnership" in message


def test_the_account_root_is_never_deletable_by_a_workspace_cleanup():
    """Deleting `AccountOwnership` closes the AWS account, in every mode.

    This is the one refusal that ownership evidence must NOT be able to satisfy: a genuinely
    owned account root is precisely the object whose deletion is the irreversible act. Closure
    goes through `closure_request`, which requires a recorded provisioned account and an
    explicit acknowledgement.
    """
    for mode, build in BUILDERS.items():
        with pytest.raises(CleanupError) as raised:
            plan(
                build(),
                extra_actions=(
                    DeleteAction(
                        kind="AccountOwnership",
                        name=FIXTURE_WORKSPACE,
                        namespace=FIXTURE_WORKSPACE,
                        reason="found while listing the workspace namespace",
                        evidence=owned_evidence(),
                        recorded_uid="uid-fixture-0001",
                    ),
                ),
            )
        message = str(raised.value)
        assert "90-day" in message, mode.value
        assert "closure_request" in message, mode.value


def test_an_adopted_cluster_workspace_cannot_delete_discovered_infrastructure():
    """ADP must not delete infrastructure it did not create, however it was discovered.

    `bring-existing-cluster` creates no VPC, cluster or account. The generated plan honours
    that, but a discovered `EKSClusterStack`, `NetworkStack` or `WorkspaceInfrastructure` was
    accepted on ownership evidence — which would be ADP destroying a tenant's own cluster
    during what the mode documents as a namespace-only cleanup.
    """
    for kind in ("WorkspaceInfrastructure", "NetworkStack", "EKSClusterStack"):
        with pytest.raises(CleanupError) as raised:
            plan(
                bring_existing_cluster_request(),
                extra_actions=(
                    DeleteAction(
                        kind=kind,
                        name=FIXTURE_WORKSPACE,
                        namespace=FIXTURE_WORKSPACE,
                        reason="found while listing the workspace namespace",
                        evidence=owned_evidence(),
                        recorded_uid="uid-fixture-0001",
                    ),
                ),
            )
        message = str(raised.value)
        assert "created no AWS infrastructure" in message, kind
        assert "not ADP's to delete" in message, kind


def test_a_provably_owned_discovered_resource_is_still_accepted_after_the_new_guards():
    """Positive control: the three refusals above are boundaries, not a blanket denial.

    An adoption-record ConfigMap in bring-existing-cluster is a kind ADP genuinely creates in
    that mode, is not the account root, and is not declared by any retained root — so it is
    still accepted, with evidence.
    """
    cleanup_plan = plan(
        bring_existing_cluster_request(),
        extra_actions=(
            DeleteAction(
                kind="ConfigMap",
                name=f"{FIXTURE_WORKSPACE}-adopted-cluster-legacy",
                namespace=FIXTURE_WORKSPACE,
                reason="an earlier adoption record this workspace left behind",
                evidence=owned_evidence(),
                recorded_uid="uid-fixture-0001",
            ),
        ),
    )
    assert "ConfigMap" in deleted_kinds(cleanup_plan)
    assert not cleanup_plan.closes_account


def test_a_discovered_infrastructure_resource_is_accepted_where_adp_created_it():
    """Positive control for the mode boundary: the refusal is about the MODE, not the kind.

    The same `NetworkStack` refused in bring-existing-cluster is accepted in
    existing-account-managed, where ADP did create it — and it converges there because the
    root that declares it (`WorkspaceInfrastructure`) is itself deleted rather than retained.
    """
    cleanup_plan = plan(
        existing_account_request(),
        extra_actions=(
            DeleteAction(
                kind="NetworkStack",
                name=FIXTURE_WORKSPACE,
                namespace=FIXTURE_WORKSPACE,
                reason="a stack left behind by an interrupted apply",
                evidence=owned_evidence(),
                recorded_uid="uid-fixture-0001",
            ),
        ),
    )
    assert "NetworkStack" in deleted_kinds(cleanup_plan)
