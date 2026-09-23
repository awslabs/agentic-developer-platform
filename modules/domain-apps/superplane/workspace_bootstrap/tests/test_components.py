"""Component install and ownership — Issue #5533 (w6-10), AC-01 and AC-02.

AC-01's "missing CRDs" and "duplicate controllers" cases live here, as does the
ownership recording AC-02's cleanup guarantee depends on: `retire.py` can only
preserve an adopted namespace if this module recorded that it was adopted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from superplane_bootstrap.access import ObservedNamespace
from superplane_bootstrap.admission import (
    RESTRICTED_ENFORCE_LABEL,
    RESTRICTED_ENFORCE_VERSION_LABEL,
)
from superplane_bootstrap.components import (
    BOOTSTRAP_OWNER_LABEL,
    WORKSPACE_CRDS,
    InstalledObject,
    install_components,
)
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.state import BootstrapState, NamespaceRecord
from superplane_bootstrap.target import verify_target

from .conftest import (
    CONTROLLER_NAME,
    CONTROLLER_SERVICE_ACCOUNT,
    ENFORCE_VERSION,
    NAMESPACE,
    WORKSPACE_ID,
    FakeClusterAccess,
    FakeStateStore,
)


@pytest.fixture
def target(binding, provider_identity, observed_cluster, expected_target):
    return verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adp-created",
        **expected_target,
    )


@pytest.fixture
def adopted_target(binding, provider_identity, observed_cluster, expected_target):
    return verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adopted",
        **expected_target,
    )


def _install(access, target, **overrides):
    """Install, returning only the `ComponentInstallation`.

    `install_components` returns `(installation, state)` since the F6 repair — every
    mutation is written to durable state as it happens, so the updated state has to come
    back for the next gate to build on. Most tests here assert on the installation, so
    the helper unpacks; the F6 tests call `_install_with_state` for both halves.
    """
    installation, _ = _install_with_state(access, target, **overrides)
    return installation


def _install_with_state(access, target, *, store=None, state=None, **overrides):
    """Install, returning both halves and defaulting the durable-state seam.

    The store and state are REQUIRED parameters of `install_components` (F6), so this
    helper defaults them rather than letting each test construct a pair — a test that
    silently got a fresh state would be on the adoption path without saying so, which is
    the F3 trap `test_workspace._access` documents.
    """
    store = FakeStateStore() if store is None else store
    if state is None:
        state = BootstrapState(
            workspace_id=target.workspace_id, cluster_arn=target.cluster_arn
        )
    arguments = {
        "access": access,
        "target": target,
        "namespace": NAMESPACE,
        "enforce_version": ENFORCE_VERSION,
        "store": store,
        "state": state,
        "controller_name": CONTROLLER_NAME,
        **overrides,
    }
    return install_components(**arguments)


# --- Control ---------------------------------------------------------------------


def test_a_fresh_cluster_gets_an_owned_namespace_and_established_crds(target):
    access = FakeClusterAccess(crds=[])

    installation = _install(access, target)

    assert installation.namespace == NAMESPACE
    assert installation.namespace_owned is True
    assert installation.namespace_uid
    assert set(access.crds) == set(WORKSPACE_CRDS)


def test_the_created_namespace_carries_the_labels_the_isolation_gate_requires(target):
    """The create path and the proof path must agree by construction.

    `components.py` builds labels from `admission.tenant_namespace_labels`. If it
    restated them, a namespace could be created with the wrong policy and then fail
    its own proof — a bootstrap that always fails at gate 3 for a reason introduced
    at gate 2.
    """
    access = FakeClusterAccess(crds=[])

    _install(access, target)

    (_, labels) = access.created_namespaces[0]
    assert labels[RESTRICTED_ENFORCE_LABEL] == "restricted"
    assert labels[RESTRICTED_ENFORCE_VERSION_LABEL] == ENFORCE_VERSION
    assert labels[BOOTSTRAP_OWNER_LABEL] == WORKSPACE_ID


def test_crds_are_recorded_as_shared_and_never_owned(target):
    """Cluster-scoped and shared between every workspace on the cluster.

    Deleting `nodepools.superplane.ai` removes every NodePool cluster-wide, including
    other workspaces'. Recording them as not-owned is what keeps them out of every
    cleanup plan.
    """
    access = FakeClusterAccess(crds=[])

    installation = _install(access, target)

    crds = [
        obj for obj in installation.objects if obj.kind == "CustomResourceDefinition"
    ]
    assert len(crds) == len(WORKSPACE_CRDS)
    assert all(obj.shared and not obj.owned for obj in crds)
    # The assertion that matters: no CRD is ever in the owned set, whatever else is.
    # Stated as an exclusion rather than as "everything owned is a Namespace", which
    # was true only while the namespace was the sole thing installed — the controller
    # and its scoped RBAC (F2) are namespaced objects this workspace owns alone, and
    # cleanup must remove them.
    assert not any(
        obj.kind == "CustomResourceDefinition" for obj in installation.owned_objects
    )
    assert {obj.kind for obj in installation.owned_objects} == {
        "Namespace",
        "ServiceAccount",
        "Role",
        "RoleBinding",
        "Deployment",
    }


# --- Missing CRDs (AC-01) --------------------------------------------------------


def test_crds_still_absent_after_installation_are_refused(target):
    """A workspace without the types the controller reconciles accepts work it
    cannot act on, so this refuses rather than registering."""
    access = FakeClusterAccess(crds=[], establish_crds_result=[])

    with pytest.raises(BootstrapRefused, match="required CRDs are not established"):
        _install(access, target)


def test_a_partially_established_crd_set_is_refused(target):
    """One of two is not "installed"."""
    access = FakeClusterAccess(
        crds=[], establish_crds_result=["nodepools.superplane.ai"]
    )

    with pytest.raises(BootstrapRefused, match="superplanenodes.superplane.ai"):
        _install(access, target)


def test_an_empty_required_crd_set_is_refused(target):
    """An empty set would let this gate pass without establishing anything."""
    access = FakeClusterAccess(crds=[])

    with pytest.raises(BootstrapRefused, match="no required CRDs"):
        _install(access, target, required_crds=())


def test_already_established_crds_are_idempotent(target):
    """A re-run is the normal case; bootstrap must be retry-safe (design item 3)."""
    access = FakeClusterAccess()

    installation = _install(access, target)

    assert set(access.crds) == set(WORKSPACE_CRDS)
    assert installation.namespace_owned is True


# --- Duplicate controllers (AC-01) -----------------------------------------------


def test_an_existing_controller_blocks_installation(target):
    """Two controllers reconciling the same cluster-scoped NodePools contend
    continuously rather than failing cleanly. AC-01 names this case."""
    access = FakeClusterAccess(
        controller_images=[
            "123.dkr.ecr.us-east-1.amazonaws.com/superplane-controller:v1"
        ]
    )

    with pytest.raises(BootstrapRefused, match="handover before installation"):
        _install(access, target)


def test_the_controller_check_runs_before_anything_is_created(target):
    """A namespace created next to a contending controller is a namespace cleanup
    then has to reason about, so the refusal must precede the create."""
    access = FakeClusterAccess(
        crds=[], controller_images=["registry.example/superplane-controller:v2"]
    )

    with pytest.raises(BootstrapRefused):
        _install(access, target)

    assert access.created_namespaces == []
    assert access.established == []


def test_an_unrelated_deployment_does_not_block_installation(target):
    """Only controllers contend. Refusing on any Deployment would make bootstrap
    impossible on a supplied cluster that runs anything at all."""
    access = FakeClusterAccess(
        crds=[], controller_images=["registry.example/some-tenant-api:v1"]
    )

    assert _install(access, target).namespace_owned is True


def test_a_retry_does_not_refuse_the_controller_it_installed_itself(target):
    """Idempotence across the controller install (design item 3).

    Now that this gate INSTALLS the controller, the controller present on a retry is
    normally the one the previous attempt installed. A duplicate-controller refusal that
    could not tell those apart would make bootstrap succeed exactly once and refuse every
    retry, leaving an operator no path forward but deleting the controller ADP just
    installed. The durable record is what distinguishes them, the same way it decides
    namespace ownership.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeStateStore()

    first, state = _install_with_state(access, target, store=store)
    assert state.controller_installed is True
    assert len(access.installed_controllers) == 1

    second, _ = _install_with_state(access, target, store=store, state=state)

    assert second.namespace_owned is True
    # Installed once, not twice: a second Deployment apply against a live controller is
    # the contention this package refuses elsewhere.
    assert len(access.installed_controllers) == 1
    assert any(obj.kind == "Deployment" and obj.owned for obj in second.owned_objects)


