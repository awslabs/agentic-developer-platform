"""Server-side resolution of the Kubernetes namespace a workspace owns.

Issue #5671 (A15). The mutating deployment paths used to take the target namespace
from the request body, and the namespace the platform had assigned to the workspace
(``Workspace.namespace_name``) was recorded but never read. On any cluster hosting
more than one workspace — the platform's own cluster included — that let a caller
name a neighbour's namespace and overwrite or delete a workload that was not theirs.

The namespace is therefore resolved HERE, from the workspace row, and never from
caller input. There is deliberately no parameter through which a request can
influence the result.

WHY AN UNRESOLVABLE NAMESPACE IS A REFUSAL, NOT A FALLBACK
----------------------------------------------------------
The obvious fallback for a workspace row with no recorded namespace is ``default``.
That is precisely the isolation defect this module exists to close: ``default`` is
shared by every workspace on the cluster, so falling back to it silently places two
tenants in one compartment and makes every ownership check meaningless. The account
factory already refuses ``default`` as a workspace namespace for the same reason
(``_CORE_NAMESPACES`` in ``infra/account-factory/account_factory/modes.py``).

So a workspace whose namespace cannot be resolved raises, and the message is aimed
at an operator, because that state is a provisioning bug to be fixed rather than a
request to be serviced.

THE DERIVED NAME, AND WHY IT IS DERIVED AT ALL
----------------------------------------------
Rows written before bootstrap began recording ``namespace_name``, or written by
scripts outside that path, can legitimately have it empty. Refusing those outright
would take away the ability to deploy from tenants who did nothing wrong, so the
name is derived deterministically from the workspace's own identity instead — the
same shape the account factory uses, where a workspace's identity *is* its
namespace. Deterministic matters: the same workspace must resolve to the same
namespace on every call, or a delete would look in a different place than the
create did.

This is a compatibility path, not the design. ``018_backfill_workspace_namespace``
writes the derived value into the column so the derivation stops being reached, and
newly bootstrapped workspaces always arrive with a namespace already set.
"""

from __future__ import annotations

import re
import uuid

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

# Prefix for a derived namespace. Keeps a derived name from ever colliding with a
# platform namespace and makes its origin legible on the cluster.
_DERIVED_PREFIX = "ws-"


class NamespaceResolutionError(Exception):
    """A workspace has no usable namespace — a configuration fault, not a bad request.

    Raised rather than defaulting, so the caller cannot be silently served out of a
    shared namespace. Callers surface this as an operator-facing 500: nothing the
    requester can change would fix it.
    """


def derive_namespace_name(workspace_id: uuid.UUID | str) -> str:
    """Derive a workspace's namespace from its identity, deterministically.

    Used only when the workspace row has no recorded namespace. Same input always
    gives the same output, so a later delete resolves to the namespace the create
    used.
    """
    return f"{_DERIVED_PREFIX}{workspace_id}"


def resolve_workspace_namespace(workspace: Workspace) -> str:
    """Return the namespace `workspace` owns. Never consults caller input.

    Prefers the namespace the platform recorded; derives one deterministically when
    the row predates that column being populated.

    Raises:
        NamespaceResolutionError: the resolved name is absent, malformed, or a
            platform/Kubernetes namespace. Fails closed — see the module docstring.
    """
    recorded = (workspace.namespace_name or "").strip()
    namespace = recorded or derive_namespace_name(workspace.id)

    # Validate whichever value we ended up with. A recorded namespace is not trusted
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
