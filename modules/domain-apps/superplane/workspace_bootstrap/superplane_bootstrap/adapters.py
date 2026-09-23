"""Production adapters: the seams, implemented against real `aws` and `kubectl`.

Issue #5533 (w6-10), EPIC #4910. Added by the F1 repair.

## What was missing

Review finding F1: `ClusterAccess` and `RegistrationStore` were Protocols with no
implementation anywhere, and `bootstrap_workspace` had no caller outside its own tests.
Merging that revision changed no production behaviour — the gates were correct and
unreachable. This module is the reachable half.

## Why subprocesses rather than boto3 and the Kubernetes client

`installation/` — the module whose `Installer.workspace()` preflight *checks* exactly
what this package *establishes* — obtains every fact by running `aws` and `kubectl`
through one wrapper, `runner.py::Commands.call`. Matching it is deliberate:

- The two must agree about what counts as established. If bootstrap created access via
  boto3 and preflight verified it via `kubectl auth can-i`, the two could disagree
  about the same cluster and the disagreement would surface as an unexplained preflight
  failure after a successful bootstrap.
- `installation/` has no declared dependencies and its CI lane installs only
  `modules/gateway[dev]`; the domain module has no pyproject at all. A boto3 or
  kubernetes-client import here would add an undeclared runtime dependency to a
  standard-library-only package. `src/superplane-api` declares both and is free to use
  them; this package is not that package.
- The CLI is the same shape an operator already runs for installation, so the
  operational surface is one thing rather than two.

## The command runner is injected, which is what makes this testable offline

`CommandRunner` is a Protocol with exactly one method. The tests pass a scripted runner
that returns canned stdout for expected argv and raises for anything unexpected, so
every adapter path — including the failure paths — is exercised with no network, no
cluster and no credential. `SubprocessRunner` is the production implementation and is
the ONLY place in this package that calls `subprocess.run`.

That boundary is the reason the integration tests in `tests/test_integration.py` can
drive `bootstrap_workspace` end to end through these real adapters and assert every
gate ran, which is precisely what F1 asked for.

## No decisions live here

Every method returns an observation or raises. There is no `if` in this module that
decides whether bootstrap may proceed — those all live in the gate modules, where they
are tested. An adapter that started making decisions would be making them in the one
layer whose tests cannot reach every branch without a real cluster.

## No credential is held, logged or returned

The runner strips the installer's two secret env vars, exactly as
`Commands.call` does. No method returns a kubeconfig, a token or a session credential:
`cni_credential_scope` returns role ARNs and booleans, `controller_permissions` returns
booleans. A failure raises `BootstrapRefused` naming the command that failed and its
exit status, never its output — a refusal is frequently the thing that lands in a log.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

from .access import (
    ClusterIdentity,
    ObservedNamespace,
    ObservedPod,
    ObservedWorkload,
    ProviderIdentity,
)
from .components import CONTROLLER_IMAGE_MARKER
from .errors import BootstrapRefused
from .readiness import (
    FORBIDDEN_CONTROLLER_PERMISSIONS,
    REQUIRED_CONTROLLER_PERMISSIONS,
)

# Env vars never passed to a child process. Same two `installation/runner.py::Commands`
# strips, for the same reason: a subprocess that does not need a secret must not be able
# to read one out of its environment or echo it into a log.
_STRIPPED_ENV: frozenset[str] = frozenset(
    {"SUPERPLANE_VERIFICATION_TOKEN", "SUPERPLANE_DATABASE_ADMIN_URL"}
)

# Default timeout, matching `Commands.call`.
_TIMEOUT = 120

# The bootstrap taint key, restated here rather than imported from `workspace.py`
# because that module imports this one's siblings and the import would be a cycle. The
# two are pinned to the same value by `tests/test_adapters.py`, so a change in one that
# is not mirrored in the other fails a test rather than silently tainting nothing.
_BOOTSTRAP_TAINT_KEY = "superplane.aws-e/bootstrap"

# Which of `REQUIRED_CONTROLLER_PERMISSIONS`' resources are cluster-scoped. Nodes and
# CRD-defined types are not namespaced, so a Role in the workspace namespace cannot grant
# them however it is written — those need a ClusterRole, and the rest must NOT be
# cluster-scoped or the controller would hold them in every namespace on the cluster.
# Splitting the set by this table is what makes "exactly the required permissions, and
# only those" expressible as RBAC.
_CLUSTER_SCOPED_RESOURCES: frozenset[str] = frozenset(
    {
        "nodes",
        "nodepools.superplane.ai",
        "superplanenodes.superplane.ai",
    }
)


def _rbac_rules(resources: Sequence[str]) -> list[dict[str, object]]:
    """Group `REQUIRED_CONTROLLER_PERMISSIONS` into RBAC rules for these resources.

    Derived from the required set rather than restated, which is the same discipline
    `_namespace_labels` follows for admission labels. A hand-written rule list here could
    drift from the set `readiness._rbac_checks` verifies, and the failure would be a
    workspace that installs and then fails its own readiness gate with no explanation.

    The verbs are grouped per resource so the generated Role says `verbs: [create,
    update]` for leases rather than repeating the resource once per verb — the same rule
    either way, but the readable form is the one an operator can audit with
    `kubectl get role -o yaml`.
    """
    wanted = set(resources)
    verbs: dict[str, list[str]] = {}
    for verb, resource in REQUIRED_CONTROLLER_PERMISSIONS:
        if resource in wanted:
            verbs.setdefault(resource, []).append(verb)

    rules: list[dict[str, object]] = []
    for resource in sorted(verbs):
        # A resource written as `name.group` in the permission set carries its API group
        # after the first dot; a bare name is core (""). This is the same spelling
        # `kubectl auth can-i` accepts, which is why the set uses it.
        name, _, group = resource.partition(".")
        rules.append(
            {
                "apiGroups": [group],
                "resources": [name],
                "verbs": sorted(verbs[resource]),
            }
        )
    return rules


#: The image probe pods name. Never pulled — `--dry-run=server` stops at admission, so
#: no node ever resolves this reference. Named rather than blank because a Pod with no
#: container image fails validation, and a validation failure is indistinguishable from
#: an admission rejection at the exit-status level.
_PROBE_IMAGE = "public.ecr.aws/docker/library/busybox:stable"


def _probe_pod(
    name: str, namespace: str, requested: Mapping[str, object]
) -> dict[str, object]:
    """Build a real Pod object for one abstract admission probe.

    `admission.py` deliberately says WHAT to probe (`{"hostPID": True}`) and not how to
    spell it, because where each unsafe field lives in the Pod schema is the adapter's
    problem. They are in three different places: `hostNetwork`/`hostPID`/`hostIPC` are
    pod-spec booleans, `privileged` is per-container under `securityContext`, and
    `hostPath` is a volume source. A single flat dict of them — which is what was sent
    before — is not a Pod at all.

    The base pod satisfies `restricted` on purpose: `runAsNonRoot`, a dropped ALL
    capability set, `allowPrivilegeEscalation: false` and a seccomp profile. That matters
    for the CONTROL probe, the one with no unsafe field requested. If the base were
    merely minimal, admission would reject it for missing those fields, and
    `_unsafe_pod_proof` reads a rejected control as "the rejections above do not
    establish selective enforcement" — a correct cluster would fail its own proof.
    """
    container: dict[str, object] = {
        "name": "probe",
        "image": _PROBE_IMAGE,
        "command": ["/bin/true"],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            "runAsUser": 65534,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
    }
    pod: dict[str, object] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            # Never actually scheduled, but a probe that outlived its dry run would be a
            # tenant pod on an unproved node, so the policy is stated regardless.
            "restartPolicy": "Never",
            "containers": [container],
        },
    }
    spec: dict[str, object] = pod["spec"]  # type: ignore[assignment]

    for field_name in ("hostNetwork", "hostPID", "hostIPC"):
        if requested.get(field_name):
            spec[field_name] = True
    if requested.get("privileged"):
        security: dict[str, object] = container["securityContext"]  # type: ignore[assignment]
        security["privileged"] = True
        # `privileged` and `allowPrivilegeEscalation: false` together are contradictory
        # and rejected as INVALID rather than as a policy violation, which would make
        # this probe pass for the wrong reason.
        security["allowPrivilegeEscalation"] = True
    if requested.get("hostPath"):
        spec["volumes"] = [{"name": "host", "hostPath": {"path": "/"}}]
        container["volumeMounts"] = [{"name": "host", "mountPath": "/host"}]

    return pod


@dataclass(frozen=True)
class CommandResult:
    """One command's exit status and streams."""

    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@runtime_checkable