def test_an_unaccounted_controller_still_blocks_a_retry(target):
    """The record excuses ONE controller, not any number of them.

    A durable record saying "we installed a controller" plus two controllers on the
    cluster means one of them is not ours, which is the contending pair the gate exists
    to refuse. Reading the record as a blanket exemption would turn the F2 install into a
    way to disable the duplicate-controller check permanently.
    """
    access = FakeClusterAccess(crds=[])
    store = FakeStateStore()
    _, state = _install_with_state(access, target, store=store)

    access.controller_images.append("registry.example/superplane-controller:rogue")

    with pytest.raises(BootstrapRefused, match="handover before installation"):
        _install_with_state(access, target, store=store, state=state)


# --- The controller install (F2) --------------------------------------------------


def test_the_controller_and_its_scoped_rbac_are_installed_and_owned(target):
    """F2's core gap: the first revision established the CRDs and installed nothing to
    reconcile them, so `readiness._controller_checks` could never verify."""
    access = FakeClusterAccess(crds=[])

    installation = _install(access, target)

    assert access.installed_rbac == [(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)]
    assert access.installed_controllers == [
        (NAMESPACE, CONTROLLER_NAME, CONTROLLER_SERVICE_ACCOUNT)
    ]
    owned = {(obj.kind, obj.name) for obj in installation.owned_objects}
    assert ("Deployment", CONTROLLER_NAME) in owned
    assert ("ServiceAccount", CONTROLLER_SERVICE_ACCOUNT) in owned


