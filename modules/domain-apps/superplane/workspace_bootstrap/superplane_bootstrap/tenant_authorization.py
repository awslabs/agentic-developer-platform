"""EKS access-policy and Kubernetes RBAC proof for tenant namespace protection.

SubjectAccessReview tests RBAC only. EKS access policies are a separate additive
permission source, so every STANDARD entry is enumerated and checked as well.
Unknown policy types, external OIDC, incomplete responses and command failures
refuse. No impersonation grant or ADP-subject-as-Kubernetes-username assumption.
"""

from __future__ import annotations

import json
import re

from .errors import BootstrapRefused

POLICY_PREFIX = "arn:aws:eks::aws:cluster-access-policy/"
NAMESPACE_POLICIES = {
    POLICY_PREFIX + name
    for name in (
        "AmazonEKSAdminPolicy",
        "AmazonEKSEditPolicy",
        "AmazonEKSViewPolicy",
    )
}


def eks_tenant_principals(
    aws, cluster_name, bootstrap_principal, *, registrar_authority=None
):
    """Read all tenant authority, including any named bootstrap principal.

    `aws` is a trusted JSON read adapter; the CLI performs AWS pagination.
    `bootstrap_principal` preserves the composition interface but grants no
    exemption: an ARN alone proves neither temporary ownership nor revocation.
    """
    external = aws(
        "eks", "list-identity-provider-configs", "--cluster-name", cluster_name
    )
    if external.get("identityProviderConfigs") != []:
        raise BootstrapRefused(
            "external Kubernetes identity providers require a separate trusted tenant proof"
        )
    entries = aws("eks", "list-access-entries", "--cluster-name", cluster_name)
    principals = entries.get("accessEntries")
    if not isinstance(principals, list):
        raise BootstrapRefused("EKS tenant access inventory was not answered")
    result = []
    for arn in principals:
        response = aws(
            "eks",
            "describe-access-entry",
            "--cluster-name",
            cluster_name,
            "--principal-arn",
            arn,
        )
        entry = response.get("accessEntry", {})
        if entry.get("principalArn") != arn:
            raise BootstrapRefused("EKS tenant access entry identity differs")
        kind = entry.get("type")
        if kind in {"EC2_LINUX", "EC2_WINDOWS", "FARGATE_LINUX", "HYBRID_LINUX", "EC2"}:
            continue  # Provider-managed node identities are not tenant operators.
        if kind != "STANDARD":
            raise BootstrapRefused("unknown EKS tenant access entry type")
        policies = aws(
            "eks",
            "list-associated-access-policies",
            "--cluster-name",
            cluster_name,
            "--principal-arn",
            arn,
        ).get("associatedAccessPolicies")
        if not isinstance(policies, list):
            raise BootstrapRefused("EKS tenant access policies were not answered")
        if arn == bootstrap_principal and registrar_authority is not None:
            # Only the live, generation-fenced journal can supply this verifier.
            # Final post-revocation enumeration supplies no exemption at all.
            registrar_authority(arn, entry, policies)
            continue
        for policy in policies:
            scope = policy.get("accessScope", {})
            if (
                policy.get("policyArn") not in NAMESPACE_POLICIES
                or scope.get("type") != "namespace"
                or not scope.get("namespaces")
            ):
                raise BootstrapRefused(
                    "tenant EKS access policy is not a verified namespace-only policy"
                )
        username, groups = entry.get("username"), entry.get("kubernetesGroups")
        if (
            not isinstance(username, str)
            or not username
            or not isinstance(groups, list)
            or not all(isinstance(g, str) and g for g in groups)
        ):
            raise BootstrapRefused("EKS tenant Kubernetes identity was not answered")
        result.append((username, tuple(groups)))
    return result


