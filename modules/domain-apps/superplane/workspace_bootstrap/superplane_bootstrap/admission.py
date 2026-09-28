"""Gate 2: prove tenant isolation before clearing the bootstrap interlock.

Issue #5533 (w6-10), EPIC #4910.

## The interlock, and why it is not a nuisance

`../infra/workspaces/eks.tf` gives every new workspace node the taint
`superplane.aws-e/bootstrap=pending:NoSchedule`. Nothing tenant-owned can be
scheduled while it is there. That taint is the only thing standing between "a
cluster exists" and "a tenant's pods are running on nodes whose credentials they
may be able to steal", so clearing it is the single most consequential action in
this package.

The infrastructure module says so itself, in the description of its
`tenant_scheduling_prerequisites` output: *"Bootstrap must prove these controls
before removing the pending taint and registering the workspace as usable. This
output is a requirement, not observed readiness."*

## Why the proof list is READ from Terraform rather than restated here

`required_proofs()` parses the list out of the infrastructure module's own output
rather than hardcoding an equivalent tuple. If this package restated the list, the
two could drift — and the drift that matters is one-directional and silent:
somebody adds a required control upstream, this package keeps checking the old
five, and the gap shows up as a workspace registered as usable without the new
control. Reading the published list means an upstream addition breaks this
package's tests instead.

`test_admission.py::test_the_proof_list_matches_the_infrastructure_declaration`
is what makes that real: it reads `../infra/workspaces/outputs.tf` as text.

## How declared controls map to checks, and why not by position

`DECLARED_PROOF_CHECKS` maps each declared control to the name of the check that
discharges it, matched on a distinctive substring of the declared text. An earlier
draft of this module matched them *by position* — declared entry N was assumed to
be discharged by check N — which is wrong in a way worth recording, because it
looks like it works:

- The declared list's fifth entry is *"Only after these proofs, remove the
  bootstrap taint through the bounded bootstrap owner"*. That is an **ordering
  rule about this gate's caller**, not a property of the cluster to probe. Under
  positional mapping it became a proof with no check, recorded as permanently
  unverified, and `may_clear_taint` could then never be true for any cluster —
  the gate would deadlock every workspace. It is discharged structurally instead:
  `workspace.py` calls `remove_bootstrap_taint` only when `may_clear_taint` is
  true, and `test_workspace.py` asserts the taint survives every failure mode.
- Reordering the declared list without changing its content would have silently
  repaired different checks to different requirements.

So the mapping is by content, `ORDERING_PROOFS` names the entries discharged by
construction, and an unrecognized declared entry is reported as an uncovered
requirement naming the text — which is the honest answer when somebody adds a
control upstream that nothing here checks.

## Why "unknown" is a refusal in every one of these checks

A proof here answers a question about an attacker's capability. "I could not
determine whether a tenant pod can reach IMDS" and "a tenant pod cannot reach
IMDS" are opposite answers, and the natural implementation conflates them — a
probe that errors returns falsy, which reads as "not reachable", which reads as
"safe". So each check requires an explicit observation for each thing it claims,
and a missing key is refused rather than treated as absence of reachability. This
is the `UnknownOutcome.RAISE_UNAVAILABLE` shape the shared contract names for
ports whose only honest answer to an unanswered question is to not return one.

## What the live handoff already demonstrated, and what it did not

A temporary namespace on the selected cluster passed actual packet tests, IMDS
token requests over IPv4 and IPv6 returned nothing, and API-server dry runs
rejected hostNetwork/hostPID/privileged/hostPath under `restricted:v1.35`. Those
prove the cluster is CAPABLE of these controls. They were performed in a
since-deleted proof namespace under operator-scoped access, so they are not proof
about the persistent workspace namespace or the final tenant identity — which is
why this gate re-runs them against the actual target and why the taint stays until
it does. A re-apply of the infrastructure can restore the taint, so these checks
are re-runnable by construction rather than one-shot.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .access import ClusterAccess
from .errors import BootstrapRefused

# Pod Security admission labels. `enforce` is what actually rejects a pod;
# `warn`/`audit` only annotate, so a namespace carrying only those is not
# protected. `enforce-version` is pinned rather than `latest` on purpose: a
# floating version means the policy a workspace was admitted under can change
# under it at an upgrade, and the proofs below were taken against a specific one.
RESTRICTED_ENFORCE_LABEL = "pod-security.kubernetes.io/enforce"
RESTRICTED_ENFORCE_VERSION_LABEL = "pod-security.kubernetes.io/enforce-version"
RESTRICTED_POLICY = "restricted"

# The escape hatches a tenant must not be able to request. Each one is a distinct
# route to the node: host networking reaches the node's network namespace (and so
# IMDS), host PID reaches other processes' memory, privileged lifts capability
# checks, and a hostPath mount reaches the node's filesystem including the
# kubelet's credentials. Rejecting four of five is not a pass.
UNSAFE_POD_FIELDS = ("hostNetwork", "hostPID", "privileged", "hostPath")

# The IMDS address families that must both be unreachable. IPv6 is listed
# explicitly because it is the one that gets forgotten: a node can disable the
# IPv4 endpoint and leave `fd00:ec2::254` answering.
IMDS_FAMILIES = ("ipv4", "ipv6")

# Which check discharges which declared control, matched on a distinctive fragment
# of the declared text rather than on list position. See the module docstring for
# why position was wrong. Keys are lowercase substrings of the declared entry.
DECLARED_PROOF_CHECKS: Mapping[str, str] = {
    "restricted pod security admission": "restricted_admission_enforced",
    "unable to change namespace policy labels": "tenant_cannot_weaken_admission",
    "cannot reach ipv4 or ipv6 imds": "tenant_pod_cannot_reach_imds",
    "rejected by admission": "unsafe_pod_requests_rejected",
    "dedicated irsa role": "cni_credentials_scoped",
}

# Declared entries that state an ordering constraint on this gate's CALLER rather
# than a cluster property to probe. They are discharged structurally — see the
# module docstring — so they are not reported as uncovered.
ORDERING_PROOFS: tuple[str, ...] = ("only after these proofs",)

_PROOFS_BLOCK = re.compile(r"required_proofs\s*=\s*\[(?P<body>.*?)\]", re.DOTALL)
_PROOF_ENTRY = re.compile(r'"(?P<text>(?:[^"\\]|\\.)*)"')

# `workspace_bootstrap/` -> `superplane/` -> `infra/workspaces/outputs.tf`
_WORKSPACE_OUTPUTS = (
    Path(__file__).resolve().parents[2] / "infra" / "workspaces" / "outputs.tf"
)
if Path(__file__).with_name("_data").is_dir():
    _WORKSPACE_OUTPUTS = Path(__file__).with_name("_data") / "outputs.tf"


def required_proofs(outputs_path: Path | None = None) -> tuple[str, ...]:
    """The proof list the workspace infrastructure module declares.

    Read from `infra/workspaces/outputs.tf` rather than restated, so an upstream
    addition cannot be silently ignored here. Refuses if the block is absent or
    empty: an empty requirement list would let this gate pass trivially, which is
    the worst possible failure mode for it.
    """
    path = outputs_path or _WORKSPACE_OUTPUTS
    try:
        source = path.read_text()
    except OSError as error:
        raise BootstrapRefused(
            "cannot read the workspace infrastructure module's declared scheduling "
            f"prerequisites at {path}; refusing rather than assuming a proof list"
        ) from error
    match = _PROOFS_BLOCK.search(source)
    if match is None:
        raise BootstrapRefused(
            "the workspace infrastructure module declares no required_proofs list; "
            "refusing rather than proceeding with no requirements"
        )
    proofs = tuple(
        entry.group("text").strip()
        for entry in _PROOF_ENTRY.finditer(match.group("body"))
        if entry.group("text").strip()
    )
    if not proofs:
        raise BootstrapRefused(
            "the declared required_proofs list is empty; an empty requirement list "
            "would let the isolation gate pass trivially"
        )
    return proofs


def tenant_namespace_labels(enforce_version: str) -> dict[str, str]:
    """The admission labels every tenant namespace must carry.

    A pinned `enforce_version` is required — `"latest"` is refused. Under
    `latest`, the policy a namespace is evaluated against changes at a cluster
    upgrade, so the admission behaviour this gate proved is not the behaviour the
    namespace keeps. `installation/cluster_probe.py` uses `latest` for a namespace
    that lives for the duration of one preflight and is then deleted; a persistent
    tenant namespace is a different case.
    """
    if not isinstance(enforce_version, str) or not enforce_version.strip():
        raise BootstrapRefused(
            "a pinned Pod Security enforce-version is required for a tenant namespace"
        )
    if enforce_version.strip() == "latest":
        return _refuse_latest()
    return {
        RESTRICTED_ENFORCE_LABEL: RESTRICTED_POLICY,
        RESTRICTED_ENFORCE_VERSION_LABEL: enforce_version.strip(),
    }


def _refuse_latest() -> dict[str, str]:
    raise BootstrapRefused(
        "Pod Security enforce-version must be pinned, not 'latest': under a "
        "floating version the policy a tenant namespace is evaluated against can "
        "change at a cluster upgrade, so the admission behaviour proved at "
        "bootstrap is not the behaviour the namespace keeps"
    )


@dataclass(frozen=True)
class AdmissionProof:
    """One named control, with the observation that established it.

    `detail` is required when `verified` is False, mirroring the shared contract's
    `ProvisioningProgress`, which requires a detail when the state is UNKNOWN: an
    unexplained negative is indistinguishable from a check that never ran.
    """

    name: str
    verified: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise BootstrapRefused("AdmissionProof.name is required")
        if not self.verified and not self.detail.strip():
            raise BootstrapRefused(
                f"AdmissionProof {self.name!r} is unverified with no detail; an "
                "unexplained negative cannot be told apart from a check that did "
                "not run"
            )


@dataclass(frozen=True)
class IsolationEvidence:
    """The complete set of proofs, and whether the interlock may be cleared.

    `may_clear_taint` is a computed property rather than a stored flag so no
    caller can construct evidence that claims clearance it did not earn.
    """

    namespace: str
    enforce_version: str
    proofs: tuple[AdmissionProof, ...] = field(default_factory=tuple)

    @property
    def unverified(self) -> tuple[str, ...]:
        return tuple(proof.name for proof in self.proofs if not proof.verified)

    @property
    def may_clear_taint(self) -> bool:
        """True only when every declared proof has an actual verified result."""
        return bool(self.proofs) and not self.unverified


def _imds_proof(observed: Mapping[str, bool]) -> AdmissionProof:
    """Both address families must be explicitly observed as unreachable.

    A missing key is refused rather than read as "not reachable" — see the module
    docstring on why unknown is not safe here.
    """
    missing = [family for family in IMDS_FAMILIES if family not in observed]
    if missing:
        return AdmissionProof(
            name="tenant_pod_cannot_reach_imds",
            verified=False,
            detail=(
                "no observation for IMDS over " + ", ".join(missing) + "; an "
                "unobserved address family is not an unreachable one"
            ),
        )
    reachable = [family for family in IMDS_FAMILIES if observed[family]]
    if reachable:
        return AdmissionProof(
            name="tenant_pod_cannot_reach_imds",
            verified=False,
            detail=(
                "a normal tenant pod reached IMDS over "
                + ", ".join(reachable)
                + "; node credentials are obtainable from tenant workloads"
            ),
        )
    return AdmissionProof(name="tenant_pod_cannot_reach_imds", verified=True)


def _unsafe_pod_proof(access: ClusterAccess, namespace: str) -> AdmissionProof:
    """Every host escape hatch must be rejected by admission, checked one at a time.

    One dry-run per field rather than one pod requesting all four: a single
    combined pod that is rejected proves only that SOMETHING was rejected, and a
    cluster that rejects `privileged` while admitting `hostPath` would pass it.
    """
    admitted: list[str] = []
    for unsafe in UNSAFE_POD_FIELDS:
        observed = access.dry_run_pod(
            namespace, {"name": f"bootstrap-probe-{unsafe.lower()}", unsafe: True}
        )
        if observed.admitted:
            admitted.append(unsafe)
    if admitted:
        return AdmissionProof(
            name="unsafe_pod_requests_rejected",
            verified=False,
            detail=(
                "admission accepted a tenant pod requesting "
                + ", ".join(admitted)
                + "; each of these reaches the node directly"
            ),
        )
    # A control: the same seam must ADMIT a conforming pod. Without this, a seam
    # that rejects everything — a broken probe, a missing namespace, a quota —
    # would read as perfect isolation.
    safe = access.dry_run_pod(namespace, {"name": "bootstrap-probe-conforming"})
    if not safe.admitted:
        return AdmissionProof(
            name="unsafe_pod_requests_rejected",
            verified=False,
            detail=(
                "admission also rejected a conforming pod "
                f"({safe.rejected_reason or 'no reason given'}), so the rejections "
                "above do not establish selective enforcement"
            ),
        )
    return AdmissionProof(name="unsafe_pod_requests_rejected", verified=True)


def _namespace_label_proof(
    observed: Mapping[str, str], enforce_version: str
) -> AdmissionProof:
    if observed.get(RESTRICTED_ENFORCE_LABEL) != RESTRICTED_POLICY:
        return AdmissionProof(
            name="restricted_admission_enforced",
            verified=False,
            detail=(
                "namespace does not enforce the restricted Pod Security policy "
                f"(observed {observed.get(RESTRICTED_ENFORCE_LABEL)!r}); warn and "
                "audit only annotate and reject nothing"
            ),
        )
    if observed.get(RESTRICTED_ENFORCE_VERSION_LABEL) != enforce_version:
        return AdmissionProof(
            name="restricted_admission_enforced",
            verified=False,
            detail=(
                "namespace enforce-version is "
                f"{observed.get(RESTRICTED_ENFORCE_VERSION_LABEL)!r}, not the "
                f"pinned {enforce_version!r} the proofs were taken against"
            ),
        )
    return AdmissionProof(name="restricted_admission_enforced", verified=True)


def _cni_scope_proof(
    observed: Mapping[str, object], expected_cni_role_arn: str
) -> AdmissionProof:
    """The CNI must use its own IRSA role, and the node role must not duplicate it.

    Both halves are required. A dedicated IRSA role for `aws-node` buys nothing
    while the node instance role still carries the same CNI permissions, because
    anything that reaches the node's credentials inherits them — and the IMDS proof
    above is what is supposed to prevent that, so treating these as independent
    would mean a single IMDS regression silently un-scopes the CNI too.

    `expected_cni_role_arn` comes from `tenant_scheduling_prerequisites.cni_role_arn`,
    so "has some IRSA role" is not accepted for "has the declared one".
    """
    name = "cni_credentials_scoped"
    required = (
        "aws_node_role_arn",
        "node_role_has_cni_permissions",
        "node_role_has_account_wide_ecr",
    )
    missing = [key for key in required if key not in observed]
    if missing:
        return AdmissionProof(
            name=name,
            verified=False,
            detail=(
                "no observation for " + ", ".join(missing) + "; an unanswered "
                "question about credential scope is not a negative answer"
            ),
        )

    aws_node_role = observed["aws_node_role_arn"]
    if not isinstance(aws_node_role, str) or not aws_node_role.strip():
        return AdmissionProof(
            name=name,
            verified=False,
            detail=(
                "the aws-node service account has no dedicated IRSA role, so the "
                "CNI is using the node instance role"
            ),
        )
    if not expected_cni_role_arn.strip():
        return AdmissionProof(
            name=name,
            verified=False,
            detail=(
                "no expected CNI role ARN was supplied, so 'has some IRSA role' "
                "cannot be distinguished from 'has the declared one'"
            ),
        )
    if aws_node_role.strip() != expected_cni_role_arn.strip():
        return AdmissionProof(
            name=name,
            verified=False,
            detail=(
                "aws-node uses an IRSA role other than the one the workspace "
                "infrastructure declared in cni_role_arn"
            ),
        )
    if observed["node_role_has_cni_permissions"]:
        return AdmissionProof(
            name=name,
            verified=False,
            detail=(
                "the node instance role still carries CNI permissions, so the "
                "dedicated IRSA role does not narrow what a workload reaching node "
                "credentials can do"
            ),
        )
    if observed["node_role_has_account_wide_ecr"]:
        return AdmissionProof(
            name=name,
            verified=False,
            detail=(
                "the node instance role can pull from any ECR repository in the "
                "account rather than only this workspace's"
            ),
        )
    return AdmissionProof(name=name, verified=True)


def _coverage_proofs(
    declared: Sequence[str], checked: Sequence[str]
) -> list[AdmissionProof]:
    """One unverified proof per declared control that nothing here actually checked.

    This is the mechanism that turns an upstream addition into a visible failure
    instead of a silent coverage gap. Matched by content, not position.
    """
    gaps: list[AdmissionProof] = []
    for text in declared:
        lowered = text.lower()
        if any(fragment in lowered for fragment in ORDERING_PROOFS):
            # Discharged structurally by workspace.py's call ordering.
            continue
        expected_check = next(
            (
                check
                for fragment, check in DECLARED_PROOF_CHECKS.items()
                if fragment in lowered
            ),
            None,
        )
        if expected_check is None:
            gaps.append(
                AdmissionProof(
                    name="declared_requirement_without_a_check",
                    verified=False,
                    detail=(
                        "the workspace infrastructure module declares a control "
                        "this bootstrap has no check for: " + text
                    ),
                )
            )
        elif expected_check not in checked:
            gaps.append(
                AdmissionProof(
                    name="declared_requirement_without_a_check",
                    verified=False,
                    detail=(
                        f"the check {expected_check!r} that discharges a declared "
                        "control did not run: " + text
                    ),
                )
            )
    return gaps


def prove_tenant_isolation(
    *,
    access: ClusterAccess,
    namespace: str,
    enforce_version: str,
    expected_cni_role_arn: str,
    declared_proofs: Sequence[str] | None = None,
) -> IsolationEvidence:
    """Run every declared isolation proof against the actual target namespace.

    Returns evidence whether or not it verifies — the caller decides, and a
    refusal that returned nothing would give the operator no way to see WHICH
    control failed. Nothing here clears the interlock; `workspace.py` does that,
    and only when `may_clear_taint` is true.

    `declared_proofs` defaults to the infrastructure module's published list and
    is injectable only so a test can show that an added upstream requirement is
    noticed rather than ignored.
    """
    declared = (
        tuple(declared_proofs) if declared_proofs is not None else required_proofs()
    )
    if not declared:
        raise BootstrapRefused(
            "no isolation proofs were declared; refusing rather than clearing the "
            "bootstrap interlock against an empty requirement list"
        )

    observed_namespace = access.namespace(namespace)
    if observed_namespace is None:
        raise BootstrapRefused(
            f"namespace {namespace!r} is absent, so no admission control can be "
            "proved for it"
        )

    proofs = [
        _namespace_label_proof(observed_namespace.labels, enforce_version),
        _imds_proof(access.imds_reachable_from_tenant_pod(namespace)),
        _unsafe_pod_proof(access, namespace),
        _cni_scope_proof(access.cni_credential_scope(), expected_cni_role_arn),
    ]

    if access.can_tenant_change_admission_labels(namespace):
        proofs.append(
            AdmissionProof(
                name="tenant_cannot_weaken_admission",
                verified=False,
                detail=(
                    "a tenant-scoped identity can change this namespace's Pod "
                    "Security labels, so the admission policy above is advisory "
                    "against the tenant it constrains"
                ),
            )
        )
    else:
        proofs.append(
            AdmissionProof(name="tenant_cannot_weaken_admission", verified=True)
        )

    # Every control the infrastructure declares must map to a check that actually
    # ran; anything unmatched becomes an unverified proof rather than being dropped.
    proofs.extend(_coverage_proofs(declared, [proof.name for proof in proofs]))

    return IsolationEvidence(
        namespace=namespace,
        enforce_version=enforce_version,
        proofs=tuple(proofs),
    )