def test_the_rbac_is_established_before_the_controller_starts(target):
    """A controller whose Role does not exist yet spends its first reconcile loops being
    denied, and a controller crash-looping on authorization is indistinguishable at the
    readiness gate from one whose image is wrong."""
    access = FakeClusterAccess(crds=[])

    _install(access, target)

    assert access.calls.index(
        f"establish_controller_rbac:{NAMESPACE}"
    ) < access.calls.index(f"install_controller:{NAMESPACE}/{CONTROLLER_NAME}")


def test_the_controller_is_installed_after_the_crds_it_reconciles(target):
    """A controller that starts before its CRDs exist watches types the API server does
    not serve."""
    access = FakeClusterAccess(crds=[])

    _install(access, target)

    assert access.calls.index("establish_crds") < access.calls.index(
        f"install_controller:{NAMESPACE}/{CONTROLLER_NAME}"
    )


def test_a_failed_rbac_install_refuses_with_the_namespace_and_crds_recorded(target):
    """F6 applied to the controller step. The namespace and the CRDs already exist by
    the time this runs, so a bare failure would leave an owned namespace with no
    cleanup plan."""
    access = FakeClusterAccess(crds=[], rbac_install_fails=True)

    with pytest.raises(BootstrapRefused, match="scoped RBAC") as raised:
        _install(access, target)

    partial = raised.value.installation
    assert partial is not None
    assert partial.namespace == NAMESPACE
    assert partial.namespace_owned is True
    assert access.installed_controllers == []


def test_rbac_that_reports_nothing_created_refuses_rather_than_continuing(target):
    """ "Succeeded but named no objects" is a distinct hazard from "failed": it is the
    case that leaves objects cleanup cannot name. Starting a controller whose permissions
    cannot be enumerated is the one thing worse than not starting it."""
    access = FakeClusterAccess(crds=[], rbac_reports_nothing=True)

    with pytest.raises(BootstrapRefused, match="no RBAC objects created"):
        _install(access, target)

    assert access.installed_controllers == []


