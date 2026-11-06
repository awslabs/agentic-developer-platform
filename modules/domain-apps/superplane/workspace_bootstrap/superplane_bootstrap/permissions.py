"""Effective worker installation permissions, checked before the first mutation.

This preflight grants nothing and does not replace F18 temporary-access recovery
or post-revocation tenant checks. Kubernetes remains authoritative at each write.
"""

from .errors import BootstrapRefused


def verify_install_permissions(
    access, namespace, service_account, controller, crds, *, submit=None
):
    """Verify installation reads; an optional submitter must join every check."""
    from .authority_runtime import FencedClusterAccess

    if isinstance(access, FencedClusterAccess):
        return _verify_fenced_permissions(
            access, namespace, service_account, controller, crds, submit=submit
        )

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

    if submit is None:

        def submit(check, *args):
            check(*args)

    # kubectl create needs collection authority; get/patch use exact object names.
    # Keep this prerequisite synchronous before reading the namespace itself.
    require("get", "namespaces", name=namespace)
    if access.namespace(namespace) is None:
        submit(require, "create", "namespaces")
    role = f"{service_account}-workspace"
    cluster_role = f"{service_account}-{namespace}-cluster"
    objects = [
        ("serviceaccounts", namespace, service_account),
        ("roles.rbac.authorization.k8s.io", namespace, role),
        ("rolebindings.rbac.authorization.k8s.io", namespace, role),
        ("clusterroles.rbac.authorization.k8s.io", None, cluster_role),
        ("clusterrolebindings.rbac.authorization.k8s.io", None, cluster_role),
        *(
            ("customresourcedefinitions.apiextensions.k8s.io", None, name)
            for name in crds
        ),
    ]
    if getattr(access, "controller_mode", None) != "management":
        objects.append(("deployments.apps", namespace, controller))
    for resource, scope, name in objects:
        submit(require, "create", resource, scope)
        submit(require, "get", resource, scope, name)
        submit(require, "patch", resource, scope, name)

    # Exact-role bind covers the roleRef, and named escalate covers named updates.
    # Collection Role/ClusterRole CREATE has no authorization request name, so
    # this named SSAR does not prove create-time escalation. compile_grants also
    # gives the installer the intended controller rules in their original scopes
    # for Kubernetes' RBAC no-escalation fallback. Ordinary SSAR allows may come
    # from additive EKS policies and cannot substitute for those held RBAC rules.
    for resource, scope, name in [
        ("roles.rbac.authorization.k8s.io", namespace, role),
        ("clusterroles.rbac.authorization.k8s.io", None, cluster_role),
    ]:
        submit(require, "bind", resource, scope, name)
        submit(require, "escalate", resource, scope, name)


def _verify_fenced_permissions(
    access, namespace, service_account, controller, crds, *, submit
):
    """Bracket this finite read batch with fresh complete grant inventories.

    The installation preflight otherwise enters TemporaryAuthority.mutate for
    each permission read, repeating the entire grant inventory dozens of times.
    Keep the reservation fence on the caller and join every independent read
    before releasing it. Actor transports still check current authority on every
    request. No observation survives this batch; mutation fencing is unchanged.
    """
    from concurrent.futures import ThreadPoolExecutor

    if access.supervisor or submit is not None:
        raise BootstrapRefused("installation permission batch requires its own fence")
    authority = access.authority
    with authority.journal.fenced():
        _, progress = authority.journal.read_locked()
        authority._require_phase(progress, "active")
        authority.backend.verify_worker_binding()
        with ThreadPoolExecutor(max_workers=4) as executor:
            reads = []

            def enqueue(check, *args):
                reads.append(executor.submit(check, *args))

            verify_install_permissions(
                access.raw,
                namespace,
                service_account,
                controller,
                crds,
                submit=enqueue,
            )
            for read in reads:
                read.result()
        authority.backend.verify_worker_binding()
