"""Complete EKS tenant identity inventory under installed cluster authority."""

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.eks_grants import _pages
from superplane_bootstrap.shared_authority import SharedWorkspaceAuthority
from superplane_bootstrap.tenant_authorization import eks_tenant_principals


# Only these read-only SDK operations implement the canonical inventory reader.
_OPERATIONS = {
    "list-identity-provider-configs": (
        "list_identity_provider_configs",
        "identityProviderConfigs",
    ),
    "list-access-entries": ("list_access_entries", "accessEntries"),
    "describe-access-entry": ("describe_access_entry", None),
    "list-associated-access-policies": (
        "list_associated_access_policies",
        "associatedAccessPolicies",
    ),
}


def installed_tenant_principals(authority):
    """Exempt only the verified installed issuer; inventory every other identity.

    The canonical namespace proof additionally inventories actual namespace SAs,
    RoleBindings and ClusterRoleBindings and reviews namespace mutation for those
    subjects. This function supplies the complete EKS side, including policies and
    username templates; it never invents a tenant username from an ADP principal.
    """
    if not isinstance(authority, SharedWorkspaceAuthority):
        raise BootstrapRefused(
            "shared tenant inventory requires installed bootstrap authority"
        )
    backend = authority.backend
    backend.verify_worker_binding()
    reference = backend.reference
    calls = 0
    issuer_seen = False

    class FreshReads:
        def __getattr__(self, name):
            if name not in {operation[0] for operation in _OPERATIONS.values()}:
                raise BootstrapRefused(
                    "tenant inventory requested a non-inventory operation"
                )

            def read(**arguments):
                nonlocal calls
                calls += 1
                if calls > 4096:
                    raise BootstrapRefused(
                        "tenant authority inventory exceeds its bound"
                    )
                backend.verify_worker_binding()
                result = getattr(backend.clients.eks, name)(**arguments)
                backend.verify_worker_binding()
                return result

            return read

    client = FreshReads()

    def read(service, operation, *arguments):
        if service != "eks" or operation not in _OPERATIONS or len(arguments) % 2:
            raise BootstrapRefused("tenant inventory requested an unsupported read")
        values = dict(zip(arguments[::2], arguments[1::2], strict=True))
        if (
            set(values) - {"--cluster-name", "--principal-arn"}
            or values.get("--cluster-name") != authority.journal.target.cluster_name
        ):
            raise BootstrapRefused("tenant inventory names another cluster")
        kwargs = {"clusterName": values["--cluster-name"]}
        if "--principal-arn" in values:
            kwargs["principalArn"] = values["--principal-arn"]
        method, key = _OPERATIONS[operation]
        if key is None:
            return getattr(client, method)(**kwargs)
        return {key: _pages(client, method, key, **kwargs)}

    def installed_issuer(principal, entry, policies):
        nonlocal issuer_seen
        # A bare role ARN or a matching group never grants an exemption. The
        # registered entry ARN and exact username/group must still match, and
        # broad AWS-managed policies are not part of the installed RBAC recipe.
        if (
            principal != reference.principal_arn
            or entry.get("accessEntryArn") != reference.access_entry_arn
            or entry.get("username") != reference.username
            or entry.get("kubernetesGroups") != [reference.group]
            or entry.get("type") != "STANDARD"
            or policies != []
        ):
            raise BootstrapRefused(
                "installed issuer tenant-inventory exemption changed"
            )
        backend.verify_worker_binding()
        issuer_seen = True

    result = eks_tenant_principals(
        read,
        authority.journal.target.cluster_name,
        reference.principal_arn,
        registrar_authority=installed_issuer,
    )
    if not issuer_seen:
        raise BootstrapRefused(
            "installed issuer was absent from complete EKS inventory"
        )
    backend.verify_worker_binding()
    return result
