"""Workspace scoping — authenticated is not sufficient, in either direction.

Issue #5043 (U8), EPIC #4910. Design §2 line 113; §7 line 454.

## Why this is a separate module from `auth.py`

Because they are separate failures and conflating them is how the second one
ships. Authentication answers "is this a real submitter?"; scoping answers "may
this submitter speak about *that* workspace?" A system that only does the first
has closed the anonymous-forgery hole and left the cross-tenant one wide open —
any legitimate monitor could then rewrite any workspace's fleet state, which for
a multi-tenant surface is barely an improvement.

Keeping them separate also means neither can be satisfied by the other's evidence.
`authorize_submit` takes an already-authenticated `Submitter` and still refuses if
the grant does not cover the subject.

## Both directions, and why read is not the lesser half

Submission and read are authorized separately and both are workspace-bound. Read
scoping is easy to treat as cosmetic — nothing is written, after all — but a fleet
observation is an operational map of a tenant's estate: which clusters exist, which
are unreachable, what they cost. Reading another workspace's observations is a
disclosure with no write involved, so it gets its own function and its own negative
test rather than riding along on the submit check.

## Fail-closed on an empty grant

A submitter with no workspaces is authorized for nothing. Stated explicitly because
the natural set-membership expression (`workspace in grant`) already behaves this
way, but the *reason* is worth pinning: an empty grant most often means a resolver
could not determine the grant, and treating "unknown" as "all" is how a
misconfigured token becomes a tenant boundary failure.

## Refusals do not distinguish absent from forbidden

`ScopeDecision.reason` is the same whether the workspace does not exist or exists
and belongs to somebody else. That difference is itself information about another
tenant's estate, and an endpoint that reveals it is an enumeration oracle. This
follows the precedent already set by the MCP tool surface's authorization module
(`tools/superplane-mcp/superplane_mcp/authz.py`), which makes the same choice for
the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass

from .auth import Submitter
from .observation import Observation

# One reason string for every scoping refusal. A single constant rather than
# per-call-site strings so no future branch can accidentally become more
# informative than the others and turn into the enumeration oracle this avoids.
_OUT_OF_SCOPE = "workspace not in submitter scope"


@dataclass(frozen=True)
class ScopeDecision:
    """Outcome of a scoping check. `allowed=False` carries no tenant detail."""

    allowed: bool
    reason: str = ""


def _covers(submitter: Submitter, workspace: str) -> bool:
    """True when the submitter's grant covers `workspace`.

    A blank workspace is never covered, even by a grant that somehow contains a
    blank string, so a payload with an empty workspace cannot slip through on a
    malformed grant.
    """
    candidate = workspace or ""
    if not candidate:
        return False
    return candidate in submitter.workspaces


def authorize_submit(
    submitter: Submitter,
    observation: Observation,
    *,
    cluster_workspace: str | None = None,
) -> ScopeDecision:
    """Authorize an authenticated submitter to write this observation.

    Takes the *authenticated* submitter and the observation as separate arguments
    on purpose: there is no way to call this such that the payload supplies its
    own authority. The subject's workspace is compared against the grant, and a
    disagreement is a refusal — never a correction of the payload to match the
    grant, which would silently accept a cross-workspace write as a same-workspace
    one.
    """
    # U15 resolves ownership from trusted storage using cluster_id; the body is
    # a claim, never an authoritative ownership lookup. Missing ownership fails.
    if cluster_workspace is None or cluster_workspace != observation.subject.workspace:
        return ScopeDecision(allowed=False, reason=_OUT_OF_SCOPE)
    if not _covers(submitter, observation.subject.workspace):
        return ScopeDecision(allowed=False, reason=_OUT_OF_SCOPE)

    if observation.budget is not None and not _covers(
        submitter, observation.budget.workspace
    ):
        # `Observation` already refuses a budget whose workspace disagrees with the
        # subject, so this is unreachable through that constructor. Kept because
        # this function's contract is "no workspace in this payload escapes the
        # grant", and a future payload variant carrying a second workspace should
        # fail here rather than depend on a validation rule in another module.
        return ScopeDecision(allowed=False, reason=_OUT_OF_SCOPE)

    return ScopeDecision(allowed=True)


def authorize_read(submitter: Submitter, workspace: str) -> ScopeDecision:
    """Authorize an authenticated submitter to read a workspace's observations.

    Separate from `authorize_submit` so that read scoping cannot be satisfied by a
    write authorization or skipped because the caller was already authenticated
    for something. Same refusal string, for the same non-enumeration reason.
    """
    if not _covers(submitter, workspace):
        return ScopeDecision(allowed=False, reason=_OUT_OF_SCOPE)
    return ScopeDecision(allowed=True)


def visible_workspaces(
    submitter: Submitter, requested: tuple[str, ...]
) -> tuple[str, ...]:
    """Narrow a requested workspace list to those the submitter may read.

    Provided so a receiver implementing a list endpoint filters rather than
    refusing the whole request, while still never returning a workspace outside
    the grant. Order follows `requested` so a caller's paging stays stable.

    Note this *silently drops* out-of-scope entries rather than reporting them —
    which is the correct behaviour precisely because reporting them would
    confirm they exist.
    """
    return tuple(w for w in requested if _covers(submitter, w))
