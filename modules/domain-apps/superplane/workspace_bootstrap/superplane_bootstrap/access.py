"""The seams: what bootstrap needs from a provider, a cluster and a store.

Issue #5533 (w6-10), EPIC #4910.

## Why these are Protocols and not clients

This package makes decisions; it does not own transport. Every external fact it
needs arrives through one of the Protocols below, and the production
implementations live where the credential to obtain them lives — provider access
under the bound operation (#5534), cluster access through the scoped access entry
this package establishes, and the registration store in the domain API
(`src/superplane-api/app/installation_bootstrap.py`, which already writes the
`Cluster`/`Workspace` rows keyed on `eks_cluster_arn` and `namespace_name`).

Three consequences, all of them the point:

1. **The whole suite is offline.** Tests pass fakes, so every refusal path is
   exercised without an AWS account, a cluster or a database. `#5540 AC-03` keeps
   the live evidence; nothing here can forge it.
2. **No credential is held by this package.** A Protocol method returns an
   observation, never a kubeconfig or a token. There is no field anywhere in this
   package that could hold one, which is a stronger guarantee than remembering
   not to log it.
3. **The refusals are testable as refusals.** A fake that raises, or that returns
   a subtly wrong CA, is how the negative cases in AC-01 get actual evidence
   rather than a docstring claiming the check exists.

## Why the observations are frozen dataclasses rather than dicts

A dict of provider output invites `observed.get("status") == "ACTIVE"`, which
silently passes when the key is missing or misspelled. Every field below is
required at construction, so a fake — or a future real adapter — that omits the
cluster status cannot produce an object at all. The failure lands at the seam
where it is legible instead of three gates later as a false pass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .errors import BootstrapRefused


def _require_text(value: object, what: str) -> str:
    """A required, non-blank string. Blank is refused, never defaulted.

    A defaulted identity is how a run acts on a cluster nobody named — the same
    reasoning `../infra/account-factory/account_factory/modes.py` records for its
    required request fields.
    """
    if not isinstance(value, str) or not value.strip():
        raise BootstrapRefused(f"{what} is required and must be a non-blank string")
    return value


@dataclass(frozen=True)
class ProviderIdentity:
    """Who the provider says the caller currently is.

    Read immediately before a gate that depends on it, never cached from an
    earlier phase: `../infra/workspaces/README.md` records the same requirement
    for apply ("Apply re-resolves STS/IAM immediately before Terraform"), because
    a credential can change between phases and the stale answer is the dangerous
    one.
    """

    account_id: str
    principal_arn: str

    def __post_init__(self) -> None:
        _require_text(self.account_id, "ProviderIdentity.account_id")
        _require_text(self.principal_arn, "ProviderIdentity.principal_arn")


@dataclass(frozen=True)
class ClusterIdentity:
    """What the provider says about the cluster, as the provider says it.

    `certificate_authority_data` is the PUBLIC certificate a client uses to verify
    the API server. It is not a credential — it authenticates the SERVER to the
    client — which is why `../infra/workspaces/outputs.tf` publishes it unmarked
    and why comparing it here is a TLS-identity check rather than a secret
    comparison.

    `status` is carried verbatim rather than as a bool so a refusal can say what
    the cluster actually was ("CREATING", "FAILED") instead of only that it was
    not ACTIVE.
    """

    name: str
    arn: str
    region: str
    account_id: str
    endpoint: str
    certificate_authority_data: str
    status: str
    oidc_issuer_url: str = ""
    version: str = ""

    def __post_init__(self) -> None:
        for name in (
            "name",
            "arn",
            "region",
            "account_id",
            "endpoint",
            "certificate_authority_data",
            "status",
        ):
            _require_text(getattr(self, name), f"ClusterIdentity.{name}")


@dataclass(frozen=True)
class ObservedNamespace:
    """A namespace as the cluster reports it, with the fields ownership depends on.

    `uid` is what makes cleanup safe. A namespace identified by NAME can be a
    different object than the one this bootstrap created — deleted and recreated
    by somebody else in between — so `retire.py` requires the recorded uid and
    refuses on a mismatch. `installation/cluster_probe.py::__exit__` already
    establishes this pattern for the management cluster's temporary namespace.
    """

    name: str
    uid: str
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.name, "ObservedNamespace.name")
        _require_text(self.uid, "ObservedNamespace.uid")


@dataclass(frozen=True)
class ObservedPod:
    """A pod the cluster admitted or refused, as observed by a dry-run request.

    Used by the admission gate. `admitted` is the API server's answer to a
    dry-run create; `rejected_reason` carries why, so a refusal can distinguish
    "admission rejected this correctly" from "the request failed for an unrelated
    reason", which are opposite outcomes for a negative test.
    """

    name: str
    admitted: bool
    rejected_reason: str = ""


@dataclass(frozen=True)
class ObservedWorkload:
    """A Deployment as the cluster reports it, with the replica counts readiness needs.

    Added by the F2 repair. Both counts are carried because "present" and "available"
    are different facts and the gap between them is the failure this story is about: on
    the selected deployment CoreDNS EXISTS with `availableReplicas: 0`, unschedulable
    behind the bootstrap taint. A seam that returned only presence could not express
    that, so the readiness gate would pass on the exact cluster state the live handoff
    recorded as broken.

    `desired_replicas` is separate from `available_replicas` so a refusal can say
    "0/2 available" (pending) rather than only "not available", and so a workload
    scaled deliberately to zero is a distinguishable case rather than a mystery.
    """

    name: str
    namespace: str
    desired_replicas: int
    available_replicas: int

    def __post_init__(self) -> None:
        _require_text(self.name, "ObservedWorkload.name")
        _require_text(self.namespace, "ObservedWorkload.namespace")
        for field_name in ("desired_replicas", "available_replicas"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise BootstrapRefused(
                    f"ObservedWorkload.{field_name} must be an int; a non-numeric "
                    "replica count cannot be compared and would read as available"
                )
            if value < 0:
                raise BootstrapRefused(
                    f"ObservedWorkload.{field_name} cannot be negative"
                )


@runtime_checkable
class ClusterAccess(Protocol):
    """Scoped Kubernetes access to ONE workspace cluster.

    Every method is read-or-create against objects this bootstrap owns. There is
    deliberately no general `apply(manifest)` and no `delete(name)`: a seam wide
    enough to mutate anything is a seam wide enough to mutate a supplied
    cluster's existing workloads, which is exactly what AC-02 forbids.
    """

    def bind_target(self, target: object, binding: object) -> None:
        """Bind the actual Kubernetes transport to the verified target and authority."""

    def custom_resource_definitions(self) -> Sequence[str]:
        """Names of CRDs currently established on the cluster."""

    def controller_deployments(self) -> Sequence[str]:
        """Images of every controller-like Deployment, across all namespaces.

        All namespaces, not just the workspace's: a second controller reconciling
        this workspace from elsewhere is the failure the single-controller gate
        exists to catch, and it would be invisible to a namespaced read.
        """

    def bootstrap_permission(
        self,
        *,
        verb: str,
        resource: str,
        namespace: str | None = None,
        name: str | None = None,
    ) -> bool:
        """Effective authorization of the actual bootstrap worker; unanswered refuses."""

    def namespace(self, name: str) -> ObservedNamespace | None:
        """The named namespace, or None if absent. None means absent, not denied."""

    def create_namespace(
        self, name: str, labels: Mapping[str, str]
    ) -> ObservedNamespace:
        """Create the namespace with exactly these labels; return it as observed."""

    def establish_crds(self, names: Sequence[str]) -> Sequence[str]:
        """Install the named CRDs; return the names now established."""

    def establish_controller_rbac(
        self, namespace: str, service_account: str
    ) -> Mapping[str, str]:
        """Create the controller's scoped Role/Binding and ServiceAccount.

        Added by the F2 repair, which found that nothing established the scoped RBAC
        the controller runs under. The permissions granted are NOT a parameter: they
        come from `readiness.REQUIRED_CONTROLLER_PERMISSIONS`, which is also the set
        `readiness._rbac_checks` verifies afterwards. A caller that could pass its own
        rules could grant cluster-admin and still satisfy the later check, so the
        widest thing this seam can create is fixed by the code rather than the call.

        Returns the created object names keyed by kind, so `install_components` can
        record exactly what it made and cleanup can remove exactly that. An
        implementation is idempotent: a re-run of a bootstrap must not fail because
        the Role it would create already exists with the same rules.
        """

    def install_controller(
        self, namespace: str, name: str, service_account: str
    ) -> ObservedWorkload:
        """Install the workspace controller Deployment; return it as observed.

        Added by the F2 repair. The first revision declared readiness with no
        controller installed anywhere — `_refuse_existing_controller` required zero
        controllers before install and `readiness._controller_checks` required exactly
        one after, and nothing in between made that transition. This is that step.

        The image is deliberately not a parameter of this Protocol: choosing a
        controller version is the release owner's decision, not a decision the
        bootstrap sequence should be able to make per call. The adapter carries the
        image reference it was constructed with.

        Returns the workload as READ back rather than as applied, for the same reason
        `create_namespace` re-reads: `kubectl apply` succeeding is not the fact the
        readiness gate needs, and a Deployment with `availableReplicas: 0` is the
        exact state F2 was about.
        """

    def dry_run_pod(self, namespace: str, spec: Mapping[str, object]) -> ObservedPod:
        """Ask the API server whether it would admit this pod. Creates nothing."""

    def imds_reachable_from_tenant_pod(self, namespace: str) -> Mapping[str, bool]:
        """Whether a normal tenant pod can reach IMDS, keyed by "ipv4"/"ipv6".

        Returns reachability, never a token or a metadata response body — the
        live proof recorded in this story's handoff emitted neither, and a seam
        that could return one would make that property depend on the caller.
        """

    def can_tenant_change_admission_labels(self, namespace: str) -> bool:
        """Whether a tenant-scoped identity can weaken this namespace's PSA labels."""

    def cni_credential_scope(self) -> Mapping[str, object]:
        """How the CNI obtains credentials, and what the node role can do.

        The workspace infrastructure module declares "aws-node uses its dedicated
        IRSA role; node role has no CNI or account-wide ECR permissions" as a
        required proof and publishes `cni_role_arn` in
        `tenant_scheduling_prerequisites` so this can be checked against a stated
        expectation rather than eyeballed.

        Required keys, each of which must be present — a missing key is an
        unanswered question, not a negative answer:

        - `aws_node_role_arn` (str): the IRSA role ARN the `aws-node` service
          account is annotated with. Blank means it is using the node role.
        - `node_role_has_cni_permissions` (bool): whether the node instance role
          still carries CNI permissions. If it does, a pod that reaches the node's
          credentials inherits them, so the dedicated IRSA role buys nothing.
        - `node_role_has_account_wide_ecr` (bool): whether the node role can pull
          from any repository in the account rather than the workspace's own.

        Returns role ARNs and booleans only — never a token, a session credential
        or a policy document that might embed one.
        """

    def node_taints(self) -> Sequence[Mapping[str, str]]:
        """Taints currently present on the workspace's nodes."""

    def remove_bootstrap_taint(self, key: str) -> Sequence[Mapping[str, str]]:
        """Remove the named bootstrap taint; return the taints that remain."""

    def restore_bootstrap_taint(self, key: str) -> Sequence[Mapping[str, str]]:
        """Re-apply the named bootstrap taint; return the taints now present.

        Added by the F5 repair. Removing the taint is the one action here whose
        EFFECT is immediate — nodes become schedulable — and the review found that a
        registration refusal or a store write failure after removal left them that way
        with no attempt to undo it. This is the undo.

        It is a separate method from `create_namespace` and friends deliberately: this
        seam can re-apply exactly the bootstrap taint and nothing else, so it cannot be
        used to taint a supplied cluster's nodes with anything of ADP's choosing.

        Returning the taints now present rather than a bool is what lets
        `workspace.py` verify the restoration actually took effect, instead of
        reporting a tidy failure over nodes that are still schedulable.
        """

    def workload(self, namespace: str, name: str) -> ObservedWorkload | None:
        """The named Deployment with its replica counts, or None if absent.

        Added by the F2 repair. None means absent, not denied — the same convention as
        `namespace`. A readiness check needs to distinguish absent (nothing installed
        it) from present-but-unavailable (installed and not serving), because those
        have different causes and different operator actions.
        """

    def place_system_workloads(
        self, namespace: str, names: Sequence[str]
    ) -> Mapping[str, bool]:
        """Make the named SYSTEM workloads schedulable; return which were placed.

        Added by the F2 repair, for the cluster's own services — CoreDNS above all —
        which the live handoff records as unschedulable behind the bootstrap taint.

        The namespace is a parameter so the call site names it explicitly and a reader
        can see it is never the tenant namespace. The workloads are named individually
        rather than passed as a selector or a manifest: there is no way to express
        "tolerate the bootstrap taint everywhere" through this seam, which is what
        keeps preparing the system side from becoming a way to bypass the tenant
        interlock. `readiness.py` re-verifies that a tenant pod is still unschedulable
        afterwards regardless.
        """

    def tenant_scheduling_denied(self, namespace: str) -> bool | None:
        """Whether a TENANT pod in this namespace is still unschedulable.

        Added by the F2 repair. Returns None when the cluster could not answer, and
        None is treated as a refusal rather than as "denied": an unanswered question is
        not a negative answer, the same rule the admission probes follow. The dangerous
        direction here is a probe that fails and reads as "still safely blocked".
        """

    def controller_handover(self, namespace: str) -> Mapping[str, object]:
        """Whether a previous workspace controller completed its explicit handover.

        Added by the F2 repair. `installation/runner.py::Installer.workspace()` demands
        that "an existing workspace controller must complete its explicit handover
        before installation", and the first revision checked only whether a controller
        Deployment existed. A controller whose Deployment is gone but whose coordination
        lease is still held has not handed over, and it may still be reconciling.

        Required key `complete` (bool). Optional `holder` (str) names the lease holder
        so a refusal can say who has not let go. A missing `complete` key is an
        unanswered question and refuses — see `cni_credential_scope` for the same rule.
        """

    def controller_permissions(self, namespace: str) -> Mapping[tuple[str, str], bool]:
        """What the workspace controller credential may do, keyed by (verb, resource).

        Added by the F2 repair, for the scoped RBAC the first revision never
        established. The keys mirror the `kubectl auth can-i` pairs
        `installation/runner.py::Installer.workspace()` already checks, so a workspace
        this gate accepts is one that installer preflight will also accept.

        An ABSENT key means the cluster was not asked or could not answer, which
        `readiness.py` refuses on; it does not mean denied. Present-and-False means
        genuinely denied. That distinction is why this returns a mapping of booleans
        rather than a set of granted pairs: a set cannot express "unknown", and unknown
        collapsing to "denied" would hide a broken probe behind a plausible refusal,
        while collapsing to "granted" would pass a controller that cannot work.
        """


