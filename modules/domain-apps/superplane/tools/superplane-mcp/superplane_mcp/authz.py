"""The single authorization decision for the Superplane tool surface.

R9 acceptance 5 requires authorization to be **identical across every transport**
the tool surface exposes. That is a structural property, not a policy statement:
the only way to guarantee it is to leave exactly one place where the decision is
made, and give the transports no way to reach a handler without passing through it.

So this module owns the decision, and `server.dispatch_tool()` is its only caller.
Transports hand over raw headers and nothing else — they never build a principal,
never consult a capability, and have no argument through which they could pass an
already-authorized one. A new transport is therefore authorized correctly by
construction rather than by its author remembering to add a check.

Contrast with the precedent this follows. The agent-context door
(`door/server.py:446`, `door/mcp_app.py:226`) calls `extract_caller_principal()`
in *each* transport and passes the result into `_dispatch_tool`. That centralizes
the dispatch but leaves principal construction duplicated per transport, so the
two entry points can drift. This module deliberately tightens that: extraction
lives inside the dispatch boundary, because R9 acc. 5 asks for the invariant to
hold through *every* entry point A adds, and a duplicated extraction is exactly
where such an invariant decays.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Capability required by each tool. A tool absent from this map is not callable:
# `authorize()` denies unknown names rather than defaulting to a permissive
# branch, so adding a tool without deciding its capability fails closed.
TOOL_CAPABILITIES: dict[str, str] = {
    "superplane_discover_capacity": "superplane:capacity:read",
    "superplane_allocate_capacity": "superplane:capacity:allocate",
    "superplane_release_allocation": "superplane:capacity:release",
}

# Tools that may only read. Kept as an explicit set rather than derived from a
# name convention: a convention is a comment, whereas this is asserted by
# `tests/test_tool_separation.py` against the handler table.
READ_ONLY_TOOLS: frozenset[str] = frozenset({"superplane_discover_capacity"})

# Tools that spend money or mutate provider state.
SPENDING_OR_MUTATING_TOOLS: frozenset[str] = frozenset(
    {"superplane_allocate_capacity", "superplane_release_allocation"}
)

# Header names carrying the caller identity. These mirror the door's ACL headers
# (`door/acl.py`) so an ADP caller needs no Superplane-specific plumbing.
_HEADER_PRINCIPAL = "x-adp-principal"
_HEADER_TENANT = "x-adp-tenant"
_HEADER_CAPABILITIES = "x-adp-capabilities"
_HEADER_WORKSPACES = "x-adp-workspaces"


@dataclass(frozen=True)
class CallerPrincipal:
    """Who is calling, which tenant they act for, and what they may do."""

    principal: str
    tenant: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    workspaces: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Decision:
    """The outcome of the one authorization decision.

    `reason` is a stable, caller-safe string. It names the missing capability or
    the unbound workspace but never echoes header values back, so a denial cannot
    be used to read another tenant's identifiers out of the surface.
    """

    allowed: bool
    reason: str = ""
    caller: CallerPrincipal | None = None


def _split(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated header into a set, dropping empty fields."""
    if not raw:
        return frozenset()
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def extract_caller_principal(headers: dict[str, str]) -> CallerPrincipal | None:
    """Build the caller from request headers, or None when identity is absent.

    Header lookup is case-insensitive because HTTP header case is not
    significant and the two transports normalize differently — the MCP path
    reads them off an ASGI scope, the REST path off a framework request object.
    Matching case-sensitively here would have made the *same* caller resolve to
    an identity on one transport and to None on the other, which is precisely
    the per-transport divergence R9 acc. 5 forbids.

    Returns None rather than raising: an absent identity is a denial
    (`authorize()` turns it into one), not an error condition to surface.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    principal = (lowered.get(_HEADER_PRINCIPAL) or "").strip()
    tenant = (lowered.get(_HEADER_TENANT) or "").strip()
    if not principal or not tenant:
        return None
    return CallerPrincipal(
        principal=principal,
        tenant=tenant,
        capabilities=_split(lowered.get(_HEADER_CAPABILITIES)),
        workspaces=_split(lowered.get(_HEADER_WORKSPACES)),
    )


def authorize(tool_name: str, arguments: dict, headers: dict[str, str]) -> Decision:
    """Make the one authorization decision for a tool call.

    Fail-closed at every step: unknown tool, missing identity, missing
    capability and unbound workspace all deny. There is no branch that allows a
    call because something was absent.
    """
    required = TOOL_CAPABILITIES.get(tool_name)
    if required is None:
        # Unknown tool. Denied before the caller is even resolved, so an
        # unregistered name cannot be probed for identity-dependent behaviour.
        return Decision(allowed=False, reason=f"unknown tool: {tool_name}")

    caller = extract_caller_principal(headers)
    if caller is None:
        return Decision(allowed=False, reason="no caller identity")

    if required not in caller.capabilities:
        return Decision(
            allowed=False,
            reason=f"missing capability: {required}",
            caller=caller,
        )

    # Tenant isolation. A workspace argument must be one the caller is bound to.
    # Checked here rather than in the handlers so that every tool — including any
    # added later — is scoped by the same rule. A handler-side check would be
    # per-tool and would silently omit whichever tool forgot it.
    workspace = arguments.get("workspace")
    if workspace is not None:
        if not isinstance(workspace, str) or not workspace.strip():
            return Decision(
                allowed=False,
                reason="workspace must be a non-empty string",
                caller=caller,
            )
        if workspace not in caller.workspaces:
            # Deliberately does not distinguish "exists but not yours" from
            # "does not exist": that difference is itself information about
            # another tenant's estate.
            return Decision(
                allowed=False,
                reason="workspace not bound to caller",
                caller=caller,
            )

    return Decision(allowed=True, caller=caller)
