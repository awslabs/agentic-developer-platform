"""Offline rendering of the object set a request would apply — Issue #5530 (w6-07).

## Why rendering exists at all

The legacy flow had no reviewable intermediate. `04-provision-account.sh` built a manifest
with a heredoc and piped it straight into `kubectl apply -f -` in the same breath:

    echo "$MANIFEST" | kubectl apply -f -

so the only way to find out what it would create was to let it create it. There was no
plan step, and the "plan" and "apply" boundaries the issue asks to make explicit did not
exist as separate acts.

`render` produces that intermediate: given a validated request, it returns the exact
objects that would be applied, as data, with no credentials and no cluster contact. A
reviewer reads the output before anything is mutated, and CI renders every mode on a runner
with no AWS identity.

## The prerequisite plan is separate on purpose

`02-enable-eks-capabilities.sh` installed cluster-wide controllers (kro, four ACK
controllers) and created IAM roles with `AWSOrganizationsFullAccess`, `AmazonEC2FullAccess`
and `IAMFullAccess` as *step 2 of provisioning one account*. That conflation is the
"implicit whole-platform deployment" the issue names: asking for one workspace installed
cluster-scoped controllers on a shared cluster and minted broadly-privileged roles, as a
side effect.

So `render` returns two things, and they are different kinds of thing:

* `objects` — the namespaced objects for THIS workspace. Applying them provisions one
  workspace and nothing else.
* `prerequisites` — the shared cluster-controller installs, as an explicit list of reviewed
  operations with digest-pinned references, for an operator to run deliberately ONCE per
  management cluster. `render` performs none of them, and rendering a workspace never
  emits them into `objects`.

## What is deliberately absent from the output

No secret value, ever: the rendered set names no credential and carries no `Secret` with
data. Cross-account access uses the `IAMRoleSelector` the vendored graph already declares,
which names a role ARN — an identity reference, not a credential.

No object outside the workspace's own namespace, and never a core ADP namespace — the same
boundary ../scripts/check_rendered_manifests.py enforces for the rollout lane.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import dependencies
from .modes import (
    LEGACY_FORBIDDEN_VALUES,
    AccountFactoryRequest,
    OwnershipMode,
    ValidationAuthorization,
    ensure_valid,
)

__all__ = [
    "MANAGED_BY",
    "PrerequisiteOperation",
    "RenderError",
    "RenderResult",
    "RenderStage",
    "render",
]

# The managed-by value every rendered object carries, and the value `cleanup.py` requires
# before it will delete a resource it discovered on the cluster. One constant so the writer
# and the reader of that evidence cannot disagree about the string.
MANAGED_BY = "adp-superplane-account-factory"

# An AWS account id. A local copy rather than an import, matching how `modes.py` and
# `cleanup.py` each hold their own: the shape is a fact about AWS, and a module that checks it
# should not be unable to do so because another module's private name moved.
_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")

# A workspace's object set must not reach ADP's core namespaces. Same reasoning as
# ../scripts/check_rendered_manifests.py: this is a second, independent tripwire behind the
# workspace-id check in `modes.py`.
_CORE_NAMESPACES = frozenset(
    {
        "adp",
        "adp-gateway",
        "adp-agent-factory",
        "adp-context",
        "adp-system",
        "bedrockgw",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "default",
        "arc-systems",
        "arc-runners",
    }
)

# Shapes that would be a secret literal in rendered output. Checked against the serialized
# object set, so a value smuggled through any field is caught rather than only the fields
# this module thought to look at.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "an AWS secret access key assignment",
        re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*\S+"),
    ),
    ("a session token assignment", re.compile(r"(?i)aws_session_token\s*[:=]\s*\S+")),
    ("a PEM private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "a password or token assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{8,}"
        ),
    ),
)


class RenderError(Exception):
    """Rendering was refused. Nothing partial is returned — the caller gets no object set."""


@dataclass(frozen=True)
class PrerequisiteOperation:
    """One shared cluster-controller install, stated for an operator to run deliberately.

    This is a description, not an action: nothing in this module executes it. It carries a
    digest-pinned reference so the operation a reviewer approves is the operation that runs.
    """

    name: str
    reason: str
    command: tuple[str, ...]
    scope: str = "management-cluster (shared, once per cluster)"


@dataclass(frozen=True)
class RenderStage:
    """One ordered step of a plan, with what must be true before it is applied.

    Stages exist because a dependency between two SEPARATE root objects cannot be expressed
    inside either of them. Within one graph, kro orders resources by their references; across
    two graphs it cannot, so `new-account-managed` — which creates the account as one root
    object and the infrastructure as another — has a real ordering requirement that no
    manifest states.

    Making it a stage with an explicit `precondition` is what turns that from a hidden
    assumption into a reviewable, resumable boundary: an operator applies stage 1, waits for
    the stated condition, then applies stage 2. Re-applying a stage whose condition already
    holds is how a resumed run continues rather than starting over.
    """

    number: int
    name: str
    objects: tuple[dict, ...]
    precondition: str | None = None


@dataclass(frozen=True)
class RenderResult:
    """The outcome of rendering one request.

    `objects` provisions this workspace, and `stages` is the same objects in the order they
    may be applied. `prerequisites` are the shared installs that must already be in place;
    they are NOT part of `objects` and are never applied by rendering.
    `unchecked_authorization` carries forward what validation could not verify, so a report
    can distinguish "verified" from "not checked" rather than implying the former.
    """

    request: AccountFactoryRequest
    objects: tuple[dict, ...]
    prerequisites: tuple[PrerequisiteOperation, ...]
    resource_graph_files: tuple[str, ...]
    unchecked_authorization: tuple[str, ...]
    stages: tuple[RenderStage, ...] = ()

    @property
    def namespace(self) -> str:
        """The single namespace this workspace's objects occupy."""
        return self.request.workspace_id