def test_a_failed_controller_install_carries_the_rbac_it_already_created(target):
    """The partial record has to include the RBAC, or cleanup leaves a Role and a
    RoleBinding behind permanently — nothing else in the system knows they exist."""
    access = FakeClusterAccess(crds=[], controller_install="fails")

    with pytest.raises(
        BootstrapRefused, match="installing the workspace controller"
    ) as raised:
        _install(access, target)

    assert access.installed_rbac == [(NAMESPACE, CONTROLLER_SERVICE_ACCOUNT)]
    partial = raised.value.installation
    assert partial is not None
    kinds = {obj.kind for obj in partial.owned_objects}
    assert {"Namespace", "Role", "RoleBinding", "ServiceAccount"} <= kinds
    # Not the Deployment: it was never installed, and a cleanup plan naming a workload
    # that does not exist sends an operator looking for the wrong thing.
    assert "Deployment" not in kinds


def test_a_controller_that_reports_a_different_name_is_refused(target):
    """The readiness gate looks the controller up by name. A workload installed under a
    different one would be reported absent, which is a confusing refusal for a
    controller that is actually running."""
    access = FakeClusterAccess(crds=[], controller_install="misnamed")

    with pytest.raises(BootstrapRefused, match="not superplane-workspace/"):
        _install(access, target)


def test_the_controller_install_does_not_assert_availability_itself(target):
    """Whether the controller is AVAILABLE is `readiness.py`'s question.

    Answering it here as well would put the same judgement in two places, and the two
    would eventually disagree. So an installed-but-unavailable controller returns
    normally from this gate, and the readiness gate is what refuses — which is also what
    keeps the taint on. `test_readiness` and `test_workspace` cover that half.
    """
    access = FakeClusterAccess(crds=[], controller_install="unavailable")

    installation = _install(access, target)

    assert any(obj.kind == "Deployment" for obj in installation.owned_objects)


# --- Namespace adoption (AC-02) --------------------------------------------------


def test_an_equivalent_existing_namespace_is_adopted_not_recreated(adopted_target):
    """The retry and BYOC case. Adopted means cleanup leaves it alone."""
    access = FakeClusterAccess(crds=[])
    access.namespaces[NAMESPACE] = ObservedNamespace(
        name=NAMESPACE,
        uid="pre-existing-uid",
        labels={
            RESTRICTED_ENFORCE_LABEL: "restricted",
            RESTRICTED_ENFORCE_VERSION_LABEL: ENFORCE_VERSION,
        },
    )

    installation = _install(access, adopted_target)

    assert installation.namespace_owned is False
    assert installation.namespace_uid == "pre-existing-uid"
    assert access.created_namespaces == []


def test_a_namespace_this_bootstrap_created_is_re_adopted_as_owned(target):
    """A re-run after a partial bootstrap must still recognise its own namespace,
    or cleanup would stop being able to remove what it created.

    Ownership comes from the DURABLE RECORD carrying the uid observed at creation (F3),
    so this test supplies that record. It used to pass on the strength of the ownership
    label alone — which is precisely the finding: the label is forgeable, so recognising
    a namespace by it meant an unrelated namespace carrying the same label was recorded
    as ADP-created and planned for deletion. The negative case is
    `test_a_pre_existing_namespace_cannot_forge_ownership_with_a_label` below: same
    labels, same uid, no record, adopted.
    """
    access = FakeClusterAccess(crds=[])
    access.namespaces[NAMESPACE] = ObservedNamespace(
        name=NAMESPACE,
        uid="our-earlier-uid",
        labels={
            RESTRICTED_ENFORCE_LABEL: "restricted",
            RESTRICTED_ENFORCE_VERSION_LABEL: ENFORCE_VERSION,
            BOOTSTRAP_OWNER_LABEL: WORKSPACE_ID,
        },
    )
    recorded = BootstrapState(
        workspace_id=target.workspace_id,
        cluster_arn=target.cluster_arn,
        namespace=NamespaceRecord(name=NAMESPACE, uid="our-earlier-uid"),
    )

    installation = _install(access, target, state=recorded)

    assert installation.namespace_owned is True
    assert installation.namespace_uid == "our-earlier-uid"


