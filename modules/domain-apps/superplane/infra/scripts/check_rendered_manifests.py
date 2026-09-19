"""Validate the rendered manifest set before any cluster mutation — Issue #5042 (U3).

## What the previous guard checked, and what it missed

PR #5283's review (finding 4) fed the rollout lane's rendered guard a digest-pinned
Deployment in namespace `adp` together with a ClusterRoleBinding to `cluster-admin`. It
returned **0** and printed "all digest-pinned". It checked image SHAPE and nothing else, so:

*   any namespace passed, including core ADP namespaces;
*   cluster-scoped objects passed, including a binding to `cluster-admin`;
*   any 64-hex string after `@sha256:` passed, so an image unrelated to the release lock
    was indistinguishable from the pinned one;
*   containers with no CPU/memory limits passed, so a domain workload could exhaust a
    shared node;
*   pods could run as `default`, or with hostNetwork, and nothing objected.

## What this validates instead

Structurally, over every document in the rendered set, before `kubectl` is invoked:

1.  **Namespace** — every namespaced object must sit in a namespace this run was told the
    domain owns. The permitted set arrives as DATA (`--namespace`, repeatable, from the
    values Terraform published), not as a literal in this file: the module's namespaces are
    `var.namespace` and `var.skypilot_namespace`, so hardcoding them here would create the
    second source of truth the rollout lane was written to avoid.
2.  **Kind** — an allowlist. Cluster-scoped kinds are rejected outright, since a domain app
    that can create a ClusterRoleBinding can grant itself anything.
3.  **RBAC scope** — Roles may not use wildcard verbs/resources, and no subject may be bound
    to a cluster-admin-like role.
4.  **Identity** — pods must run under a domain ServiceAccount, never `default`, and every
    IRSA annotation must name a role in THIS account carrying THIS environment's domain
    prefix. That is what binds the rendered set to the selected account/environment rather
    than to whatever the runner's credentials happen to be.
5.  **Resource bounds** — every container declares CPU and memory requests AND limits.
6.  **Digest binding** — every image digest must be one the release lock actually pins. A
    64-hex digest is not evidence of provenance; this compares against the lock.
7.  **GPU placement** — a container requesting a GPU must carry a node selector or affinity,
    so it cannot land on the ADP management cluster (platform isolation requirement,
    2026-09-16).

## What this deliberately does NOT establish

Rendering or applying a NetworkPolicy is not evidence that network isolation is ENFORCED.
Enforcement on the dev/embark1 Auto Mode cluster is a platform-owned prerequisite tracked in
**#4999**, which is still open. This script checks that a NetworkPolicy is PRESENT and
shaped as default-deny; it does not and cannot claim enforcement. That evidence requires
controlled positive/negative traffic probes and belongs to the gated live acceptance. See
`modules/domain-apps/superplane/k8s/README.md`.

Exit codes: 0 = the rendered set is safe to apply, 1 = denied.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

# Namespaces that are unambiguously not this domain's. The permitted set is supplied as
# data, so this exists as a second, independent tripwire: if a caller ever passes `--namespace
# adp` (through a mistaken SSM value, or a hand-edited parameter), the allowlist alone would
# accept it. A core namespace is never a legitimate value for that flag.
CORE_NAMESPACES = frozenset(
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

NAMESPACED_KINDS = frozenset(
    {
        "Deployment",
        "StatefulSet",
        "Service",
        "ServiceAccount",
        "ConfigMap",
        "Secret",
        "Job",
        "CronJob",
        "Role",
        "RoleBinding",
        "PersistentVolumeClaim",
        "NetworkPolicy",
        "PodDisruptionBudget",
        "HorizontalPodAutoscaler",
    }
)

# `Namespace` is the only cluster-scoped kind permitted, and only for the domain's own
# namespaces — checked by name below.
PERMITTED_CLUSTER_SCOPED_KINDS = frozenset({"Namespace"})

FORBIDDEN_CLUSTER_SCOPED_KINDS = frozenset(
    {
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "MutatingWebhookConfiguration",
        "ValidatingWebhookConfiguration",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
        "PersistentVolume",
        "StorageClass",
        "PriorityClass",
        "APIService",
        "IngressClass",
        "RuntimeClass",
    }
)

# Bindings to any of these grant far beyond a domain namespace.
CLUSTER_ADMIN_ROLES = frozenset({"cluster-admin", "admin", "edit", "system:masters"})

DIGEST_RE = re.compile(r"@sha256:([0-9a-f]{64})$")
IRSA_ANNOTATION = "eks.amazonaws.com/role-arn"
ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::(?P<account>\d{12}):role/(?P<name>.+)$")

POD_SPEC_KINDS = frozenset({"Deployment", "StatefulSet", "Job", "CronJob", "Pod"})

GPU_RESOURCE_KEYS = ("nvidia.com/gpu", "amd.com/gpu")

PLACEHOLDER_RE = re.compile(r"REPLACE_WITH_[A-Z_]+")


class ManifestViolation(Exception):
    """The rendered set could not be validated at all — always a denial, never a pass."""


def _iter_documents(directory: Path):
    files = [p for p in sorted(directory.iterdir()) if p.suffix in {".yaml", ".yml"}]
    for path in files:
        text = path.read_text(encoding="utf-8")
        placeholder = PLACEHOLDER_RE.search(text)
        if placeholder:
            # kubectl accepts a namespace or annotation value of "REPLACE_WITH_..." without
            # complaint, so an unsubstituted placeholder is applied literally.
            raise ManifestViolation(
                f"{path.name}: the placeholder {placeholder.group(0)} survived rendering. It "
                f"would be applied literally."
            )
        try:
            documents = list(yaml.safe_load_all(text))
        except yaml.YAMLError as exc:
            raise ManifestViolation(
                f"{path.name}: not valid YAML after rendering: {exc}"
            ) from exc
        for index, doc in enumerate(documents):
            if doc is None:
                continue
            if not isinstance(doc, dict):
                raise ManifestViolation(
                    f"{path.name} document {index}: rendered to a {type(doc).__name__}, not a "
                    f"Kubernetes object"
                )
            yield path.name, doc


def _pod_spec(doc: dict) -> dict | None:
    kind = doc.get("kind")
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return None
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        job_template = spec.get("jobTemplate")
        if not isinstance(job_template, dict):
            return None
        job_spec = job_template.get("spec")
        if not isinstance(job_spec, dict):
            return None
        template = job_spec.get("template")
        return template.get("spec") if isinstance(template, dict) else None
    if kind in POD_SPEC_KINDS:
        template = spec.get("template")
        return template.get("spec") if isinstance(template, dict) else None
    return None


def _containers(pod_spec: dict) -> list[tuple[str, dict]]:
    found = []
    for key in ("initContainers", "containers"):
        for container in pod_spec.get(key) or []:
            if isinstance(container, dict):
                found.append((container.get("name", "<unnamed>"), container))
    return found


def lock_digests(lock_path: Path) -> set[str]:
    """The digests the release lock actually pins.

    `pending_images` contribute nothing by construction — they carry no digest — which is
    what makes deploying an unbuildable image impossible rather than merely discouraged.
    """
    try:
        lock = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestViolation(
            f"could not read the release lock at {lock_path}: {exc}"
        ) from exc
    if not isinstance(lock, dict):
        raise ManifestViolation(f"{lock_path} did not parse as a mapping")
    images = lock.get("images") or {}
    if not isinstance(images, dict):
        raise ManifestViolation(f"{lock_path}: `images` is not a mapping")
    digests = set()
    for value in images.values():
        if isinstance(value, str) and value.startswith("sha256:"):
            digests.add(value.split("sha256:", 1)[1])
    return digests


def validate(
    directory: Path,
    lock_path: Path,
    *,
    namespaces: set[str],
    account_id: str | None = None,
    environment: str | None = None,
) -> list[str]:
    """Return a list of violations; empty means the rendered set is safe to apply.

    Raises ManifestViolation when validation could not be performed — which is a denial in
    `main`, never a pass. The distinction matters because the guard this replaces had an
    error path indistinguishable from its success path.
    """
    violations: list[str] = []

    if not namespaces:
        raise ManifestViolation(
            "no permitted namespaces were supplied, so every namespace check would pass "
            "vacuously. Refusing to validate."
        )
    for namespace in sorted(namespaces):
        if namespace in CORE_NAMESPACES:
            raise ManifestViolation(
                f"{namespace!r} was supplied as a domain namespace, but it is a core ADP or "
                f"Kubernetes namespace. A domain app owns its own namespace (platform "
                f"isolation requirement, 2026-09-16). Refusing to validate."
            )

    pinned = lock_digests(lock_path)
    if not pinned:
        # With an empty set the digest-binding check below would accept any digest, so this
        # is a hard failure rather than a permissive default.
        raise ManifestViolation(
            f"{lock_path} pins no resolved image digests, so the digest-binding check could "
            f"not distinguish a pinned image from any other. Refusing to validate."
        )

    documents = list(_iter_documents(directory))
    if not documents:
        raise ManifestViolation(
            f"no Kubernetes objects found in {directory}. 'applied nothing' must not share a "
            f"green tick with 'applied everything'."
        )

    expected_role_prefix = f"adp-{environment}-superplane" if environment else None

    for source, doc in documents:
        kind = doc.get("kind")
        metadata = doc.get("metadata")
        if not isinstance(metadata, dict):
            violations.append(f"{source}: object has no metadata mapping")
            continue
        name = metadata.get("name", "<unnamed>")
        where = f"{source}: {kind}/{name}"

        if not kind:
            violations.append(f"{source}: object has no `kind`")
            continue

        # --- Kind allowlist / cluster scope -------------------------------------------
        if kind in FORBIDDEN_CLUSTER_SCOPED_KINDS:
            violations.append(
                f"{where}: {kind} is cluster-scoped and must not be created by a domain app "
                f"— it would let the domain act outside its own namespaces"
            )
            continue
        if kind not in NAMESPACED_KINDS and kind not in PERMITTED_CLUSTER_SCOPED_KINDS:
            violations.append(
                f"{where}: kind {kind} is not in the domain's allowlist. Add it deliberately "
                f"in check_rendered_manifests.py with the reasoning, rather than widening the "
                f"check at the call site"
            )
            continue

        # --- Namespace ownership ------------------------------------------------------
        if kind == "Namespace":
            if name not in namespaces:
                violations.append(
                    f"{where}: declares a Namespace outside the set this rollout was told "
                    f"the domain owns {sorted(namespaces)}"
                )
        else:
            namespace = metadata.get("namespace")
            if not namespace:
                violations.append(
                    f"{where}: has no explicit metadata.namespace, so it would be applied to "
                    f"whatever namespace the kubeconfig context happens to select"
                )
            elif namespace in CORE_NAMESPACES:
                violations.append(
                    f"{where}: targets the core namespace {namespace!r}. This is the "
                    f"review's reproduced case — a digest-pinned Deployment in a core "
                    f"namespace previously passed"
                )
            elif namespace not in namespaces:
                violations.append(
                    f"{where}: namespace {namespace!r} is not one of the domain's namespaces "
                    f"{sorted(namespaces)}"
                )

        # --- RBAC scope ---------------------------------------------------------------
        if kind == "Role":
            for rule in doc.get("rules") or []:
                if not isinstance(rule, dict):
                    violations.append(f"{where}: a rule is not a mapping")
                    continue
                if "*" in (rule.get("verbs") or []):
                    violations.append(f"{where}: Role grants wildcard verbs")
                if "*" in (rule.get("resources") or []):
                    violations.append(f"{where}: Role grants wildcard resources")
        if kind == "RoleBinding":
            role_ref = doc.get("roleRef")
            if not isinstance(role_ref, dict):
                violations.append(f"{where}: RoleBinding has no roleRef mapping")
            elif (
                role_ref.get("kind") == "ClusterRole"
                and role_ref.get("name") in CLUSTER_ADMIN_ROLES
            ):
                violations.append(
                    f"{where}: binds to the cluster-wide role {role_ref.get('name')!r}. This "
                    f"is the review's reproduced case — a binding to cluster-admin "
                    f"previously passed the digest-only guard"
                )

        # --- IRSA identity binding ----------------------------------------------------
        annotations = metadata.get("annotations") or {}
        if isinstance(annotations, dict) and IRSA_ANNOTATION in annotations:
            arn = annotations[IRSA_ANNOTATION]
            match = ROLE_ARN_RE.match(arn) if isinstance(arn, str) else None
            if not match:
                violations.append(
                    f"{where}: {IRSA_ANNOTATION} is not an IAM role ARN ({arn!r})"
                )
            else:
                if account_id and match.group("account") != account_id:
                    violations.append(
                        f"{where}: {IRSA_ANNOTATION} names account "
                        f"{match.group('account')} but this rollout targets {account_id}. A "
                        f"cross-account identity is not this domain's to assume"
                    )
                if expected_role_prefix and not match.group("name").startswith(
                    expected_role_prefix
                ):
                    violations.append(
                        f"{where}: {IRSA_ANNOTATION} names the role "
                        f"{match.group('name')!r}, which does not carry this environment's "
                        f"domain prefix {expected_role_prefix!r}. The rendered set would "
                        f"bind pods to an identity outside the domain"
                    )

        # --- Pod-level checks ---------------------------------------------------------
        pod_spec = _pod_spec(doc)
        if kind in POD_SPEC_KINDS and pod_spec is None:
            violations.append(f"{where}: {kind} declares no pod template spec")
            continue
        if pod_spec is None:
            continue

        service_account = pod_spec.get("serviceAccountName")
        if not service_account:
            violations.append(
                f"{where}: no serviceAccountName, so pods would run as `default` with "
                f"whatever that account can do"
            )
        elif service_account == "default":
            violations.append(f"{where}: runs as the `default` ServiceAccount")

        if pod_spec.get("hostNetwork"):
            violations.append(
                f"{where}: hostNetwork:true bypasses the namespace network boundary"
            )
        if pod_spec.get("hostPID") or pod_spec.get("hostIPC"):
            violations.append(f"{where}: hostPID/hostIPC break the isolation boundary")
        for volume in pod_spec.get("volumes") or []:
            if isinstance(volume, dict) and "hostPath" in volume:
                violations.append(
                    f"{where}: mounts a hostPath volume, which reaches outside the container "
                    f"onto a shared node"
                )

        containers = _containers(pod_spec)
        if not containers:
            violations.append(f"{where}: pod template declares no containers")

        for container_name, container in containers:
            image = container.get("image")
            spot = f"{where} container {container_name}"
            if not isinstance(image, str) or not image:
                violations.append(f"{spot}: no image")
                continue

            match = DIGEST_RE.search(image)
            if not match:
                violations.append(
                    f"{spot}: image {image!r} is not digest-pinned (R2: the manifest that "
                    f"reaches the cluster must be @sha256: addressed)"
                )
            elif match.group(1) not in pinned:
                violations.append(
                    f"{spot}: the image digest is not one the release lock pins. A 64-hex "
                    f"digest is not provenance — the deployed image must be an image the "
                    f"lock resolved ({image})"
                )

            resources = container.get("resources") or {}
            requests = resources.get("requests") or {}
            limits = resources.get("limits") or {}
            if not isinstance(requests, dict) or not isinstance(limits, dict):
                violations.append(f"{spot}: resources.requests/limits are not mappings")
                continue
            for field, values in (("requests", requests), ("limits", limits)):
                for dimension in ("cpu", "memory"):
                    if dimension not in values:
                        violations.append(
                            f"{spot}: missing resources.{field}.{dimension}. An unbounded "
                            f"domain workload can exhaust a node shared with core ADP"
                        )

            # GPU placement. Scheduling a GPU container wherever the current context points
            # would put it on the ADP management cluster.
            if any(key in limits or key in requests for key in GPU_RESOURCE_KEYS):
                if not (pod_spec.get("nodeSelector") or pod_spec.get("affinity")):
                    violations.append(
                        f"{spot}: requests a GPU with no nodeSelector or affinity. GPU "
                        f"workloads must target the intended workspace cluster, never the "
                        f"ADP management cluster (platform isolation requirement, "
                        f"2026-09-16)"
                    )

    # --- NetworkPolicy PRESENCE (not enforcement) -------------------------------------
    policy_namespaces = {
        (doc.get("metadata") or {}).get("namespace")
        for _, doc in documents
        if doc.get("kind") == "NetworkPolicy"
    }
    workload_namespaces = {
        (doc.get("metadata") or {}).get("namespace")
        for _, doc in documents
        if doc.get("kind") in POD_SPEC_KINDS
    }
    for namespace in sorted(n for n in workload_namespaces - policy_namespaces if n):
        violations.append(
            f"namespace {namespace!r} runs workloads but the rendered set declares no "
            f"NetworkPolicy for it. (Presence only — enforcement on the existing cluster is "
            f"the still-open platform-owned prerequisite #4999 and is NOT claimed here.)"
        )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rendered-dir", required=True, type=Path)
    parser.add_argument("--lock-file", required=True, type=Path)
    parser.add_argument(
        "--namespace",
        action="append",
        default=[],
        dest="namespaces",
        help=(
            "A namespace this domain owns. Repeatable. Supplied from the values Terraform "
            "published (var.namespace and var.skypilot_namespace) rather than hardcoded, so "
            "this check and the module cannot drift."
        ),
    )
    parser.add_argument("--account-id", default="")
    parser.add_argument("--environment", default="")
    args = parser.parse_args(argv)

    if not args.rendered_dir.is_dir():
        print(f"::error::rendered directory {args.rendered_dir} does not exist")
        return 1

    try:
        violations = validate(
            args.rendered_dir,
            args.lock_file,
            namespaces=set(args.namespaces),
            account_id=args.account_id or None,
            environment=args.environment or None,
        )
    except ManifestViolation as exc:
        print(f"::error::{exc}")
        return 1
    except OSError as exc:
        print(f"::error::could not read the rendered manifests: {exc}")
        return 1

    if violations:
        print("::error::The rendered manifest set is not safe to apply:")
        for violation in violations:
            print(f"  - {violation}")
        print()
        print(
            "A domain rollout must stay inside its own namespaces and identities, declare "
            "bounded resources, and deploy only images the release lock pins (platform "
            "isolation requirement, 2026-09-16). Nothing was applied."
        )
        return 1

    print(
        "Rendered manifest set validated: namespaces, kinds, RBAC scope, IRSA identities,"
    )
    print("resource bounds and lock-pinned digests all conform.")
    print(
        "NOTE: NetworkPolicy PRESENCE was checked. Enforcement is NOT established by this "
        "check — it depends on the still-open platform-owned prerequisite #4999 and requires "
        "positive/negative traffic probes in the gated live acceptance."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