def _prerequisites(
    deps: dependencies.PinnedDependencies, request: AccountFactoryRequest
) -> tuple[PrerequisiteOperation, ...]:
    """The shared installs, as reviewed operations rather than implicit side effects.

    Which controllers are needed depends on the mode, and stating that dependency is part
    of the point: `bring-existing-cluster` creates no VPC, no cluster and no account, so it
    needs neither the Organizations controller nor the EC2/EKS ones. The legacy flow
    installed all of them regardless of what was being asked for.
    """
    operations: list[PrerequisiteOperation] = []

    kro = deps.chart("kro")
    operations.append(
        PrerequisiteOperation(
            name="install kro",
            reason=(
                "kro reconciles the ResourceGraphDefinitions this module renders against. "
                "Self-managed rather than the EKS Capability build, which was recorded "
                "upstream as not reconciling ResourceGraphDefinitions reliably"
            ),
            command=(
                "helm",
                "upgrade",
                "--install",
                "kro",
                kro.oci_reference,
                "--namespace",
                kro.namespace,
                "--create-namespace",
            ),
        )
    )

    needed = ["ack-iam"]
    if request.mode.creates_account:
        # Only the mode that creates an account needs the controller that creates accounts.
        needed.insert(0, "ack-organizations")
    if request.mode.creates_cluster:
        needed.extend(["ack-ec2", "ack-eks"])

    for name in needed:
        chart = deps.chart(name)
        operations.append(
            PrerequisiteOperation(
                name=f"install {name}",
                reason=(
                    f"required by mode {request.mode.value}: "
                    + (
                        "creates the AWS account"
                        if name == "ack-organizations"
                        else "manages the cross-account role selection"
                        if name == "ack-iam"
                        else "creates the VPC and subnets"
                        if name == "ack-ec2"
                        else "creates the cluster and node group"
                    )
                ),
                command=(
                    "helm",
                    "upgrade",
                    "--install",
                    name,
                    chart.oci_reference,
                    "--namespace",
                    chart.namespace,
                    "--create-namespace",
                    "--set",
                    f"aws.region={request.region}",
                ),
            )
        )

    operations.append(
        PrerequisiteOperation(
            name="apply the vendored ResourceGraphDefinitions",
            reason=(
                "the base graphs (NetworkStack, EKSClusterStack) that ADP's maintained "
                "graphs build on. Cluster-scoped, so applied once per management cluster by "
                "an operator — never as a side effect of provisioning one workspace"
            ),
            command=(
                "kubectl",
                "apply",
                "-f",
                "vendor/kro-account-factory/01-network-stack.yaml",
                "-f",
                "vendor/kro-account-factory/02-eks-cluster-stack.yaml",
            ),
        )
    )
    operations.append(
        PrerequisiteOperation(
            name="apply the ADP-maintained ResourceGraphDefinitions",
            reason=(
                "the graphs the rendered custom resources actually instantiate: "
                "AccountOwnership and WorkspaceInfrastructure. Separate from the vendored "
                "set because they are ADP's own code, and separate from each other because "
                "splitting account ownership from infrastructure ownership is what lets a "
                "teardown remove infrastructure without closing the AWS account"
            ),
            command=("kubectl", "apply", "-f", "manifests/"),
        )
    )
    return tuple(operations)