def test_a_pre_existing_namespace_cannot_forge_ownership_with_a_label(target):
    """F3, stated directly: the ownership label is not evidence of ownership.

    Neither the label key nor the workspace id is a secret, so a BYOC owner or an
    unrelated prior process can create a namespace carrying
    `superplane.aws-e/bootstrap-owner == workspace_id`. The first revision read that
    label, recorded the observed uid as if ADP had created the namespace, and
    `retire.py` then planned to delete it along with every workload in it.

    With no durable creation record the namespace is ADOPTED, so the cleanup plan
    preserves it. Identical to the test above in every respect except the record, which
    is the point.
    """
    access = FakeClusterAccess(crds=[])
    access.namespaces[NAMESPACE] = ObservedNamespace(
        name=NAMESPACE,
        uid="our-earlier-uid",
        labels={
            RESTRICTED_ENFORCE_LABEL: "restricted",
            RESTRICTED_ENFORCE_VERSION_LABEL: ENFORCE_VERSION,
            BOOTSTRAP_OWNER_LABEL: WORKSPACE_ID,
        },
    )

    installation = _install(access, target)

    assert installation.namespace_owned is False
    assert access.created_namespaces == []


def test_a_recreated_namespace_with_a_new_uid_is_adopted_not_deleted(target):
    """The other direction F3's uid cross-check catches.

    A namespace ADP genuinely created, then deleted and recreated by somebody else,
    has the recorded NAME and a different uid. The object under that name is no longer
    the one ADP made, so it is adopted and preserved — a delete keyed on the name alone
    would hit whatever now occupies it.
    """
    access = FakeClusterAccess(crds=[])
    access.namespaces[NAMESPACE] = ObservedNamespace(
        name=NAMESPACE,
        uid="somebody-elses-uid",
        labels={
            RESTRICTED_ENFORCE_LABEL: "restricted",
            RESTRICTED_ENFORCE_VERSION_LABEL: ENFORCE_VERSION,
        },
    )
    recorded = BootstrapState(
        workspace_id=target.workspace_id,
        cluster_arn=target.cluster_arn,
        namespace=NamespaceRecord(name=NAMESPACE, uid="our-earlier-uid"),
    )

    installation = _install(access, target, state=recorded)

    assert installation.namespace_owned is False
    assert installation.namespace_uid == "somebody-elses-uid"


def test_an_existing_namespace_with_weaker_admission_is_refused_not_relabelled(
    adopted_target,
):
    """The AC-02 violation this closes.

    On a supplied cluster the namespace may hold workloads ADP knows nothing about,
    and changing its Pod Security policy could stop them scheduling. So a divergent
    namespace is refused, and nothing is mutated.
    """
    access = FakeClusterAccess(crds=[])
    access.namespaces[NAMESPACE] = ObservedNamespace(
        name=NAMESPACE,
        uid="someone-elses-uid",
        labels={RESTRICTED_ENFORCE_LABEL: "privileged"},
    )

    with pytest.raises(BootstrapRefused, match="refusing to relabel"):
        _install(access, adopted_target)

    assert access.created_namespaces == []
    assert access.namespaces[NAMESPACE].labels[RESTRICTED_ENFORCE_LABEL] == "privileged"


def test_a_namespace_missing_the_ownership_stamp_is_adopted_rather_than_refused(
    adopted_target,
):
    """Requiring the stamp would turn every legitimate BYOC adoption into a refusal.

    Only the admission labels are compared; the stamp is absent by definition on a
    namespace this bootstrap did not create.
    """
    access = FakeClusterAccess(crds=[])
    access.namespaces[NAMESPACE] = ObservedNamespace(
        name=NAMESPACE,
        uid="pre-existing-uid",
        labels={
            RESTRICTED_ENFORCE_LABEL: "restricted",
            RESTRICTED_ENFORCE_VERSION_LABEL: ENFORCE_VERSION,
        },
    )

    assert _install(access, adopted_target).namespace_owned is False


# --- Structural ------------------------------------------------------------------


def test_an_object_cannot_be_both_owned_and_shared():
    """Cleanup would have to both delete it and preserve it."""
    with pytest.raises(BootstrapRefused, match="both owned and shared"):
        InstalledObject(kind="Namespace", name="x", owned=True, shared=True)


