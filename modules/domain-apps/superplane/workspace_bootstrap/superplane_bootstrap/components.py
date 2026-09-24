"""Gate 3: install the declared components with explicit ownership.

Issue #5533 (w6-10), EPIC #4910. Design item 1: "install the declared
namespace/CRDs/components using explicit cluster-scoped ownership and a single-
controller handover".

## What "components" means here, after F2

Namespace, CRDs, the workspace controller, and the scoped RBAC the controller runs
under. The first revision stopped after the namespace and the CRDs and returned
success — review finding F2 — which left a workspace with the types the controller
reconciles and nothing reconciling them. `_refuse_existing_controller` required ZERO
controllers on entry and `readiness._controller_checks` required EXACTLY ONE
afterwards, and no step in between made that transition, so readiness could not
verify on any cluster. `_install_controller` is that step.

The permissions granted are not a parameter of this module: the seam takes only a
namespace and a service account, and the rules come from
`readiness.REQUIRED_CONTROLLER_PERMISSIONS`, which is the same set
`readiness._rbac_checks` verifies afterwards. A caller able to pass its own rules
could grant cluster-admin and still satisfy the later check.

## The two hazards, and why they need different answers

**Cluster-scoped objects outlive the thing that made them.** `nodepools.superplane.ai`
is a CRD with `scope: Cluster` (see `src/superplane-controller/deploy/crds.yaml`).
It is not namespaced, so it is not cleaned up when a namespace goes away, and two
workspaces on one cluster share it. That makes "who owns this CRD" a question with
a wrong answer that destroys data: a cleanup that deletes a CRD deletes every
custom resource of that type across every workspace on the cluster. So CRDs are
recorded as SHARED and `retire.py` never deletes them — establishing a CRD is
idempotent and leaving it costs nothing, while removing one is unbounded.

**A namespace named the same is not the same namespace.** A namespace identified by
name can be a different object than the one this bootstrap created — deleted and
recreated by somebody else in between. So the created namespace's `uid` is recorded
and cleanup requires it, mirroring `installation/cluster_probe.py::__exit__`, which
verifies uid and an ownership label before deleting its preflight namespace and
passes `preconditions.uid` on the delete itself.

## Why an existing namespace is adopted rather than recreated or refused

A pre-existing workspace namespace is the normal state of a re-run: bootstrap is
required to be idempotent (design item 3), and taking a namespace that already has
the right labels and moving on is what makes a retry safe. But adoption is only
safe when the namespace is actually equivalent — so labels are compared, and a
namespace whose admission labels differ is refused rather than relabelled. Silently
relabelling somebody else's namespace is the AC-02 violation: on a supplied cluster
that namespace may hold workloads ADP knows nothing about, and changing its Pod
Security policy could stop them scheduling.

An adopted namespace is recorded as NOT owned, so cleanup leaves it. This is the
asymmetry that matters for BYOC: ADP deletes what it created and nothing else.

## Ownership comes from the durable record, never from a label (F3)

The first revision inferred ownership from the namespace LABEL
`superplane.aws-e/bootstrap-owner == workspace_id`. Review finding F3: neither the
label key nor the workspace id is a secret, so a BYOC owner or an unrelated prior
process can create a namespace carrying that label — and the code then recorded the
observed uid *as if ADP had created it*, after which `retire.py` planned to delete
that namespace and every workload in it. Observing a label, or a uid, does not
establish who created an object.

So ownership is now `state.namespace_ownership()`: the durable record written at the
successful create, cross-checked against the live uid. The label is still STAMPED on
namespaces this bootstrap creates — it is how an operator sees attribution by reading
the cluster — but it is no longer *read* as evidence of ownership. A correctly
labelled pre-existing namespace is adopted and preserved.

## Partial progress is recorded before the next mutation (F6)

The namespace is created before the CRDs are established, so a CRD failure happens
with an owned namespace already on the cluster. The first revision built
`ComponentInstallation` only after every CRD was present, so that failure raised with
no installation record and `bootstrap_workspace` returned `cleanup=None` — an owned
namespace with no rollback plan, which is one of AC-01's named failure modes.

Two changes fix it. The namespace creation is recorded in durable state immediately
after it succeeds, and `install_components` attaches the partial
`ComponentInstallation` to the refusal itself (`BootstrapRefused.installation`), so
the orchestration can build an exact cleanup plan from a gate that raised rather than
returned.

The same applies to each later mutation. The controller's RBAC is created before the
controller, so a controller failure refuses with the Role and RoleBinding already in
the record — unlike the shared CRDs those are namespaced objects this workspace owns
alone, and a cleanup plan that omitted them would leave them behind permanently,
since nothing else in the system knows they exist.

## The single-controller handover

`installation/runner.py::Installer.workspace()` already refuses to install when any
Deployment's image contains `superplane-controller`: *"An existing workspace
controller must complete its explicit handover before installation."* Two
controllers reconciling the same NodePools fight — each sees the other's nodes as
unmanaged and acts, which is a live scaling loop rather than a config error, and
the cluster-scoped CRD means the second controller need not even be in this
workspace's namespace to do it. This gate applies the same refusal at install time
and reads across ALL namespaces, because a namespaced read cannot see the case it
exists to catch. AC-01 names "duplicate controllers".

One exception, and it is the durable record's: now that this module installs the
controller, the controller found on a retry is normally the one the previous attempt
installed. Refusing it would make bootstrap succeed exactly once and refuse every
retry, contradicting design item 3, and would leave an operator no path forward but
deleting the controller ADP had just installed. So a recorded install excuses exactly
ONE controller — two still refuse, because one of them is not ours.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .access import ClusterAccess, ObservedNamespace, ObservedWorkload
from .admission import tenant_namespace_labels
from .errors import BootstrapRefused, failure_kind
from .state import (
    ADP_CREATED,
    BootstrapState,
    NamespaceRecord,
    StateStore,
    namespace_ownership,
    record,
)
from .target import VerifiedTarget

# Stamped on every namespace this package creates. Cleanup requires it in addition
# to the recorded uid: the uid proves object identity, and the label lets an
# operator see ownership by reading the cluster, without consulting a state file
# that may not exist by then. `installation/cluster_probe.py` uses the same
# label-plus-uid pairing for the same reason.
BOOTSTRAP_OWNER_LABEL = "superplane.aws-e/bootstrap-owner"

# Substring identifying a workspace controller Deployment, matched against image
# references. Same marker `installation/runner.py::Installer.workspace()` uses, so
# the install-time refusal and the preflight refusal cannot disagree about what
# counts as a controller.
CONTROLLER_IMAGE_MARKER = "superplane-controller"

# The ServiceAccount the workspace controller runs as, and the subject the scoped RBAC
# established here is bound to. Named explicitly rather than using the namespace's
# `default` account: every pod in the namespace gets `default` automatically, so
# binding the controller's permissions to it would grant them to any tenant workload
# that happens to land there.
CONTROLLER_SERVICE_ACCOUNT = "superplane-controller"

# The CRDs a workspace requires, matching `kubectl get crd` in
# `Installer.workspace()`'s preflight. Both are declared in
# `src/superplane-controller/deploy/crds.yaml`; nothing in the repo applied them to
# a workspace cluster before this package.
WORKSPACE_CRDS: tuple[str, ...] = (
    "nodepools.superplane.ai",
    "superplanenodes.superplane.ai",
)


@dataclass(frozen=True)
class InstalledObject:
    """One object this bootstrap established, and whether it may ever be deleted.

    `owned` drives cleanup and is the whole reason this record exists. `uid` is
    populated for namespaced objects whose identity can be re-used by a different
    object of the same name; it is empty for shared cluster-scoped objects, which
    are never deleted anyway.
    """

    kind: str
    name: str
    owned: bool
    uid: str = ""
    shared: bool = False

    def __post_init__(self) -> None:
        if not self.kind.strip() or not self.name.strip():
            raise BootstrapRefused("InstalledObject requires a kind and a name")
        if self.owned and self.shared:
            raise BootstrapRefused(
                f"{self.kind}/{self.name} is recorded as both owned and shared; "
                "cleanup would have to both delete it and preserve it"
            )


@dataclass(frozen=True)
class ComponentInstallation:
    """What was established, for the target it was established on.

    Carries the target's `workspace_id` so a record cannot be applied to the wrong
    workspace's cleanup, and the namespace uid separately because cleanup's most
    important precondition should not require walking a list to find.
    """

    workspace_id: str
    cluster_arn: str
    namespace: str
    namespace_uid: str
    namespace_owned: bool
    objects: tuple[InstalledObject, ...] = field(default_factory=tuple)

    @property
    def owned_objects(self) -> tuple[InstalledObject, ...]:
        """Only what this bootstrap created — the complete set cleanup may touch."""
        return tuple(obj for obj in self.objects if obj.owned)


def _refuse_existing_controller(access: ClusterAccess, state: BootstrapState) -> None:
    """Refuse if a controller this bootstrap did not install is reconciling the cluster.

    Read across all namespaces. See the module docstring on why two controllers is
    a live scaling loop rather than a config error.

    `state.controller_installed` is the exception, and it has to be: now that this
    module INSTALLS the controller (F2), the controller found on a retry is usually the
    one the previous attempt installed. Refusing it would make bootstrap succeed exactly
    once and refuse every retry, breaking the idempotence design item 3 requires — and
    an operator whose retry refuses with "a controller already exists" has no path
    forward except deleting the controller ADP just installed.

    The durable record is what distinguishes the two cases, for the same reason it
    decides namespace ownership in F3: the cluster read alone cannot tell "the
    controller we installed" from "somebody else's controller", and only one of those
    is safe to proceed past. Exactly one controller must be present either way, which
    `readiness._controller_checks` re-verifies after installation.
    """
    existing = [
        image
        for image in access.controller_deployments()
        if CONTROLLER_IMAGE_MARKER in image
    ]
    if not existing:
        return
    if (
        getattr(access, "controller_mode", None) != "management"
        and state.controller_installed
        and len(existing) == 1
    ):
        return
    raise BootstrapRefused(
        f"{len(existing)} existing workspace controller deployment(s) already "
        "reconcile this cluster and this bootstrap's durable record does not account "
        "for them; an existing controller must complete its explicit handover before "
        "installation. Two controllers reconciling the same cluster-scoped NodePools "
        "contend continuously rather than failing cleanly"
    )


def _namespace_labels(target: VerifiedTarget, enforce_version: str) -> dict[str, str]:
    """Admission labels plus the ownership stamp.

    Admission labels come from `admission.py` rather than being restated, so the
    labels the namespace is CREATED with are by construction the labels the
    isolation gate later REQUIRES. Restating them here is how a namespace gets
    created with `enforce: baseline` and then fails its own proof.
    """
    labels = tenant_namespace_labels(enforce_version)
    labels[BOOTSTRAP_OWNER_LABEL] = target.workspace_id
    return labels


def _adopt_or_create_namespace(
    access: ClusterAccess,
    target: VerifiedTarget,
    namespace: str,
    enforce_version: str,
    store: StateStore,
    state: BootstrapState,
) -> tuple[ObservedNamespace, bool, BootstrapState]:
    """Return the workspace namespace, whether ADP owns it, and the updated state.

    Ownership is decided by `state.namespace_ownership` — the durable creation record
    cross-checked against the live uid — and never by reading a label. See the module
    docstring on F3: a label is forgeable, and ownership authorizes deletion.

    A successful create is recorded in durable state BEFORE this returns, so a process
    killed immediately afterwards still leaves an accurate account of the namespace it
    owns (F6).
    """
    required = _namespace_labels(target, enforce_version)
    existing = access.namespace(namespace)
    if existing is None:
        created = access.create_namespace(namespace, required)
        if created.name != namespace:
            raise BootstrapRefused(
                f"created namespace is named {created.name!r}, not the requested "
                f"{namespace!r}"
            )
        if not created.uid.strip():
            raise BootstrapRefused(
                f"the created namespace {namespace!r} reported no uid; refusing to "
                "record ownership that cleanup could not later verify, because a "
                "delete identified only by name can hit a different object"
            )
        # Recorded immediately: this is the mutation whose loss caused F6, and the
        # record is what makes the ownership in the next line a fact rather than an
        # inference.
        state = record(
            store,
            state,
            namespace=NamespaceRecord(name=created.name, uid=created.uid),
        )
        return created, True, state

    # Adoption path. Compare only the admission labels: the ownership stamp is
    # absent on a namespace this bootstrap did not create, and requiring it would
    # turn every legitimate BYOC adoption into a refusal.
    divergent = [
        key
        for key, value in required.items()
        if key != BOOTSTRAP_OWNER_LABEL and existing.labels.get(key) != value
    ]
    if divergent:
        raise BootstrapRefused(
            f"namespace {namespace!r} already exists with different admission "
            f"labels ({', '.join(sorted(divergent))}); refusing to relabel a "
            "namespace this bootstrap did not create, because on a supplied cluster "
            "it may hold workloads whose scheduling depends on the current policy"
        )
    # F3: the durable record decides, not `existing.labels[BOOTSTRAP_OWNER_LABEL]`.
    # A pre-existing namespace carrying a correct-looking ownership label is ADOPTED,
    # because a label proves nothing about who created the object.
    ownership = namespace_ownership(state, name=namespace, observed_uid=existing.uid)
    return existing, ownership == ADP_CREATED, state


def _install_controller(
    access: ClusterAccess,
    namespace: str,
    controller_name: str,
    service_account: str,
    partial: ComponentInstallation,
) -> tuple[tuple[InstalledObject, ...], ObservedWorkload | None]:
    """Establish the controller's scoped RBAC and then the controller itself.

    Order matters and is not interchangeable. The RBAC comes first because a
    controller Deployment that starts before its Role exists spends its first
    reconcile loops being denied by the API server, and a workspace whose controller
    is crash-looping on authorization is indistinguishable at the readiness gate from
    one whose controller image is wrong.

    Both steps carry `partial` onto any refusal for the same F6 reason
    `install_components` does: the namespace and CRDs already exist by the time this
    runs, so a controller failure that raised bare would leave owned objects with no
    cleanup plan. The objects created here are appended to the record as OWNED —
    unlike the CRDs, a Role and a Deployment in the workspace namespace belong to this
    workspace alone, so cleanup may and must remove them.
    """
    created: list[InstalledObject] = []
    try:
        rbac = access.establish_controller_rbac(namespace, service_account)
    except BootstrapRefused as refusal:
        if getattr(refusal, "installation", None) is None:
            refusal.installation = partial
        raise
    except Exception as error:
        # F9: the STAGE and the exception TYPE, never the exception's own text. A
        # kubectl/RBAC failure arrives here as whatever the adapter's subprocess or SDK
        # raised, and that string can carry a bearer token or a request body.
        raise BootstrapRefused(
            f"establishing the workspace controller's scoped RBAC in {namespace!r} "
            f"failed with {failure_kind(error)}. The controller must not be started "
            "without it, because a controller denied by the API server looks the same "
            "at the readiness gate as one that was never installed",
            installation=partial,
        ) from error

    # A seam that reported nothing created is a seam whose result cleanup cannot act
    # on. Refusing here rather than continuing keeps "installed" and "recorded" the
    # same set, which is the property F6 is about.
    if not rbac:
        raise BootstrapRefused(
            "the cluster reported no RBAC objects created for the workspace "
            f"controller in {namespace!r}; refusing to start a controller whose "
            "permissions cannot be named, because cleanup could not later remove them",
            installation=partial,
        )
    created.extend(
        _controller_object(access, str(kind), str(name), namespace)
        for kind, name in sorted(rbac.items())
    )
    if getattr(access, "controller_mode", None) == "management":
        # The manager runs in the management cluster. Its workspace credential
        # uses only this observation RBAC; no execution secret or second
        # controller Deployment is installed in the customer namespace.
        return tuple(created), None

    existing = access.workload(namespace, controller_name)
    if existing is not None:
        if getattr(access, "component_journal", None) is not None:
            # Revalidate and adopt only through the durable component journal;
            # name/image observations alone never establish deletion ownership.
            existing = access.install_controller(
                namespace, controller_name, service_account
            )
        # Already installed by a previous attempt. Not re-installed, and deliberately
        # not re-read for availability here: whether it is AVAILABLE is
        # `readiness.py`'s question, and answering it in two places is how the two
        # answers come to differ. It is still recorded as owned, because this bootstrap
        # created it and cleanup must remove it.
        created.append(
            _controller_object(access, "Deployment", controller_name, namespace)
        )
        return tuple(created), existing

    try:
        observed = access.install_controller(
            namespace, controller_name, service_account
        )
    except BootstrapRefused as refusal:
        if getattr(refusal, "installation", None) is None:
            refusal.installation = dataclasses.replace(
                partial, objects=partial.objects + tuple(created)
            )
        raise
    except Exception as error:
        # F9, same reason as the RBAC wrapper above.
        raise BootstrapRefused(
            f"installing the workspace controller {controller_name!r} in "
            f"{namespace!r} failed with {failure_kind(error)}",
            installation=dataclasses.replace(
                partial, objects=partial.objects + tuple(created)
            ),
        ) from error

    if observed.name != controller_name or observed.namespace != namespace:
        raise BootstrapRefused(
            f"the installed controller reports itself as "
            f"{observed.namespace}/{observed.name}, not {namespace}/{controller_name}; "
            "refusing to record a workload the readiness gate would then look for "
            "under a different name and not find",
            installation=dataclasses.replace(
                partial, objects=partial.objects + tuple(created)
            ),
        )
    created.append(_controller_object(access, "Deployment", controller_name, namespace))
    return tuple(created), observed


def _controller_object(access, kind, name, namespace):
    journal = getattr(access, "component_journal", None)
    if journal is None:
        return InstalledObject(kind=kind, name=name, owned=True)
    import json

    key = json.dumps(
        [kind, "" if kind.startswith("Cluster") else namespace, name],
        separators=(",", ":"),
    )
    _, progress = journal.journal.read()
    component = progress.get("components", {}).get(key, {})
    if component.get("phase") not in {"owned", "adopted"}:
        raise BootstrapRefused("installed component ownership is not durable")
    return InstalledObject(
        kind=kind,
        name=name,
        owned=component["phase"] == "owned",
        uid=component["identity"]["uid"],
    )


def _installation_record(
    target: VerifiedTarget,
    observed: ObservedNamespace,
    namespace_owned: bool,
    crds: Sequence[str],
    extra: Sequence[InstalledObject] = (),
) -> ComponentInstallation:
    """Build the installation record, including for a PARTIAL install.

    Shared by the success path and the F6 refusal path so a partial record describes
    the same objects with the same ownership as a complete one would — a separately
    constructed "partial" record is how the two drift and cleanup starts planning
    something different from what was created.
    """
    objects = [
        InstalledObject(
            kind="Namespace",
            name=observed.name,
            owned=namespace_owned,
            uid=observed.uid,
        )
    ]
    objects.extend(
        # Cluster-scoped and shared between every workspace on this cluster, so
        # never owned and never deleted. See the module docstring.
        InstalledObject(
            kind="CustomResourceDefinition", name=name, owned=False, shared=True
        )
        for name in crds
    )
    objects.extend(extra)
    return ComponentInstallation(
        workspace_id=target.workspace_id,
        cluster_arn=target.cluster_arn,
        namespace=observed.name,
        namespace_uid=observed.uid,
        namespace_owned=namespace_owned,
        objects=tuple(objects),
    )


def install_components(
    *,
    access: ClusterAccess,
    target: VerifiedTarget,
    namespace: str,
    enforce_version: str,
    store: StateStore,
    state: BootstrapState,
    controller_name: str,
    controller_service_account: str = CONTROLLER_SERVICE_ACCOUNT,
    required_crds: Sequence[str] = WORKSPACE_CRDS,
) -> tuple[ComponentInstallation, BootstrapState]:
    """Establish the workspace namespace and CRDs, recording exact ownership.

    Idempotent: a re-run adopts what it finds if it is equivalent, and refuses if it
    is not. Installs nothing until the single-controller handover check passes,
    because a namespace created next to a contending controller is a namespace
    cleanup now has to reason about.

    Ownership comes from the durable record (F3) and every mutation is recorded before
    the next one is attempted (F6). Any refusal raised after the namespace exists
    carries the partial `ComponentInstallation` on the exception, so the orchestration
    can still produce an exact cleanup plan.

    The controller and its scoped RBAC are installed here too (F2). The first revision
    returned success after the namespace and CRDs, leaving nothing to reconcile the
    types it had just established: `_refuse_existing_controller` required zero
    controllers on entry and `readiness._controller_checks` required exactly one after,
    and no step made that transition. `readiness.py` then verifies what this installed
    rather than trusting it.
    """
    if not namespace.strip():
        raise BootstrapRefused("a workspace namespace name is required")
    if not controller_name.strip():
        raise BootstrapRefused(
            "a workspace controller name is required; without one this gate would "
            "establish the CRDs and leave nothing to reconcile them"
        )
    if not controller_service_account.strip():
        raise BootstrapRefused(
            "a controller service account name is required; the scoped RBAC is bound "
            "to it, and an unnamed subject would mean granting the namespace default"
        )
    if not required_crds:
        raise BootstrapRefused(
            "no required CRDs were supplied; an empty set would let this gate pass "
            "without establishing the types the controller reconciles"
        )

    _refuse_existing_controller(access, state)
    from .permissions import verify_install_permissions

    verify_install_permissions(
        access, namespace, controller_service_account, controller_name, required_crds
    )

    observed, namespace_owned, state = _adopt_or_create_namespace(
        access, target, namespace, enforce_version, store, state
    )

    # From here on the namespace may exist and may be owned, so every refusal has to
    # carry the partial record. `partial` is what F6 asked for: "propagate partial
    # installation state on refusal".
    partial = _installation_record(target, observed, namespace_owned, ())

    try:
        established = set(access.establish_crds(tuple(required_crds)))
    except BootstrapRefused as refusal:
        # A refusal from the seam itself. Re-raise carrying the partial state rather
        # than letting it escape bare — the review's "exception-raising CRD install".
        if getattr(refusal, "installation", None) is None:
            refusal.installation = partial
        raise
    except Exception as error:
        # Any other exception from a real adapter (a subprocess failure, a transport
        # error) is converted here so the partial record is never lost to an
        # exception type this package does not control.
        # F9: `error` here is a real adapter's transport failure, so its text is the
        # least bounded of the three. Type only; `__cause__` keeps the rest.
        raise BootstrapRefused(
            f"establishing the required CRDs failed with {failure_kind(error)}; the "
            "workspace namespace already exists, so the cleanup plan on this refusal "
            "is the record of it",
            installation=partial,
        ) from error

    missing = [name for name in required_crds if name not in established]
    if missing:
        raise BootstrapRefused(
            "required CRDs are not established after installation: "
            + ", ".join(sorted(missing))
            + ". The controller reconciles these types, so a workspace registered "
            "without them would accept work it cannot act on",
            installation=partial,
        )

    state = record(store, state, crds_established=tuple(required_crds))

    # F2/F6: the controller comes after the types it reconciles, and the partial record
    # handed to it already names the namespace and the CRDs, so a controller failure
    # refuses with the complete account of what exists.
    partial = _installation_record(target, observed, namespace_owned, required_crds)
    controller_objects, _ = _install_controller(
        access, namespace, controller_name, controller_service_account, partial
    )
    state = record(
        store,
        state,
        controller_installed=getattr(access, "controller_mode", None) != "management",
    )
    return (
        _installation_record(
            target, observed, namespace_owned, required_crds, controller_objects
        ),
        state,
    )


def observed_labels(installation: ComponentInstallation) -> Mapping[str, str]:
    """The ownership stamp a reader can expect on the namespace, if owned.

    Exposed so `retire.py` and the tests agree on the expected stamp without either
    restating the label key.
    """
    if not installation.namespace_owned:
        return {}
    return {BOOTSTRAP_OWNER_LABEL: installation.workspace_id}