def _namespace_object(request: AccountFactoryRequest) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": request.workspace_id,
            "labels": {
                "app.kubernetes.io/managed-by": "adp-superplane-account-factory",
                "adp.aws.dev/workspace": request.workspace_id,
                # The mode and the cluster's ownership travel WITH the resources, not only
                # in the request that created them. `cleanup.py` reads these back, so a
                # later delete can tell whether ADP created the cluster without needing
                # the original request to still exist.
                "adp.aws.dev/ownership-mode": request.mode.value,
                "adp.aws.dev/cluster-ownership": request.cluster_ownership.value,
            },
            "annotations": {
                "adp.aws.dev/organization-id": request.organization_id,
                "adp.aws.dev/management-cluster": request.management_cluster,
                "adp.aws.dev/issue": "5530",
            },
        },
    }


def _owned_labels(request: AccountFactoryRequest) -> dict:
    """Ownership evidence carried by every object this module renders.

    `cleanup.py` requires exactly these before it will delete a resource it discovered on the
    cluster. Sharing a namespace is not ownership, so the labels are what make a discovered
    resource provably ours — see `cleanup.OwnershipEvidence`.
    """
    return {
        "app.kubernetes.io/managed-by": MANAGED_BY,
        "adp.aws.dev/workspace": request.workspace_id,
        "adp.aws.dev/ownership-mode": request.mode.value,
    }


def _infrastructure_object(request: AccountFactoryRequest) -> dict:
    """The maintained `WorkspaceInfrastructure` — the VPC and the cluster wired together.

    One object rather than a separate `NetworkStack` and `EKSClusterStack`, because the
    cluster's required `subnetIds` and `securityGroupIds` are OUTPUTS of the network. Two
    independent top-level objects cannot carry one's output into the other's input, so
    rendering them separately produced an `EKSClusterStack` missing both required fields —
    a plan that would be rejected, or would leave a VPC and IAM roles behind with no cluster.
    `manifests/adp-workspace-infrastructure.yaml` wires them internally.
    """
    return {
        "apiVersion": "kro.run/v1alpha1",
        "kind": "WorkspaceInfrastructure",
        "metadata": {
            "name": request.workspace_id,
            "namespace": request.workspace_id,
            "labels": _owned_labels(request),
        },
        "spec": {
            "workspaceName": request.workspace_id,
            "region": request.region,
            "vpcCidr": request.vpc_cidr,
            "availabilityZones": list(request.availability_zones),
            "clusterVersion": request.cluster_version,
            "nodeInstanceType": request.node_instance_type,
        },
    }