class CommandRunner(Protocol):
    """Runs one external command. The only transport in this package.

    One method on purpose. A wider seam — "run this shell string", "set this env" —
    would let a caller express things the adapters below cannot, and the value of this
    Protocol is that a scripted test double can cover it completely.
    """

    def run(
        self, args: Sequence[str], *, data: str | None = None, timeout: int = _TIMEOUT
    ) -> CommandResult:
        """Execute the command. Returns its result; does not interpret it."""


class SubprocessRunner:
    """The production runner. The only `subprocess.run` call in this package.

    Mirrors `installation/runner.py::Commands.call`: captures both streams, passes input
    on stdin rather than in argv (so a value never appears in a process listing), strips
    the two secret env vars, and converts a missing binary or a timeout into a refusal
    rather than letting an OSError escape into a gate's exception handler.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        source = os.environ if env is None else env
        self._env = {k: v for k, v in source.items() if k not in _STRIPPED_ENV}

    def run(
        self, args: Sequence[str], *, data: str | None = None, timeout: int = _TIMEOUT
    ) -> CommandResult:
        argv = tuple(str(arg) for arg in args)
        try:
            completed = subprocess.run(  # noqa: S603 - argv is a list, never a shell string
                argv,
                input=data,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=self._env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BootstrapRefused(
                f"{Path(argv[0]).name} is unavailable or timed out; the bootstrap step "
                "did not complete, so nothing may be treated as established"
            ) from error
        return CommandResult(
            args=argv,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )


def _require_success(result: CommandResult, what: str) -> CommandResult:
    """A non-zero exit is a refusal naming the command, never its output.

    The output is deliberately not interpolated: `kubectl` and `aws` echo request
    bodies and occasionally tokens into stderr, and a refusal message is the thing most
    likely to end up in an issue comment.
    """
    if result.returncode != 0:
        raise BootstrapRefused(
            f"{what} failed: {Path(result.args[0]).name} exited "
            f"{result.returncode}. The step did not complete"
        )
    return result


def _parse_json(result: CommandResult, what: str) -> object:
    """Parse a command's stdout as JSON, refusing unparseable output.

    Matches `Installer.json`. Unparseable output is a refusal rather than an empty
    default, because an empty default for "does this CRD exist" reads as a confident
    no.
    """
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise BootstrapRefused(
            f"{what} did not return valid JSON; refusing to infer an answer from "
            "unparseable output"
        ) from error


@runtime_checkable
class NodeRoleFacts(Protocol):
    """The IAM half of the CNI credential-scope proof.

    Split from `KubectlClusterAccess` because these are IAM facts read with `aws iam`,
    not cluster facts read with `kubectl` — a different credential and a different API.
    A Protocol rather than a concrete type for the same reason every other seam here is
    one: the keys it must produce are then assertable without an AWS account.
    """

    def cni_credential_facts(self) -> Mapping[str, object]:
        """The two node-role keys `admission._cni_scope_proof` requires.

        Must return both `node_role_has_cni_permissions` and
        `node_role_has_account_wide_ecr` as booleans when they can be determined, and
        OMIT a key it could not determine. Omission is the safe direction: the proof
        treats a missing key as an unanswered question and refuses, whereas a defaulted
        `False` would read as a verified negative and clear the interlock on an
        unobserved fact.
        """


@dataclass(frozen=True)
class IamNodeRoleFacts:
    """`NodeRoleFacts` via `aws iam simulate-principal-policy`.

    Simulation rather than a policy-document read on purpose: the question is what the
    node role CAN DO, and that is the union of its attached managed policies, its inline
    policies and any permission boundary. Parsing documents ourselves would re-implement
    IAM evaluation, and the failure mode of getting that wrong is reporting a node role
    as scoped when it is not — which is the exact claim this proof exists to make.

    `node_role_arn` comes from the workspace module's `node_role_arn` output, so the role
    being simulated is the one the infrastructure declares, not one inferred from a
    cluster read.
    """

    runner: CommandRunner
    node_role_arn: str

    #: The CNI actions a dedicated IRSA role is supposed to be the ONLY holder of. If
    #: the node role can still call these, anything that reaches node credentials
    #: inherits the CNI's power and the separate role buys nothing.
    CNI_ACTIONS: ClassVar[tuple[str, ...]] = (
        "ec2:CreateNetworkInterface",
        "ec2:AttachNetworkInterface",
        "ec2:AssignPrivateIpAddresses",
    )

    #: Account-wide ECR pull. Checked against `*` deliberately: a node role scoped to
    #: the workspace's own repositories is fine, and one that can pull every image in
    #: the account is a cross-tenant read.
    ECR_ACTIONS: ClassVar[tuple[str, ...]] = ("ecr:BatchGetImage",)

    def __post_init__(self) -> None:
        if not self.node_role_arn.strip():
            raise BootstrapRefused(
                "no node role ARN was supplied; the CNI credential-scope proof compares "
                "the node role's permissions against the dedicated IRSA role and cannot "
                "be run without knowing which role the nodes assume"
            )

    def cni_credential_facts(self) -> Mapping[str, object]:
        """Both node-role keys, each omitted rather than guessed when unreadable."""
        facts: dict[str, object] = {}
        cni = self._allows(self.CNI_ACTIONS, resource="*")
        if cni is not None:
            facts["node_role_has_cni_permissions"] = cni
        ecr = self._allows(self.ECR_ACTIONS, resource="*")
        if ecr is not None:
            facts["node_role_has_account_wide_ecr"] = ecr
        return facts

    def _allows(self, actions: Sequence[str], *, resource: str) -> bool | None:
        """True if ANY action is allowed, False if all are denied, None if unreadable.

        ANY rather than ALL: the claim being tested is "the node role does not carry CNI
        permissions", and one allowed action falsifies it. Requiring all of them would
        report a role holding two of the three as scoped.

        None on a failed call, never False — an `aws iam` call that fails has not
        established that anything is denied, and `cni_credential_scope` omits the key so
        the proof refuses instead of passing on it.
        """
        result = self.runner.run(
            (
                "aws",
                "iam",
                "simulate-principal-policy",
                "--policy-source-arn",
                self.node_role_arn,
                "--action-names",
                *actions,
                "--resource-arns",
                resource,
                "--output",
                "json",
            )
        )
        if result.returncode != 0:
            return None
        try:
            payload = _parse_json(result, "aws iam simulate-principal-policy")
        except BootstrapRefused:
            return None
        if not isinstance(payload, Mapping):
            return None
        results = payload.get("EvaluationResults")
        if not isinstance(results, list) or not results:
            # No evaluation came back, so nothing was evaluated. Not a denial.
            return None
        decisions = []
        for entry in results:
            if not isinstance(entry, Mapping):
                return None
            decision = entry.get("EvalDecision")
            if not isinstance(decision, str) or not decision:
                return None
            decisions.append(decision)
        if len(decisions) != len(actions):
            # A partial answer cannot distinguish "denied" from "not asked about".
            return None
        return any(decision == "allowed" for decision in decisions)


@dataclass
class KubectlClusterAccess:
    """`ClusterAccess` against a real cluster, via `kubectl`.

    Pins the verified kubeconfig to an adapter-owned mode-0600 snapshot inside
    a private temporary directory. Every command uses that snapshot, so replacing
    the caller's file after verification cannot redirect a mutation. The snapshot
    is removed on close; no credential content is returned or logged.

    `manifests` maps a CRD name to the path of the file declaring it. Passed in rather
    than discovered so this adapter cannot be pointed at arbitrary YAML: the caller
    (the CLI) resolves them from the repo's own `deploy/crds.yaml`, and a CRD with no
    declared manifest is a refusal rather than a silent skip.

    `node_role` reads the IAM half of the CNI credential-scope proof. It is a required
    constructor argument and not an optional one: with it absent,
    `cni_credential_scope` answers one of the three keys
    `admission._cni_scope_proof` requires, that proof is unverifiable by construction,
    and the bootstrap taint can never be cleared on any cluster.
    """

    runner: CommandRunner
    kubeconfig: Path
    controller_namespace: str
    controller_service_account: str
    controller_image: str
    node_role: NodeRoleFacts
    manifests: Mapping[str, Path] = field(default_factory=dict)
    request_timeout: str = "30s"
    imds_probe_image: str = ""
    tenant_identity_reader: object = None

    def __post_init__(self) -> None:
        import re

        if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", self.imds_probe_image):
            raise BootstrapRefused("an immutable Python IMDS probe image is required")
        # No default image, and an empty one refuses here rather than at install time.
        # `install_controller` runs after the namespace and the CRDs exist, so an absent
        # image discovered there costs a refusal with a partial installation to clean up;
        # discovered at construction it costs nothing. The marker check is the load-bearing
        # half: `controller_deployments` and `components._refuse_existing_controller`
        # identify a workspace controller by that marker, so an image without it installs
        # a Deployment that the single-reconciler gate cannot see — the workspace would
        # then pass readiness with its own controller uncounted, and a SECOND bootstrap
        # against the same cluster would not notice this one.
        if not self.controller_image.strip():
            raise BootstrapRefused(
                "no workspace controller image was supplied; there is no default, "
                "because choosing a controller version silently is the release owner's "
                "decision being made by whoever last ran a deploy"
            )
        if CONTROLLER_IMAGE_MARKER not in self.controller_image:
            raise BootstrapRefused(
                f"the controller image {self.controller_image!r} does not contain "
                f"{CONTROLLER_IMAGE_MARKER!r}, which is how this package recognises a "
                "workspace controller; a Deployment it cannot recognise is one the "
                "single-reconciler gate would not count, so a second controller could "
                "later be installed alongside it without refusal"
            )

    def bind_target(self, target, binding) -> None:
        """Verify selected kubeconfig transport against AWS before cluster I/O."""
        from .target import _binding_identity

        if _binding_identity(binding) != (target.org_id, target.workspace_id):
            raise BootstrapRefused(
                "cluster transport binding has a different workspace"
            )
        self._bound_target = target
        self._operation_binding = binding
        payload = self._verify_transport()
        import os
        from tempfile import TemporaryDirectory

        self.close()
        self._transport_directory = TemporaryDirectory(
            prefix="adp-bootstrap-transport-"
        )
        self._transport_path = Path(self._transport_directory.name) / "config"
        descriptor = os.open(
            self._transport_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle)

    def close(self) -> None:
        directory = getattr(self, "_transport_directory", None)
        if directory is not None:
            directory.cleanup()
            del self._transport_directory
            del self._transport_path

    def _verify_transport(self):
        from hmac import compare_digest
        from .target import _binding_identity

        target = self._bound_target
        _binding_identity(self._operation_binding)
        result = self.runner.run(
            (
                "kubectl",
                "--kubeconfig",
                str(self.kubeconfig),
                "config",
                "view",
                "--raw",
                "--minify",
                "-o",
                "json",
            )
        )
        payload = _parse_json(
            _require_success(result, "reading cluster transport"), "cluster transport"
        )
        clusters = payload.get("clusters", [])
        if len(clusters) != 1 or not isinstance(clusters[0], Mapping):
            raise BootstrapRefused("kubeconfig must select exactly one cluster")
        cluster = clusters[0].get("cluster", {})
        if (
            cluster.get("server") != target.endpoint
            or cluster.get("insecure-skip-tls-verify")
            or cluster.get("tls-server-name")
            or not compare_digest(
                str(cluster.get("certificate-authority-data", "")),
                target.certificate_authority_data,
            )
        ):
            raise BootstrapRefused(
                "kubeconfig endpoint or CA does not match the verified target"
            )

        return payload

    def _kube(self, *args: str, data: str | None = None) -> CommandResult:
        if hasattr(self, "_bound_target"):
            from .target import _binding_identity

            _binding_identity(self._operation_binding)
            if not hasattr(self, "_transport_path"):
                raise BootstrapRefused("bound cluster transport is closed")
        return self.runner.run(
            (
                "kubectl",
                "--kubeconfig",
                str(getattr(self, "_transport_path", self.kubeconfig)),
                f"--request-timeout={self.request_timeout}",
                *args,
            ),
            data=data,
        )

    def bootstrap_permission(self, *, verb, resource, namespace=None, name=None):
        resource, _, group = resource.partition(".")
        attributes = {"verb": verb, "resource": resource, "group": group}
        if namespace is not None:
            attributes["namespace"] = namespace
        if name is not None:
            attributes["name"] = name
        body = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectAccessReview",
            "spec": {"resourceAttributes": attributes},
        }
        result = self._kube("create", "-f", "-", "-o", "json", data=json.dumps(body))
        try:
            status = json.loads(result.stdout)["status"]
            answer = status["allowed"]
            if (
                result.returncode
                or type(answer) is not bool
                or status.get("evaluationError")
            ):
                raise ValueError("unanswered")
        except (ValueError, KeyError, TypeError) as exc:
            raise BootstrapRefused(
                "bootstrap permission review was not answered"
            ) from exc
        return answer

    # --- reads -------------------------------------------------------------

    def custom_resource_definitions(self) -> Sequence[str]:
        result = _require_success(
            self._kube("get", "crd", "-o", "json"),
            "listing custom resource definitions",
        )
        payload = _parse_json(result, "kubectl get crd")
        items = payload.get("items", []) if isinstance(payload, Mapping) else []
        return tuple(
            str(item.get("metadata", {}).get("name", ""))
            for item in items
            if isinstance(item, Mapping)
        )

    def controller_deployments(self) -> Sequence[str]:
        """Images of every WORKSPACE-CONTROLLER-like Deployment, across all namespaces.

        `--all-namespaces` is deliberate: a second controller reconciling this workspace
        from elsewhere is the failure the single-controller gate exists to catch, and it
        would be invisible to a namespaced read. Same read `Installer.workspace()`
        performs.

        The `CONTROLLER_IMAGE_MARKER` filter is equally deliberate, and it is the half an
        earlier revision of this adapter got wrong. `readiness._controller_checks`
        requires EXACTLY ONE image back from this method, so returning every Deployment
        on the cluster made readiness fail on every real cluster in existence: CoreDNS,
        the EBS CSI controller and the load-balancer controller are all Deployments, so
        the count was never one and the taint could never come off. Meanwhile
        `components._refuse_existing_controller` applies this same marker to this same
        method's result, so the unfiltered read was also answering a DIFFERENT question
        than the one gate already asking it — two callers, two meanings, one method.

        Filtering here rather than in `readiness.py` keeps the decision out of the
        adapter layer in the sense that matters: the marker is imported from
        `components.py` rather than restated, so "which Deployments are workspace
        controllers" has exactly one definition in the package.
        """
        result = _require_success(
            self._kube("get", "deployments", "--all-namespaces", "-o", "json"),
            "listing deployments across all namespaces",
        )
        payload = _parse_json(result, "kubectl get deployments")
        items = payload.get("items", []) if isinstance(payload, Mapping) else []
        images: list[str] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            containers = (
                item.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("containers", [])
            )
            for container in containers:
                if not isinstance(container, Mapping):
                    continue
                image = str(container.get("image", ""))
                if CONTROLLER_IMAGE_MARKER in image:
                    images.append(image)
        return tuple(images)

    def namespace(self, name: str) -> ObservedNamespace | None:
        result = self._kube("get", "namespace", name, "-o", "json")
        if result.returncode != 0:
            # kubectl exits non-zero both for "absent" and for "denied". They are
            # different answers and only the first may be treated as None, so the
            # stderr is checked for the API server's own NotFound wording rather than
            # assuming absence from the exit status alone.
            if "notfound" in result.stderr.lower().replace(" ", ""):
                return None
            raise BootstrapRefused(
                f"reading namespace {name!r} failed with exit {result.returncode}; "
                "refusing to treat an unreadable namespace as absent, because creating "
                "one that already exists would adopt it without comparing it"
            )
        payload = _parse_json(result, f"kubectl get namespace {name}")
        if not isinstance(payload, Mapping):
            raise BootstrapRefused(f"kubectl returned no object for namespace {name!r}")
        metadata = payload.get("metadata", {})
        return ObservedNamespace(
            name=str(metadata.get("name", "")),
            uid=str(metadata.get("uid", "")),
            labels={str(k): str(v) for k, v in (metadata.get("labels") or {}).items()},
        )

    def workload(self, namespace: str, name: str) -> ObservedWorkload | None:
        result = self._kube("get", "deployment", name, "-n", namespace, "-o", "json")
        if result.returncode != 0:
            if "notfound" in result.stderr.lower().replace(" ", ""):
                return None
            raise BootstrapRefused(
                f"reading deployment {namespace}/{name} failed with exit "
                f"{result.returncode}; an unreadable workload is not an available one, "
                "and it is not a confidently absent one either"
            )
        payload = _parse_json(result, f"kubectl get deployment {name}")
        if not isinstance(payload, Mapping):
            raise BootstrapRefused(
                f"kubectl returned no object for deployment {namespace}/{name}"
            )
        spec = payload.get("spec", {})
        status = payload.get("status", {})
        return ObservedWorkload(
            name=name,
            namespace=namespace,
            desired_replicas=int(spec.get("replicas", 0) or 0),
            available_replicas=int(status.get("availableReplicas", 0) or 0),
        )

    def node_taints(self) -> Sequence[Mapping[str, str]]:
        result = _require_success(
            self._kube("get", "nodes", "-o", "json"), "listing node taints"
        )
        payload = _parse_json(result, "kubectl get nodes")
        items = payload.get("items", []) if isinstance(payload, Mapping) else []
        taints: list[Mapping[str, str]] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            for taint in item.get("spec", {}).get("taints", []) or []:
                if isinstance(taint, Mapping):
                    taints.append({str(k): str(v) for k, v in taint.items()})
        return tuple(taints)

    # --- mutations ---------------------------------------------------------

    def create_namespace(
        self, name: str, labels: Mapping[str, str]
    ) -> ObservedNamespace:
        """Create the namespace with exactly these labels, returning it as observed.

        The manifest goes in on STDIN rather than as a file path or an argv value, the
        same way `cluster_probe.py::create` does it: nothing is written to disk and
        nothing appears in a process listing.

        The created object is re-read from the API server's own response rather than
        assumed, because the uid it assigns is what every later ownership decision
        depends on (F3).
        """
        manifest = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": name, "labels": dict(labels)},
            }
        )
        result = _require_success(
            self._kube("create", "-f", "-", "-o", "json", data=manifest),
            f"creating namespace {name!r}",
        )
        payload = _parse_json(result, f"kubectl create namespace {name}")
        if not isinstance(payload, Mapping):
            raise BootstrapRefused(
                f"creating namespace {name!r} returned no object; without the assigned "
                "uid there is no way to prove later that ADP created it"
            )
        metadata = payload.get("metadata", {})
        return ObservedNamespace(
            name=str(metadata.get("name", "")),
            uid=str(metadata.get("uid", "")),
            labels={str(k): str(v) for k, v in (metadata.get("labels") or {}).items()},
        )

    def establish_crds(self, names: Sequence[str]) -> Sequence[str]:
        """Apply the declared CRD manifests, then re-read what is established.

        Applies only manifests the caller declared for the requested names — a name with
        no declared manifest refuses rather than being skipped, because a skipped CRD
        would surface later as a mysteriously missing type.

        The return value is a fresh READ, not an echo of what was applied. `kubectl
        apply` succeeding is not the same fact as the CRD being established, and only
        the second one may satisfy the gate.
        """
        missing_manifests = [name for name in names if name not in self.manifests]
        if missing_manifests:
            raise BootstrapRefused(
                "no manifest was declared for required CRD(s): "
                + ", ".join(sorted(missing_manifests))
                + ". Refusing to skip one, because a missing CRD surfaces later as a "
                "type the controller cannot reconcile"
            )
        for name in names:
            _require_success(
                self._kube("apply", "-f", str(self.manifests[name])),
                f"applying the manifest for CRD {name!r}",
            )
        established = set(self.custom_resource_definitions())
        return tuple(name for name in names if name in established)

    def establish_controller_rbac(
        self, namespace: str, service_account: str
    ) -> Mapping[str, str]:
        """Create the controller's ServiceAccount and its scoped Role/ClusterRole pair.

        Five objects, because the required permission set spans both scopes and RBAC
        cannot express it in fewer: `nodes` and the two CRD-defined types are
        cluster-scoped, while `pods` and `leases` are namespaced. Granting the whole set
        via one ClusterRole would hand the controller `watch pods` in EVERY namespace on a
        supplied cluster — a BYOC tenant's namespaces included — which is exactly the
        over-broad credential `FORBIDDEN_CONTROLLER_PERMISSIONS` exists to catch. So the
        namespaced half is bound with a RoleBinding scoped to this namespace.

        The rules are generated from `REQUIRED_CONTROLLER_PERMISSIONS`, so this method
        cannot grant something the readiness gate does not verify, and the caller cannot
        pass its own rules — `access.ClusterAccess` deliberately gives this seam no
        parameter for them, because a caller that could would be able to grant
        cluster-admin and still satisfy the later check.

        `apply` rather than `create`, so a retry over objects a previous attempt made
        succeeds instead of failing on AlreadyExists. That idempotence is required by the
        Protocol and it is what lets a bootstrap be re-run after a partial failure.

        Returns the created object names keyed by kind so `_install_controller` can record
        exactly what it made and cleanup can remove exactly that.
        """
        role_name = f"{service_account}-workspace"
        cluster_role_name = f"{service_account}-{namespace}-cluster"
        namespaced_rules = _rbac_rules(
            [
                resource
                for _, resource in REQUIRED_CONTROLLER_PERMISSIONS
                if resource not in _CLUSTER_SCOPED_RESOURCES
            ]
        )
        cluster_rules = _rbac_rules(sorted(_CLUSTER_SCOPED_RESOURCES))
        subject = {
            "kind": "ServiceAccount",
            "name": service_account,
            "namespace": namespace,
        }
        objects: list[Mapping[str, object]] = [
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": service_account, "namespace": namespace},
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": role_name, "namespace": namespace},
                "rules": namespaced_rules,
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": role_name, "namespace": namespace},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": role_name,
                },
                "subjects": [subject],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": cluster_role_name},
                "rules": cluster_rules,
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": cluster_role_name},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": cluster_role_name,
                },
                "subjects": [subject],
            },
        ]

        manifest = json.dumps({"apiVersion": "v1", "kind": "List", "items": objects})
        _require_success(
            self._kube("apply", "-f", "-", data=manifest),
            f"establishing the workspace controller's RBAC in {namespace!r}",
        )
        return {
            "ServiceAccount": service_account,
            "Role": role_name,
            "RoleBinding": role_name,
            "ClusterRole": cluster_role_name,
            "ClusterRoleBinding": cluster_role_name,
        }

    def install_controller(
        self, namespace: str, name: str, service_account: str
    ) -> ObservedWorkload:
        """Install the workspace controller Deployment, then read it back.

        The image comes from `self.controller_image` — the adapter carries it, so a
        caller cannot choose a controller version per call. Choosing one is the release
        owner's decision, and `ClusterAccess.install_controller` gives it no parameter.

        The returned workload is a fresh READ, not an echo of the apply. `kubectl apply`
        succeeding says the object was accepted, not that a pod is running: a Deployment
        with `availableReplicas: 0` is the exact state F2 was about, and only the read can
        report it. Readiness is then a separate gate's question, so nothing here waits or
        retries — a not-yet-available controller is a fact to return, not an error.

        The controller must run before readiness can clear the bootstrap interlock.
        Tolerate only that exact taint; tenant Pods do not receive this toleration.
        """
        manifest = json.dumps(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": {"app": name},
                },
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": name}},
                    "template": {
                        "metadata": {"labels": {"app": name}},
                        "spec": {
                            "serviceAccountName": service_account,
                            "tolerations": [
                                {
                                    "key": _BOOTSTRAP_TAINT_KEY,
                                    "operator": "Equal",
                                    "value": "pending",
                                    "effect": "NoSchedule",
                                }
                            ],
                            # Matches the `restricted` Pod Security Standard the tenant
                            # namespace enforces. The controller's own namespace is not
                            # tenant-labelled, but a controller that could not run under
                            # `restricted` would be one this platform cannot honestly
                            # claim to run under it either.
                            "securityContext": {
                                "runAsNonRoot": True,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [
                                {
                                    "name": name,
                                    "image": self.controller_image,
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                }
                            ],
                        },
                    },
                },
            }
        )
        _require_success(
            self._kube("apply", "-f", "-", data=manifest),
            f"installing the workspace controller {namespace}/{name}",
        )
        observed = self.workload(namespace, name)
        if observed is None:
            raise BootstrapRefused(
                f"the workspace controller {namespace}/{name} could not be read back "
                "after a successful apply; an object that cannot be observed cannot be "
                "reported as available, and the readiness gate would have nothing to "
                "check"
            )
        return observed

    def place_system_workloads(
        self, namespace: str, names: Sequence[str]
    ) -> Mapping[str, bool]:
        """Let the named SYSTEM workloads tolerate the bootstrap taint, and nothing else.

        This is the F2 mechanism for CoreDNS, which the live handoff records as
        unschedulable behind the bootstrap taint. The patch adds a toleration to the
        named Deployment in the SYSTEM namespace only, one workload at a time. There is
        no code path here that can patch a tenant workload or edit a shared default, so
        the tenant interlock cannot be relaxed through this method — and
        `readiness.py` re-verifies a tenant pod is still unschedulable regardless.

        Placement is then confirmed by re-reading availability rather than by trusting
        the patch's exit status.
        """
        placed: dict[str, bool] = {}
        for name in names:
            patch = json.dumps(
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "tolerations": [
                                    {
                                        "key": _BOOTSTRAP_TAINT_KEY,
                                        "operator": "Exists",
                                        "effect": "NoSchedule",
                                    }
                                ]
                            }
                        }
                    }
                }
            )
            result = self._kube(
                "patch",
                "deployment",
                name,
                "-n",
                namespace,
                "--type",
                "strategic",
                "-p",
                patch,
            )
            if result.returncode != 0:
                placed[name] = False
                continue
            observed = self.workload(namespace, name)
            placed[name] = observed is not None and observed.available_replicas > 0
        return placed

    def remove_bootstrap_taint(self, key: str) -> Sequence[Mapping[str, str]]:
        """Remove the taint from every node, then re-read what remains.

        The return value is a fresh read for the same reason as `establish_crds`: the
        gate's question is "are the nodes schedulable now", and a successful `kubectl
        taint` call does not answer it for a node that was added mid-run.
        """
        _require_success(
            self._kube("taint", "nodes", "--all", f"{key}-"),
            f"removing the bootstrap taint {key!r}",
        )
        return self.node_taints()

    def restore_bootstrap_taint(self, key: str) -> Sequence[Mapping[str, str]]:
        """Re-apply the bootstrap taint to every node, then re-read what is present.

        The F5 undo. `--overwrite` so a node that still carries it is not an error —
        this runs on a recovery path where a partially-completed removal is exactly the
        expected state.
        """
        _require_success(
            self._kube(
                "taint", "nodes", "--all", f"{key}=pending:NoSchedule", "--overwrite"
            ),
            f"restoring the bootstrap taint {key!r}",
        )
        payload = _parse_json(
            _require_success(
                self._kube("get", "nodes", "-o", "json"),
                "reading restored bootstrap taints",
            ),
            "verifying restored bootstrap taints",
        )
        nodes = payload.get("items") if isinstance(payload, Mapping) else None
        if (
            not isinstance(nodes, list)
            or not nodes
            or payload.get("metadata", {}).get("continue")
            or any(
                not isinstance(node, Mapping)
                or not any(
                    isinstance(taint, Mapping)
                    and taint.get("key") == key
                    and taint.get("effect") == "NoSchedule"
                    for taint in node.get("spec", {}).get("taints", []) or []
                )
                for node in nodes
            )
        ):
            raise BootstrapRefused("bootstrap interlock is not verified on every node")
        return [taint for node in nodes for taint in node["spec"]["taints"]]

    # --- probes ------------------------------------------------------------

    def dry_run_pod(self, namespace: str, spec: Mapping[str, object]) -> ObservedPod:
        """Ask the API server whether it would admit this pod. Creates nothing.

        `--dry-run=server` runs admission — including Pod Security Admission — without
        persisting the object. A client-side dry run would skip exactly the controller
        whose behaviour is being probed and would report every unsafe pod as admitted.

        `spec` is the ABSTRACT probe `admission.py` writes — `{"name": ..., "hostPID":
        True}` — and this method is what turns it into a Pod the API server will parse.
        That translation is the whole of this method and it is not cosmetic: a body with
        no `apiVersion`/`kind`, no container and a `hostPID` key at the top level is
        rejected by client-side VALIDATION before admission is ever consulted. Every
        probe then comes back `admitted=False` — including the conforming control — so
        `_unsafe_pod_proof` reports "admission also rejected a conforming pod" and the
        taint can never come off. The failure is total and it is silent: a validation
        error and a Pod Security rejection are both a non-zero exit.

        So the unsafe fields are placed where Kubernetes actually reads them —
        `hostNetwork`/`hostPID`/`hostIPC` on the pod spec, `privileged` in the
        container's securityContext, `hostPath` as a volume — and the conforming
        control carries the securityContext `restricted` requires, so that it is
        admitted on a correctly-configured cluster and the rejections above it mean
        something.
        """
        name = str(spec.get("name") or "bootstrap-probe")
        manifest = json.dumps(_probe_pod(name, namespace, spec))
        result = self._kube(
            "apply", "-f", "-", "-n", namespace, "--dry-run=server", data=manifest
        )
        if result.returncode == 0:
            return ObservedPod(name=name, admitted=True)
        return ObservedPod(
            name=name,
            admitted=False,
            # The API server's own message, trimmed to one line. Carried because a
            # negative test has to distinguish "admission rejected this" from "the
            # request failed for an unrelated reason" — opposite outcomes.
            rejected_reason=(result.stderr or "rejected").strip().splitlines()[0][:200],
        )

    def imds_reachable_from_tenant_pod(self, namespace: str) -> Mapping[str, bool]:
        """Perform IMDSv2 token and credential requests without exposing responses."""
        code = Path(__file__).with_name("imds_probe.py").read_text()
        answers = {}
        for family, host in (("ipv4", "169.254.169.254"), ("ipv6", "fd00:ec2::254")):
            name = f"imds-probe-{family}"
            pod = {
                "apiVersion": "v1",
                "kind": "Pod",
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "tolerations": [
                        {
                            "key": _BOOTSTRAP_TAINT_KEY,
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        }
                    ],
                    "containers": [
                        {
                            "name": name,
                            "image": self.imds_probe_image,
                            "command": ["python3", "-c", code, host],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                },
            }
            result = self._kube(
                "run",
                name,
                "-n",
                namespace,
                "--rm",
                "--restart=Never",
                "--attach",
                "--quiet",
                "--pod-running-timeout=30s",
                f"--image={self.imds_probe_image}",
                "--overrides",
                json.dumps(pod),
            )
            markers = [
                line.strip()
                for line in result.stdout.splitlines()
                if line.startswith("ADP_IMDS_")
            ]
            if (
                result.returncode != 0
                or len(markers) != 1
                or markers[0] not in ("ADP_IMDS_REACHABLE", "ADP_IMDS_UNREACHABLE")
            ):
                raise BootstrapRefused(
                    "IMDS isolation probe did not produce a verified result"
                )
            answers[family] = markers[0] == "ADP_IMDS_REACHABLE"
        return answers

    def can_tenant_change_admission_labels(self, namespace: str) -> bool:
        from .tenant_authorization import can_tenants_patch_namespace

        return can_tenants_patch_namespace(self, namespace)

    def cni_credential_scope(self) -> Mapping[str, object]:
        """How the CNI obtains credentials, and what the node role can do.

        Every required key is populated from a read. A key this method cannot answer is
        deliberately OMITTED rather than defaulted, because `admission.py` treats a
        missing key as an unanswered question and refuses — which is the safe direction
        — while a defaulted `False` would read as a verified negative.
        """
        scope: dict[str, object] = {}

        account = self._kube(
            "get",
            "serviceaccount",
            "aws-node",
            "-n",
            "kube-system",
            "-o",
            "json",
        )
        if account.returncode == 0:
            payload = _parse_json(account, "kubectl get serviceaccount aws-node")
            annotations = (
                payload.get("metadata", {}).get("annotations", {})
                if isinstance(payload, Mapping)
                else {}
            )
            scope["aws_node_role_arn"] = str(
                (annotations or {}).get("eks.amazonaws.com/role-arn", "")
            )

        # The node role's permissions are an IAM fact, not a cluster fact, so they are
        # read through a separate adapter — but they are merged in HERE, not left to a
        # caller. The previous revision's comment said the CLI merged them and nothing
        # did, so `cni_credential_scope` returned one key of the three
        # `admission._cni_scope_proof` requires and `cni_credentials_scoped` was
        # permanently unprovable: "no observation for node_role_has_cni_permissions,
        # node_role_has_account_wide_ecr" on every cluster, and with it the taint stayed
        # on forever. A seam whose contract is satisfied nowhere is the F1 defect again.
        scope.update(self.node_role.cni_credential_facts())
        return scope

    def tenant_scheduling_denied(self, namespace: str) -> bool | None:
        """Whether a TENANT pod is still unschedulable. None when unanswerable.

        A dry-run create cannot answer this: admission accepts a pod the scheduler will
        never place. So this reads the live taints and asks whether the bootstrap taint
        is still on every node — which is the fact that keeps an untolerated tenant pod
        pending.

        Returns None when there are no nodes to reason about, because "no nodes" is not
        the same answer as "tenant work is safely blocked" and `readiness.py` must
        refuse rather than pass on it.
        """
        result = self._kube("get", "nodes", "-o", "json")
        if result.returncode != 0:
            return None
        payload = _parse_json(result, "kubectl get nodes")
        items = payload.get("items", []) if isinstance(payload, Mapping) else []
        nodes = [item for item in items if isinstance(item, Mapping)]
        if not nodes:
            return None
        for node in nodes:
            taints = node.get("spec", {}).get("taints", []) or []
            if not any(
                isinstance(taint, Mapping) and taint.get("key") == _BOOTSTRAP_TAINT_KEY
                for taint in taints
            ):
                # One schedulable node is enough for a tenant pod to land on.
                return False
        return True

    def controller_handover(self, namespace: str) -> Mapping[str, object]:
        """Whether a previous controller completed its handover, from the lease.

        The coordination lease is the authority, not the Deployment: a controller whose
        Deployment is gone but whose lease is still held has not handed over and may
        still be reconciling. A held lease whose holder is this workspace's own
        controller is complete; any other holder is not.

        An unreadable lease returns no `complete` key at all, which `readiness.py`
        refuses on.
        """
        result = self._kube(
            "get",
            "lease",
            "superplane-controller",
            "-n",
            namespace,
            "-o",
            "json",
        )
        if result.returncode != 0:
            if "notfound" in result.stderr.lower().replace(" ", ""):
                # No lease at all: nothing holds the workspace, so there is no
                # incomplete handover to block on.
                return {"complete": True}
            return {}
        payload = _parse_json(result, "kubectl get lease superplane-controller")
        if not isinstance(payload, Mapping):
            return {}
        holder = str(payload.get("spec", {}).get("holderIdentity", ""))
        return {
            "complete": holder.startswith(self.controller_service_account)
            or not holder.strip(),
            "holder": holder,
        }

    def controller_permissions(self, namespace: str) -> Mapping[tuple[str, str], bool]:
        """Ask RBAC about the service account installed in the workspace namespace."""
        from .tenant_authorization import review

        subject = f"system:serviceaccount:{namespace}:{self.controller_service_account}"
        groups = (
            "system:authenticated",
            "system:serviceaccounts",
            f"system:serviceaccounts:{namespace}",
        )
        answers = {}
        for verb, resource in (
            *REQUIRED_CONTROLLER_PERMISSIONS,
            *FORBIDDEN_CONTROLLER_PERMISSIONS,
        ):
            try:
                answers[(verb, resource)] = review(
                    self,
                    user=subject,
                    groups=groups,
                    verb=verb,
                    resource=resource,
                    namespace=None
                    if resource in _CLUSTER_SCOPED_RESOURCES
                    or resource
                    in {
                        "namespaces",
                        "clusterrolebindings.rbac.authorization.k8s.io",
                    }
                    else namespace,
                )
            except BootstrapRefused:
                continue  # Missing is unanswered, not a denial.
        return answers


@dataclass(frozen=True)
class AwsPrerequisiteAccess:
    """`PrerequisiteAccess` against real AWS, via the `aws` CLI.

    Every method is a READ. There is no create here at all: the access entry and the
    security-group rules are created by whatever holds the credential for them
    (#5534's provisioning provider, or Terraform), and this adapter's job is the
    authoritative observation the F4 gate validates attribution against.

    That split is why the gate is trustworthy. If this adapter could create a rule, a
    missing rule would be silently fixed and the gate would pass on a cluster whose
    access path nobody reviewed.
    """

    runner: CommandRunner
    region: str

    def _aws(self, *args: str) -> CommandResult:
        return self.runner.run(
            ("aws", "--region", self.region, "--no-cli-pager", *args)
        )

    def tenant_principals(self, cluster_arn: str, bootstrap_principal: str):
        from .tenant_authorization import eks_tenant_principals

        def read(*args):
            result = self._aws(*args, "--output", "json")
            return _parse_json(
                _require_success(result, "reading EKS tenant identity inventory"),
                "EKS tenant identity inventory",
            )

        return eks_tenant_principals(
            read, cluster_arn.rsplit("/", 1)[-1], bootstrap_principal
        )

    def access_entry(
        self, cluster_arn: str, principal_arn: str
    ) -> Mapping[str, object]:
        """The EKS access entry for this principal, with its scope and policy."""
        cluster_name = cluster_arn.rsplit("/", 1)[-1]
        described = self._aws(
            "eks",
            "describe-access-entry",
            "--cluster-name",
            cluster_name,
            "--principal-arn",
            principal_arn,
            "--output",
            "json",
        )
        if described.returncode != 0:
            if "resourcenotfound" in described.stderr.lower().replace(" ", ""):
                return {"exists": False}
            # Not an absence. Returning `exists: False` here would let a permissions
            # failure read as "the entry is missing", and the gate's refusal would send
            # an operator to create something that already exists.
            raise BootstrapRefused(
                "reading the EKS access entry failed with exit "
                f"{described.returncode}; refusing to treat an unreadable entry as an "
                "absent one"
            )

        from .prerequisites import access_entry_identity

        payload = _parse_json(described, "aws eks describe-access-entry")
        entry = payload.get("accessEntry") if isinstance(payload, Mapping) else None
        if (
            not isinstance(entry, Mapping)
            or entry.get("clusterName") != cluster_name
            or entry.get("type") != "STANDARD"
        ):
            raise BootstrapRefused(
                "EKS access entry identity is missing or differs from the bound target/principal"
            )
        identity = {
            "cluster_arn": cluster_arn,
            "principal_arn": entry.get("principalArn"),
            "access_entry_arn": entry.get("accessEntryArn"),
        }
        access_entry_identity(identity, cluster_arn, principal_arn)

        policies = self._aws(
            "eks",
            "list-associated-access-policies",
            "--cluster-name",
            cluster_name,
            "--principal-arn",
            principal_arn,
            "--output",
            "json",
        )
        _require_success(policies, "listing the access entry's associated policies")
        payload = _parse_json(policies, "aws eks list-associated-access-policies")
        associated = (
            payload.get("associatedAccessPolicies", [])
            if isinstance(payload, Mapping)
            else []
        )
        if len(associated) != 1:
            raise BootstrapRefused(
                f"the EKS access entry has {len(associated)} associated access "
                "policies, not exactly one; an entry with several policies has an "
                "authority that cannot be stated in one scope"
            )
        policy = associated[0] if isinstance(associated[0], Mapping) else {}
        scope = policy.get("accessScope", {}) or {}
        return {
            **identity,
            "exists": True,
            "scope": str(scope.get("type", "")).lower(),
            "namespaces": tuple(str(n) for n in (scope.get("namespaces") or ())),
            "policy": str(policy.get("policyArn", "")),
            # Whether ADP created it is not an AWS-observable fact, so it is
            # deliberately absent: `prerequisites._ownership` then records the
            # prerequisite as ADOPTED, which is the safe direction — adopted objects
            # are never removed.
        }

    def security_group_rule(
        self, group_id: str, source: str, port: int, protocol: str
    ) -> Mapping[str, object]:
        """The rule matching this exact source, target, port and protocol."""
        result = self._aws(
            "ec2",
            "describe-security-group-rules",
            "--filters",
            f"Name=group-id,Values={group_id}",
            "--output",
            "json",
        )
        _require_success(result, f"describing security group rules for {group_id!r}")
        payload = _parse_json(result, "aws ec2 describe-security-group-rules")
        rules = (
            payload.get("SecurityGroupRules", [])
            if isinstance(payload, Mapping)
            else []
        )

        for rule in rules:
            if not isinstance(rule, Mapping):
                continue
            if rule.get("IsEgress"):
                continue
            if str(rule.get("IpProtocol", "")) != protocol:
                continue
            if int(rule.get("FromPort", -1)) != port:
                continue
            if int(rule.get("ToPort", -1)) != port:
                continue
            referenced = rule.get("ReferencedGroupInfo") or {}
            observed_source = str(
                referenced.get("GroupId") or rule.get("CidrIpv4") or ""
            )
            if observed_source != source:
                continue
            return {
                "exists": True,
                "rule_id": str(rule.get("SecurityGroupRuleId", "")),
                "tags": {
                    str(t.get("Key")): str(t.get("Value")) for t in rule.get("Tags", [])
                },
                "group_id": str(rule.get("GroupId", "")),
                "source": observed_source,
                "vpc_id": self._group_vpc(group_id),
                "account_id": str(
                    rule.get("OwnerId") or rule.get("GroupOwnerId") or ""
                ),
                "port": port,
                "protocol": protocol,
            }
        return {"exists": False}

    def _group_vpc(self, group_id: str) -> str:
        """The VPC a security group belongs to, read rather than inferred."""
        result = self._aws(
            "ec2",
            "describe-security-groups",
            "--group-ids",
            group_id,
            "--query",
            "SecurityGroups[0].VpcId",
            "--output",
            "text",
        )
        _require_success(result, f"reading the VPC of security group {group_id!r}")
        return result.stdout.strip()


@dataclass(frozen=True)
class AwsObserver:
    """Reads the two identities gate 1 compares its expectations against.

    Separate from `AwsPrerequisiteAccess` because these reads answer a different
    question. The prerequisite adapter observes objects the gate validates attribution
    for; this one observes WHO THE CALLER IS and WHAT THE CLUSTER IS, which is what
    `target.verify_target` compares against the Terraform outputs.

    Both are reads, and neither is cached. `ProviderIdentity`'s docstring records why:
    `../infra/workspaces/README.md` requires apply to re-resolve STS immediately before
    Terraform, because a credential can change between phases and the stale answer is
    the dangerous one. So the CLI calls these at the moment of use, not at startup.
    """

    runner: CommandRunner
    region: str

    def _aws(self, *args: str) -> CommandResult:
        return self.runner.run(
            ("aws", "--region", self.region, "--no-cli-pager", *args)
        )

    def provider_identity(self) -> ProviderIdentity:
        """Who the provider says the caller currently is.

        `ProviderIdentity.__post_init__` requires both fields to be non-blank, so an
        `aws sts get-caller-identity` that somehow returned an empty arn refuses at
        construction rather than flowing into a gate that would compare it against
        an expectation and find no difference worth reporting.
        """
        result = self._aws("sts", "get-caller-identity", "--output", "json")
        _require_success(result, "resolving the caller identity")
        payload = _parse_json(result, "aws sts get-caller-identity")
        if not isinstance(payload, Mapping):
            raise BootstrapRefused(
                "aws sts get-caller-identity did not return an object"
            )
        return ProviderIdentity(
            account_id=str(payload.get("Account", "")),
            principal_arn=str(payload.get("Arn", "")),
        )

    def cluster_identity(self, cluster_name: str) -> ClusterIdentity:
        """What the provider says about the cluster, carried verbatim.

        `status` is passed through as EKS reports it rather than reduced to a bool, so
        `verify_target`'s refusal can name what the cluster actually was. A cluster that
        is CREATING is a "not yet", and the distinction between that and FAILED is the
        difference between re-running in a minute and escalating.
        """
        result = self._aws(
            "eks", "describe-cluster", "--name", cluster_name, "--output", "json"
        )
        _require_success(result, f"describing cluster {cluster_name!r}")
        payload = _parse_json(result, "aws eks describe-cluster")
        cluster = payload.get("cluster", {}) if isinstance(payload, Mapping) else {}
        if not isinstance(cluster, Mapping):
            raise BootstrapRefused(
                "aws eks describe-cluster returned no cluster object"
            )
        arn = str(cluster.get("arn", ""))
        certificate = cluster.get("certificateAuthority") or {}
        identity = cluster.get("identity") or {}
        oidc = identity.get("oidc") or {} if isinstance(identity, Mapping) else {}
        return ClusterIdentity(
            name=str(cluster.get("name", "")),
            arn=arn,
            region=self.region,
            # The account is taken from the ARN rather than from the caller's STS
            # answer. Reading it from `get-caller-identity` would compare the caller's
            # account against itself and always agree, which is exactly the
            # request-against-itself comparison `target.py` refuses to rely on.
            account_id=_account_from_arn(arn),
            endpoint=str(cluster.get("endpoint", "")),
            certificate_authority_data=str(
                certificate.get("data", "") if isinstance(certificate, Mapping) else ""
            ),
            status=str(cluster.get("status", "")),
            oidc_issuer_url=str(oidc.get("issuer", ""))
            if isinstance(oidc, Mapping)
            else "",
            version=str(cluster.get("version", "")),
        )


def _account_from_arn(arn: str) -> str:
    """The account id field of an EKS cluster ARN.

    `arn:aws:eks:<region>:<account>:cluster/<name>`. Refuses a shape it cannot parse
    instead of returning an empty string: an empty account would be compared against
    the expected one, differ, and be refused — but with a message about a mismatch
    rather than about an ARN nobody could read, which sends the reader to the wrong
    problem.
    """
    parts = arn.split(":")
    if len(parts) < 6 or parts[0] != "arn" or not parts[4].strip():
        raise BootstrapRefused(
            "the cluster ARN reported by EKS is not in the expected "
            "arn:aws:eks:<region>:<account>:cluster/<name> form, so the account it "
            "names cannot be read; refusing rather than comparing a blank account"
        )
    return parts[4]
