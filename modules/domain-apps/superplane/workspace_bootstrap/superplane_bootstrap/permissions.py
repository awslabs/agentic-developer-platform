"""Effective worker installation permissions, checked before the first mutation.

This preflight grants nothing and does not replace F18 temporary-access recovery
or post-revocation tenant checks. Kubernetes remains authoritative at each write.
"""

from .errors import BootstrapRefused


def verify_install_permissions(access, namespace, service_account, controller, crds):
    def allowed(verb, resource, scope=None, name=None):
        answer = access.bootstrap_permission(
            verb=verb, resource=resource, namespace=scope, name=name
        )
        if type(answer) is not bool:
            raise BootstrapRefused("bootstrap permission was not answered")
        return answer

    def require(verb, resource, scope=None, name=None):
        if not allowed(verb, resource, scope, name):
            raise BootstrapRefused(
                f"bootstrap permission denied: {verb} {resource} in {scope or 'cluster'}"
            )

    # kubectl create needs collection authority; get/patch use exact object names.
    require("get", "namespaces", name=namespace)
    if access.namespace(namespace) is None:
        require("create", "namespaces")
    role = f"{service_account}-workspace"
    cluster_role = f"{service_account}-{namespace}-cluster"
    objects = [
        ("serviceaccounts", namespace, service_account),
        ("roles.rbac.authorization.k8s.io", namespace, role),
        ("rolebindings.rbac.authorization.k8s.io", namespace, role),
        ("clusterroles.rbac.authorization.k8s.io", None, cluster_role),
        ("clusterrolebindings.rbac.authorization.k8s.io", None, cluster_role),
        ("deployments.apps", namespace, controller),
        *(
            ("customresourcedefinitions.apiextensions.k8s.io", None, name)
            for name in crds
        ),
    ]
    for resource, scope, name in objects:
        require("create", resource, scope)
        require("get", resource, scope, name)
        require("patch", resource, scope, name)

    # Require explicit, exact-role bind/escalate authorization for this temporary
    # worker. Ordinary SSAR allows may come from additive EKS access policies;
    # they are not proof of the RBAC rule-resolution fallback Kubernetes uses
    # for no-escalation checks. Never infer bind authority from those allows.
    for resource, scope, name in [
        ("roles.rbac.authorization.k8s.io", namespace, role),
        ("clusterroles.rbac.authorization.k8s.io", None, cluster_role),
    ]:
        require("bind", resource, scope, name)
        require("escalate", resource, scope, name)