def _new_account_stages(
    request: AccountFactoryRequest, account_id: str | None
) -> list[RenderStage]:
    """new-account-managed: bind to the account the governed path opened, then build in it.

    Two stages, and two separate root objects, rather than the vendored
    `FullAccountInfrastructure` that owns both. That graph is not instantiated here because
    its single-object ownership is what made teardown unsafe in both directions: deleting it
    to remove infrastructure closes the AWS account into an irreversible 90-day suspension,
    and retaining it to protect the account retains the VPC and cluster DECLARATION, which a
    controller then reconciles back into existence. Splitting them is what lets `cleanup.py`
    delete infrastructure that stays deleted while the account is retained.

    The cost of splitting is that the ordering between the two is no longer expressed by a
    reference inside one graph, so it is stated as a stage precondition instead — explicit
    and resumable rather than assumed.

    ## Why stage 1 refuses to render without an account id (#5531, w6-08)

    Stage 1 used to render an `AccountOwnership` that declared an ACK `Account`, so APPLYING
    it was what opened the AWS account. That made the rendered manifest the thing that
    created an account, on the Organizations controller's own reconciliation schedule,
    outside the durable fence — no committed intent, no lease, no recorded
    `CreateAccountRequestId` bound to the operation, and a controller-level retry on a lost
    response that is indistinguishable from a second `CreateAccount`.

    Creation now happens in `account_provisioning.creation_runner.create_account` under one
    fenced operation, and this stage binds to its result. `account_id` is therefore required
    input, and rendering without it is REFUSED rather than defaulted or omitted:

    * Refusing is what makes the ordering checkable offline. An optional id would render an
      object that reconciles against an empty account number, which is a runtime failure in a
      cluster instead of a refusal in review.
    * The id must come from the durable record, which only exists if the governed call
      happened. So "was the account opened through the audited path?" is answered by whether
      this render is possible at all.
    """
    if not account_id:
        raise RenderError(
            "new-account-managed cannot be rendered without the id of the account this "
            "workspace owns. This mode's account is opened by the governed, fenced creation "
            "path (account_provisioning.creation_runner.create_account) under one durable "
            "operation, and rendering binds to the account it recorded. Rendering without an "
            "id is refused rather than deferred: the previous behaviour declared an ACK "
            "`Account` resource, which made applying this manifest the act that called "
            "CreateAccount — outside the fence, with no committed intent and no request id "
            "tied to the operation, so a controller retry could open a second billable "
            "account. Pass the account id from the durable creation record"
        )
    return [
        RenderStage(
            number=1,
            name="account ownership",
            objects=(
                {
                    "apiVersion": "kro.run/v1alpha1",
                    "kind": "AccountOwnership",
                    "metadata": {
                        "name": request.workspace_id,
                        "namespace": request.workspace_id,
                        "labels": _owned_labels(request),
                    },
                    "spec": {
                        "accountName": request.workspace_id,
                        # The account this binds to. An INPUT, not a status field a
                        # controller fills in — see the docstring.
                        "accountId": account_id,
                        "accountEmail": request.account_email,
                        "region": request.region,
                        # Required by the graph with no default (#5531). Now a record of
                        # where the governed call PLACED the account rather than an
                        # instruction to place it: placement happens at creation, because an
                        # account created at the organization root and moved afterwards is
                        # live outside its guardrails for the duration of the move.
                        "organizationalUnitId": request.organizational_unit_id,
                    },
                },
            ),
            precondition=(
                f"account {account_id} was opened for this workspace by the governed "
                f"creation path and is recorded durably against that operation. This stage "
                f"binds to it and creates no account: nothing here can call CreateAccount, "
                f"so applying it cannot open a duplicate. The recorded id is also the only "
                f"account `closure_request` will later accept"
            ),
        ),
        RenderStage(
            number=2,
            name="workspace infrastructure",
            objects=(_infrastructure_object(request),),
            precondition=(
                "stage 1 reports Ready with a non-empty status.accountId, and that id is "
                "recorded as the provisioned account for this workspace. The infrastructure "
                "is a separate root object, so nothing orders it after the account "
                "automatically — applying it early would create a VPC before the account "
                "exists to create it in. The recorded id is also the only account "
                "`closure_request` will later accept"
            ),
        ),
    ]


def _existing_account_stages(
    request: AccountFactoryRequest, account_id: str | None
) -> list[RenderStage]:
    """existing-account-managed: reference the adopted account, then build infrastructure.

    No `Account` and no `AccountOwnership`: an `Account` custom resource naming an existing
    account asks the Organizations controller to create one that is already there. The
    account is reached through an `IAMRoleSelector` naming a role ARN instead.

    One stage, because the role selector's ARN is built from the account id the REQUEST
    supplies rather than from another object's status — there is no output to wait for.

    `account_id` is accepted and unused: this mode's account comes from
    `request.target_account_id`, which `modes.py` requires here and FORBIDS in
    new-account-managed. Taking the parameter keeps every builder one shape (see
    `_STAGE_BUILDERS`) so a mode cannot be dispatched with the wrong arity; `render` refuses a
    creation-record id supplied against an adopting mode before reaching here.
    """
    return [
        RenderStage(
            number=1,
            name="adopted account access and workspace infrastructure",
            objects=(
                {
                    "apiVersion": "services.k8s.aws/v1alpha1",
                    "kind": "IAMRoleSelector",
                    "metadata": {
                        "name": request.workspace_id,
                        "namespace": request.workspace_id,
                        "labels": _owned_labels(request),
                    },
                    "spec": {
                        # An identity reference, not a credential. This is how ACK reaches
                        # the target account, and why no secret appears in a rendered set.
                        "arn": (
                            f"arn:aws:iam::{request.target_account_id}:role/"
                            f"OrganizationAccountAccessRole"
                        ),
                        "namespaceSelector": {"names": [request.workspace_id]},
                    },
                },
                _infrastructure_object(request),
            ),
        )
    ]


