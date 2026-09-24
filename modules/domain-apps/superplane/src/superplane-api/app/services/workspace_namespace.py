"""Resolve the platform-recorded workspace namespace; missing ownership fails closed."""

from __future__ import annotations

import re

from app.models.workspace import Workspace

# Namespaces that belong to the platform or to Kubernetes itself, and so can never
# be a workspace's own namespace. Mirrors `_CORE_NAMESPACES` in the account factory
# (infra/account-factory/account_factory/modes.py); duplicated rather than imported
# because that package is not a dependency of this service, and a namespace landing
# in one of these is severe enough to be worth checking twice.
RESERVED_NAMESPACES = frozenset(
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
        "kro-system",
        "ack-system",
    }
)

# RFC 1123 label: what Kubernetes will accept as a namespace name.
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

class NamespaceResolutionError(Exception):
    """A workspace has no usable namespace — a configuration fault, not a bad request.

    Raised rather than defaulting, so the caller cannot be silently served out of a
    shared namespace. Callers surface this as a 409 requiring ownership reconciliation.
    """


def resolve_workspace_namespace(workspace: Workspace) -> str:
    """Return the namespace `workspace` owns. Never consults caller input.

    Requires a valid namespace recorded by platform provisioning. Legacy rows with
    no namespace require ownership reconciliation before use.

    Raises:
        NamespaceResolutionError: the resolved name is absent, malformed, or a
            platform/Kubernetes namespace. Fails closed — see the module docstring.
    """
    recorded = (workspace.namespace_name or "").strip()
    if not recorded:
        raise NamespaceResolutionError(
            "Workspace has no recorded namespace; reconcile its ownership before deployment"
        )
    namespace = recorded

    # A recorded namespace is not trusted
    # blindly: it reaches the column from provisioning inputs, and a malformed or
    # reserved value there would send tenant workloads into a platform namespace.
    if not _NAMESPACE_RE.match(namespace) or len(namespace) > 63:
        raise NamespaceResolutionError(
            f"Workspace {workspace.id} resolves to namespace {namespace!r}, which is "
            f"not a valid Kubernetes namespace name. Fix the workspace's "
            f"namespace_name; deployments are refused until it is valid."
        )
    if namespace in RESERVED_NAMESPACES:
        raise NamespaceResolutionError(
            f"Workspace {workspace.id} resolves to namespace {namespace!r}, which is a "
            f"platform or Kubernetes namespace and cannot hold tenant workloads. Fix "
            f"the workspace's namespace_name; deployments are refused until it is."
        )

    return namespace