def test_a_blank_namespace_name_is_refused(target):
    with pytest.raises(BootstrapRefused, match="namespace name is required"):
        _install(FakeClusterAccess(), target, namespace="  ")


def test_a_blank_controller_name_is_refused(target):
    """Without a controller name this gate would establish the CRDs and leave nothing
    to reconcile them — the F2 defect, reachable through a blank parameter."""
    with pytest.raises(BootstrapRefused, match="controller name is required"):
        _install(FakeClusterAccess(crds=[]), target, controller_name="  ")


def test_a_blank_controller_service_account_is_refused(target):
    """The scoped RBAC is bound to this subject. An unnamed one would mean binding the
    controller's permissions to the namespace `default` account, which every pod in the
    namespace gets automatically."""
    with pytest.raises(BootstrapRefused, match="service account name is required"):
        _install(FakeClusterAccess(crds=[]), target, controller_service_account="  ")


def test_the_installation_records_the_workspace_and_cluster_it_belongs_to(target):
    """Both are checked by `registration.py` and `retire.py`, so a record cannot be
    applied to the wrong workspace's cleanup."""
    installation = _install(FakeClusterAccess(), target)

    assert installation.workspace_id == target.workspace_id
    assert installation.cluster_arn == target.cluster_arn


# --- F9: no wrapper reproduces a foreign exception's text -------------------------


def test_no_module_interpolates_a_raw_exception_into_a_refusal():
    """A source-level guard, because F9 was the SAME defect in four places.

    The finalization path was fixed and three equivalent wrappers were left behind —
    `establish_controller_rbac`, `install_controller` and `establish_crds` — plus the
    orchestration's catch-all. Fixing four call sites does not stop a fifth being added,
    and a fifth would leak exactly as silently as these did: the code reads as helpful
    diagnostics, and the leak only appears when a real backend puts a credential in its
    error string.

    So this asserts over the package SOURCE rather than over behaviour. Behavioural
    tests (in `test_cli.py`) prove the four known paths are clean; only a structural
    check can speak for the paths nobody has written yet. `failure_kind(error)` is the
    sanctioned form and is what this permits.

    Matched on the `{error}`-style interpolation of a caught exception name. Narrow on
    purpose: it looks for the exception variables these wrappers bind (`error`, `exc`,
    `refusal`) inside an f-string brace, not for every f-string, so it cannot be
    satisfied by renaming and does not fire on unrelated formatting.

    Read from the parsed f-strings rather than from the raw lines, via `ast`. A line-wise
    regex flagged `errors.py`'s own docstring, which QUOTES the forbidden form to explain
    why it is forbidden — and the fix for that must not be "allow the pattern in
    anything that looks like prose", because a leak one line below a `#` comment would
    then pass. Asking the syntax tree for actual f-string interpolations distinguishes
    the two exactly: a docstring is a plain string constant with no interpolation, and a
    real leak is an f-string whose substituted expression is the caught name.
    """
    package = Path(__file__).resolve().parents[1] / "superplane_bootstrap"
    sources = sorted(package.glob("*.py"))
    assert sources, "no package sources found; this guard would pass vacuously"

    # The caught-exception variables these wrappers bind. `failure_kind(error)` and
    # `type(error).__name__` are CALLS, not bare names, so they do not match — which is
    # the point: they are bounded, and the bare name is not.
    caught = {"error", "exc", "refusal"}

    offenders = []
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.JoinedStr):
                continue
            for part in node.values:
                if (
                    isinstance(part, ast.FormattedValue)
                    and isinstance(part.value, ast.Name)
                    and part.value.id in caught
                ):
                    offenders.append(
                        f"{path.name}:{part.lineno}: interpolates {{{part.value.id}}}"
                    )

    assert not offenders, (
        "a refusal message interpolates a caught exception's own text. That text comes "
        "from a cloud SDK, a database driver or a subprocess and can carry a token, a "
        "request body or a DSN; `cli.py` prints a refusal to stdout. Use "
        "`failure_kind(error)` and let `raise ... from error` carry the rest:\n  "
        + "\n  ".join(offenders)
    )