def _bring_existing_cluster_objects(request: AccountFactoryRequest) -> list[dict]:
    """bring-existing-cluster: reference what exists; create no AWS infrastructure.

    This mode emits NO NetworkStack, NO EKSClusterStack and NO Account — which is the whole
    reason it is a distinct mode rather than a flag. The legacy flow had no way to express
    "use this cluster", so the only way to attach a workspace to an existing cluster was to
    let `FullAccountInfrastructure` try to create one and fail partway.

    The ConfigMap records WHICH cluster was adopted and that ADP did not create it, so a
    later cleanup can establish the delete boundary from cluster state.
    """
    return [
        {
            "apiVersion": "services.k8s.aws/v1alpha1",
            "kind": "IAMRoleSelector",
            "metadata": {
                "name": request.workspace_id,
                "namespace": request.workspace_id,
                "labels": _owned_labels(request),
            },
            "spec": {
                "arn": (
                    f"arn:aws:iam::{request.target_account_id}:role/"
                    f"OrganizationAccountAccessRole"
                ),
                "namespaceSelector": {"names": [request.workspace_id]},
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{request.workspace_id}-adopted-cluster",
                "namespace": request.workspace_id,
                "labels": {
                    **_owned_labels(request),
                    "adp.aws.dev/cluster-ownership": request.cluster_ownership.value,
                },
            },
            # Identities and ownership facts only. A ConfigMap is world-readable within its
            # namespace, so nothing here may be sensitive.
            "data": {
                "clusterName": request.existing_cluster_name or "",
                "accountId": request.target_account_id or "",
                "region": request.region,
                "clusterOwnership": request.cluster_ownership.value,
                "adpCreatedCluster": "false",
                "cleanupBoundary": (
                    "ADP did not create this cluster and must not delete it. Removing this "
                    "workspace removes the workspace's own namespaced resources only."
                ),
            },
        },
    ]


def _bring_existing_cluster_stages(
    request: AccountFactoryRequest, account_id: str | None
) -> list[RenderStage]:
    """bring-existing-cluster: reference what exists; create no AWS infrastructure.

    `account_id` is accepted and unused, for the reason `_existing_account_stages` gives.
    """
    return [
        RenderStage(
            number=1,
            name="adopted cluster access and adoption record",
            objects=tuple(_bring_existing_cluster_objects(request)),
        )
    ]


_STAGE_BUILDERS = {
    OwnershipMode.NEW_ACCOUNT_MANAGED: _new_account_stages,
    OwnershipMode.EXISTING_ACCOUNT_MANAGED: _existing_account_stages,
    OwnershipMode.BRING_EXISTING_CLUSTER: _bring_existing_cluster_stages,
}


def _check_graph_inputs(
    objects: list[dict], deps: dependencies.PinnedDependencies
) -> None:
    """Refuse a custom resource that does not satisfy the graph it instantiates.

    This is the general form of the defect that made existing-account-managed unusable: an
    `EKSClusterStack` rendered without `subnetIds` or `securityGroupIds`, both of which its
    graph declares as required inputs with no default. The old check only asked whether SOME
    graph declared the rendered KIND, which that object passed — the kind existed, the object
    was still unreconcilable.

    So the check compares against each graph's own declared schema, in both directions:

    * a missing REQUIRED input is refused — the object cannot reconcile;
    * an UNKNOWN input is refused — kro would ignore it, so a misspelled `subnetIds` would
      otherwise render as a silently-absent required field, which is exactly the failure this
      check exists to catch appearing in a new disguise.

    Optional inputs (kro's `default=`) may be absent; that is what the marker means.
    """
    for obj in objects:
        if obj.get("apiVersion") != "kro.run/v1alpha1":
            continue
        kind = obj.get("kind", "")
        name = (obj.get("metadata") or {}).get("name", "<unnamed>")
        schema = deps.schema_for(kind)
        supplied = set((obj.get("spec") or {}).keys())

        missing = sorted(schema.required_inputs - supplied)
        if missing:
            raise RenderError(
                f"{kind}/{name}: does not supply {', '.join(missing)}, which "
                f"{kind}'s resource graph declares as required input(s) with no default. "
                f"Applying this object would be rejected, or would create partial "
                f"infrastructure with no working cluster"
            )
        unknown = sorted(supplied - schema.known_inputs)
        if unknown:
            raise RenderError(
                f"{kind}/{name}: supplies {', '.join(unknown)}, which {kind}'s resource "
                f"graph does not declare. kro ignores an undeclared field, so this would be "
                f"silently dropped — a misspelled required input looks exactly like this"
            )