@runtime_checkable
class RegistrationStore(Protocol):
    """The domain's record of workspace targets.

    `read` returning None means "no record", which is why `register_workspace`
    can treat a present record as a replay to reconcile rather than a conflict to
    overwrite. The production implementation is the domain API's bootstrap path,
    which already takes a PostgreSQL advisory lock and refuses rebinding.

    ## Why there is a reserve/finalize pair and not only `write` (F5)

    The first revision had `read` then `write`, and the orchestration called both
    AFTER removing the bootstrap taint. Review finding F5: a conflicting binding was
    therefore discovered — and a store write could fail — with the nodes already
    schedulable for a bootstrap that was then rejected, and nothing restored the
    taint.

    `reserve` moves the conflict decision in front of the irreversible action, while
    it is still free to refuse against an untouched cluster. `finalize` completes the
    record after readiness is actually proved. The split is what makes the dangerous
    window small and recoverable rather than merely unlikely.

    The production implementation is `app/installation_bootstrap.py`'s pattern: a
    `pg_advisory_xact_lock` keyed on the workspace, then `session.get(...,
    with_for_update=True)` on the `Cluster` and `Workspace` rows, refusing when
    `eks_cluster_arn` differs. `reserve` is that lock-and-compare; `finalize` is the
    commit.
    """

    def read(self, workspace_id: str) -> object | None:
        """The existing registration for this workspace, or None."""

    def reserve(
        self, workspace_id: str, identity: Mapping[str, str]
    ) -> Mapping[str, object]:
        """Atomically claim this workspace for this identity, before any mutation.

        `identity` is the pre-mutation immutable identity — everything known before a
        namespace exists. The implementation must make the check-and-claim atomic
        against a concurrent attempt: two bootstraps racing for one workspace must
        produce one reservation and one refusal, never two reservations.

        Required key `reserved` (bool). When false, `conflict` (str) must say what
        already holds the workspace, because a refusal an operator cannot act on is
        barely better than the race it prevented. Optional `replayed` (bool) marks a
        COMPLETED registration being re-run — the idempotent case, which must not be
        refused as a conflict.

        A missing `reserved` key is an unanswered question and refuses.

        ## F10: `attempt_token`, and why a same-identity claim is not a replay

        `reserved: True` for a NEW claim must also carry `attempt_token` (str): opaque
        material identifying this attempt, which `finalize` and `release` then require.

        Review finding F10: an implementation that returned `reserved: True` for an
        existing unfinalized reservation with a matching identity authorized a SECOND
        concurrent bootstrap of the same workspace. The advisory lock the production store
        takes is transaction-scoped — it is released when `reserve` commits, and every step
        it exists to guard runs after that — so "the row is already here and it matches" is
        indistinguishable from a live competitor. An implementation must refuse it.

        Taking over an abandoned claim is a separate decision, made by
        `workspace.recover_interrupted_bootstrap` from the durable state file, which is the
        only thing that can say the previous attempt was this workspace's own and is gone.

        Serializing the check-and-claim is therefore necessary and NOT sufficient: the
        store must also be able to tell the holder from anyone else after the transaction
        that took the claim has committed.
        """

    def finalize(self, target: object, attempt_token: str = "") -> None:
        """Complete the reserved registration. Called only after readiness is proved.

        Separate from `reserve` so the record becomes visible to the rest of the domain
        exactly once, at the point where the workspace is genuinely usable.

        `attempt_token` is the one `reserve` issued to this attempt. An implementation must
        refuse when it does not match the claim being completed: F10's fence is worth
        nothing if the last step — the one that publishes the record — does not check it.
        """

    def release(self, workspace_id: str, attempt_token: str = "") -> bool:
        """Drop THIS attempt's unfinalized reservation; return whether it was released.

        Called on the refusal path when bootstrap fails after reserving. Returns a
        bool rather than nothing so `workspace.py` can report an unreleased reservation
        explicitly instead of implying a clean rollback — a reservation left behind
        blocks every later attempt for this workspace, so a silent failure here is a
        workspace nobody can bootstrap.

        `attempt_token` fences it. F10's second half: "a losing process may also release
        the shared reservation while the winner is active, causing the winner's
        finalization to fail and re-taint the workspace". An implementation must drop only
        the claim this token holds.

        There is deliberately no `write` method. The pre-F5 revision had one, and
        keeping it beside `finalize` would leave two ways to create the same record —
        one of which skips the reservation entirely. A single writer is what makes "the
        record appears only after readiness" a property of this seam rather than
        something every caller has to remember.
        """

    def recover_claim(self, workspace_id, fingerprint, *, restore):
        """Fence restoration and release together; return matched/restored/released."""
        ...

    def release_claim(self, workspace_id: str, claim_fingerprint: str) -> bool:
        """Drop the reservation matching this fingerprint; return whether it was released.

        The recovery path's release. A process killed between reserving and finalizing
        takes the sole copy of its token with it, so something must be able to clear a
        claim whose holder is gone — otherwise the F10 fence would have converted a
        concurrent-mutation defect into a denial of service, one crash per workspace.

        **F13: that path is fenced too, and this method replaced `release_abandoned`.** The
        old signature took only a workspace id and deleted whichever `reserved` row it
        found, on the grounds that the durable state file authorized it. The file held a
        boolean, so it could say a claim was outstanding and not which one — and a stale
        record was therefore accepted as authority over a LIVE successor's claim, whose
        deletion admitted a third concurrent writer.

        `claim_fingerprint` is `state.claim_fingerprint(attempt_token)`: a one-way digest,
        because the recovering process by definition does not hold the token and a durable
        record holding one would be a durable copy of the permission to publish. An
        implementation must compare it against the claim it is deleting in a SINGLE
        statement — a read followed by a delete leaves a window for the row to change, and
        a possibly-stale record acting on a row that changed is exactly this finding.

        A fingerprint matching no reservation must release nothing and report so; that is
        the stale case and doing nothing is correct. Blank must be refused rather than
        treated as a wildcard. A completed registration is never dropped here either.
        """