def review(access, *, user, groups, verb, resource, name=None, namespace=None):
    attrs = {"verb": verb, "group": "", "resource": resource}
    if "." in resource:
        attrs["resource"], attrs["group"] = resource.split(".", 1)
    if name:
        attrs["name"] = name
    if namespace:
        attrs["namespace"] = namespace
    payload = {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SubjectAccessReview",
        "spec": {
            "user": user,
            "groups": list(groups),
            "resourceAttributes": attrs,
        },
    }
    result = access._kube("create", "-f", "-", "-o", "json", data=json.dumps(payload))
    try:
        status = json.loads(result.stdout)["status"]
        allowed = status["allowed"]
    except (ValueError, KeyError, TypeError) as exc:
        raise BootstrapRefused(
            "tenant authorization probe failed; denial was not verified"
        ) from exc
    if result.returncode or type(allowed) is not bool or status.get("evaluationError"):
        raise BootstrapRefused(
            "tenant authorization probe failed; denial was not verified"
        )
    return allowed


def _subjects(items, namespace):
    subjects = set()
    for item in items:
        for subject in item.get("subjects", []):
            kind, name = subject.get("kind"), subject.get("name")
            if (
                kind not in {"User", "Group", "ServiceAccount"}
                or not isinstance(name, str)
                or not name
            ):
                raise BootstrapRefused("tenant RBAC contains an unknown principal")
            subjects.add((kind, name, subject.get("namespace", namespace)))
    return subjects


def can_tenants_patch_namespace(access, namespace):
    from .adapters import _parse_json, _require_success

    def items(resource, scoped=False):
        args = ("get", resource, *(("-n", namespace) if scoped else ()), "-o", "json")
        payload = _parse_json(
            _require_success(access._kube(*args), "reading tenant RBAC subjects"),
            "tenant RBAC subjects",
        )
        if not isinstance(payload.get("items"), list) or payload.get(
            "metadata", {}
        ).get("continue"):
            raise BootstrapRefused("tenant RBAC inventory was not answered")
        return payload["items"]

    subjects = _subjects(items("rolebindings", True), namespace)
    for sa in items("serviceaccounts", True):
        name = sa.get("metadata", {}).get("name")
        if not name:
            raise BootstrapRefused("tenant service account inventory was not answered")
        subjects.add(("ServiceAccount", name, namespace))
    subjects.add(("ServiceAccount", "default", namespace))
    cluster_subjects = _subjects(items("clusterrolebindings"), namespace)
    reader = access.tenant_identity_reader
    if reader is None:
        raise BootstrapRefused("trusted EKS tenant identity inventory is required")
    identities = []
    for username, groups in reader():
        # IAM role usernames contain session templates. RBAC user bindings are
        # finite exact strings: evaluate every matching binding plus an unbound
        # representative session, with the exact EKS groups in every review.
        pattern = re.escape(username)
        for template in ("{{SessionName}}", "{{SessionNameRaw}}"):
            pattern = pattern.replace(re.escape(template), ".+")
        users = {
            re.sub(r"\{\{SessionName(?:Raw)?\}\}", "adp-authorization-probe", username)
        }
        users.update(
            name
            for kind, name, _ in subjects | cluster_subjects
            if kind == "User" and re.fullmatch(pattern, name)
        )
        identities.extend(
            (user, tuple(groups) + ("system:authenticated",)) for user in users
        )
    for kind, name, ns in sorted(subjects):
        if kind == "ServiceAccount":
            identities.append(
                (
                    f"system:serviceaccount:{ns}:{name}",
                    (
                        "system:serviceaccounts",
                        f"system:serviceaccounts:{ns}",
                        "system:authenticated",
                    ),
                )
            )
        elif kind == "Group":
            identities.append(
                ("adp-tenant-authorization-probe", (name, "system:authenticated"))
            )
        else:
            identities.append((name, ("system:authenticated",)))
    return any(
        review(
            access,
            user=user,
            groups=groups,
            verb=verb,
            resource="namespaces",
            name=namespace,
        )
        for user, groups in identities
        for verb in ("patch", "update")
    )