def _check_output(objects: list[dict], request: AccountFactoryRequest) -> None:
    """Refuse output that leaves the workspace, names a legacy target, or carries a secret.

    Runs over the rendered set as a whole, including its serialized form, so a violation
    introduced through any field is caught rather than only the fields checked by name.
    This is a self-check on this module's own output — it is the negative evidence AC-01
    asks for, produced by the code path that would ship the defect.
    """
    import json

    for obj in objects:
        kind = obj.get("kind")
        metadata = obj.get("metadata") or {}
        name = metadata.get("name", "<unnamed>")
        where = f"{kind}/{name}"

        if kind == "Namespace":
            if name != request.workspace_id:
                raise RenderError(
                    f"{where}: rendered a Namespace other than the workspace's own "
                    f"({request.workspace_id!r})"
                )
            continue

        namespace = metadata.get("namespace")
        if not namespace:
            raise RenderError(
                f"{where}: no explicit metadata.namespace, so it would be applied to "
                f"whatever namespace the kubeconfig context happens to select"
            )
        if namespace in _CORE_NAMESPACES:
            raise RenderError(f"{where}: targets the core ADP namespace {namespace!r}")
        if namespace != request.workspace_id:
            raise RenderError(
                f"{where}: namespace {namespace!r} is not this workspace's namespace "
                f"({request.workspace_id!r})"
            )
        if kind == "Secret":
            raise RenderError(
                f"{where}: a Secret is never rendered. Cross-account access uses an "
                f"IAMRoleSelector naming a role ARN, not a credential"
            )

    serialized = json.dumps(objects, sort_keys=True)

    for value, why in LEGACY_FORBIDDEN_VALUES.items():
        if value.lower() in serialized.lower():
            raise RenderError(
                f"the rendered set contains the legacy target {value!r}, which is {why}"
            )

    for description, pattern in _SECRET_PATTERNS:
        if not pattern.search(serialized):
            continue
        # Name the shape and the object it is in, never the value. A refusal message is
        # itself written to logs and CI artifacts, so quoting even a prefix of the matched
        # text would disclose the thing being refused — the error would become the leak.
        locations = sorted(
            f"{obj.get('kind')}/{(obj.get('metadata') or {}).get('name', '<unnamed>')}"
            for obj in objects
            if pattern.search(json.dumps(obj, sort_keys=True))
        )
        raise RenderError(
            f"the rendered set contains what looks like {description}, in "
            f"{', '.join(locations) or 'the rendered set'}. The matched value is "
            f"deliberately not reproduced here: rendered output and these messages are "
            f"reviewed, logged and committed to artifacts, so no secret value may appear "
            f"in either. Remove it from the request — this module renders identity "
            f"references (role ARNs), never credentials"
        )

    if "${" in serialized:
        # An unsubstituted template marker. The legacy `04-provision-account.yaml` shipped
        # `ACCOUNT_NAME_PLACEHOLDER` values that kubectl would apply literally; the same
        # class of defect with kro's `${...}` syntax would bind a resource to a literal
        # string. `check_rendered_manifests.py` catches the REPLACE_WITH_ form for the
        # rollout lane; this catches this module's own.
        raise RenderError(
            "the rendered set contains an unsubstituted '${...}' expression, which would "
            "be applied literally"
        )
    if "PLACEHOLDER" in serialized:
        raise RenderError(
            "the rendered set contains a PLACEHOLDER value inherited from the legacy "
            "manifest template, which would be applied literally"
        )

    for obj in objects:
        for key, value in (obj.get("spec") or {}).items():
            if value is None or value == "" or value == []:
                raise RenderError(
                    f"{obj.get('kind')}/{(obj.get('metadata') or {}).get('name')}: "
                    f"spec.{key} rendered empty. An empty required field is applied as "
                    f"empty rather than rejected"
                )


def render(
    request: AccountFactoryRequest,
    authorization: ValidationAuthorization | None = None,
    *,
    lock_path: Path | None = None,
    account_id: str | None = None,
    creation_record: object | None = None,
) -> RenderResult:
    """Render the object set a request would apply. Mutates nothing.

    Order is deliberate:

    1.  `ensure_valid` — an unsupported or unauthorized request is refused before a single
        object is produced, which is the "unsupported modes fail before mutation"
        requirement.
    2.  `dependencies.load` — refuses to render against an unpinned or drifted dependency
        set, so a rendered set always corresponds to a known dependency set.
    3.  build the mode's objects.
    4.  `_check_output` — self-check for legacy targets, secret literals, namespace escape
        and unsubstituted placeholders.

    Performs no AWS call, no Kubernetes call and no network access. Runs with no
    credentials.

    New-account rendering requires `creation_record`, loaded by the maintained
    account-provisioning registration adapter from this operation's successful
    durable creation history and verified OU placement. The legacy `account_id`
    argument refuses every supplied value. A string is not creation evidence.
    Existing-account modes continue to use their explicitly authorized target
    account and may not consume a new-account creation record.
    """
    unchecked = ensure_valid(request, authorization)
    deps = dependencies.load(lock_path)

    from .registration import CreatedAccountRegistration
    from .creation import account_identity_key

    if account_id is not None and not request.mode.creates_account:
        raise RenderError(
            "adopting modes use target_account_id and cannot consume a creation record"
        )
    if account_id is not None:
        raise RenderError(
            "caller-supplied account_id is not creation evidence; use the trusted durable registration adapter"
        )
    if request.mode.creates_account:
        if not isinstance(creation_record, CreatedAccountRegistration):
            raise RenderError(
                "new-account rendering requires the operation-bound CreateAccount record from creation_runner through the trusted registration adapter"
            )
        if (
            creation_record.organization_id,
            creation_record.workspace_id,
            creation_record.approved_identity,
        ) != (
            request.organization_id,
            request.workspace_id,
            account_identity_key(request),
        ):
            raise RenderError(
                "creation record belongs to a different operation payload or workspace"
            )
        account_id = creation_record.account_id
    elif creation_record is not None:
        raise RenderError("adopted-account onboarding must not use a creation record")

    builder = _STAGE_BUILDERS.get(request.mode)
    if builder is None:  # pragma: no cover - defensive; validated above
        raise RenderError(f"no renderer for mode {request.mode!r}")

    # The namespace is stage 0 in every mode: every later object names it explicitly, so it
    # must exist first. Rendering it here rather than letting a graph create it also keeps the
    # workspace's ownership labels present in all three modes rather than only some.
    stages = [
        RenderStage(
            number=0, name="workspace namespace", objects=(_namespace_object(request),)
        ),
        *builder(request, account_id.strip() if account_id else None),
    ]
    objects = [obj for stage in stages for obj in stage.objects]

    _check_output(objects, request)
    _check_graph_inputs(objects, deps)

    declared = {
        graph.declares for graph in deps.resource_graphs if graph.schema is not None
    }
    required = {
        obj["kind"] for obj in objects if obj["apiVersion"] == "kro.run/v1alpha1"
    }
    missing = sorted(required - declared)
    if (
        missing
    ):  # pragma: no cover - `_check_graph_inputs` raises first via `schema_for`
        raise RenderError(
            f"the rendered set instantiates {', '.join(missing)}, which no resource graph "
            f"declares. Applying it would create a custom resource with no definition "
            f"behind it"
        )

    return RenderResult(
        request=request,
        objects=tuple(objects),
        prerequisites=_prerequisites(deps, request),
        resource_graph_files=tuple(graph.filename for graph in deps.resource_graphs),
        unchecked_authorization=tuple(unchecked),
        stages=tuple(stages),
    )
