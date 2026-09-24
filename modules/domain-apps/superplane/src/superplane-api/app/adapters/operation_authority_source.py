"""Principal resolution and approval, derived from the domain's own grant rows.

Issue #5535 (Superplane W6), EPIC #4910.

`OperationFacadeService` requires a `PrincipalResolver` and an `ApprovalSource`.
Neither can come from `harness_jobs`: the resolver's whole job is to produce a
tenant from *this* application's verified authentication context, and the approval
source reads *this* application's grants. A shared package supplying either would
be deciding who the domain's principals are.

## The resolver's contract, and the trap in it

    async def resolve(self, *, org_id: str, workspace_id: str, permission: str)
        -> ResolvedPrincipal | None

The arguments are **an assertion to check, not a source of authority**
(`harness_jobs/facade.py:206`). And `report_progress` calls this with
`org_id=""`, `workspace_id=""` — no asserted tenant at all — so an implementation
must derive the tenant itself and must tolerate empty strings rather than treating
them as a tenant named "".

The authority therefore comes from a caller this process authenticated, carried in
a contextvar rather than passed as an argument. A contextvar and not an attribute
on the adapter: the adapter is composed once per process and serves concurrent
requests, so an attribute would be a cross-request identity leak of the worst
kind — request B resolving as request A's principal, non-deterministically under
load.

**`permission` is required, not merely requested.** Passing
`permission="workspace:provision"` asks for a principal; it does not establish
that the principal came back holding it. This resolver returns `None` unless the
grant actually carries the permission, and the harness independently re-checks
`may_provision` — both, because each side's check guards a different mistake.

## Why approval is refused rather than auto-granted

`ApprovalSource.approval_for` may return a context whose `record is None`, and the
gate refuses on it: *absence is not permission*. This implementation returns
`record=None` in every case where the domain holds no explicit approval, which
today is every case — the domain has no approval table (the only approval-shaped
rows are `research_proposals`, which have no envelope, no expiry, no binding
digest and no distinct-approver rule, so they cannot back an `ApprovalRecord`).

That makes operation admission refuse for want of approval, which is the honest
state and is deliberately **not** patched over by synthesizing a record. A
synthesized `ApprovalRecord` naming the requester as its own approver would defeat
`requires_distinct_approver` and turn the approval gate into a formality, while
reporting the port as composed. Refusing names the missing thing; fabricating hides
it.

`approver_statuses` is still populated from real org and workspace grants, because
that is the *current* authority of each approver and the gate needs it per request
rather than cached — and because `ApproverStatus` distinguishes `revoked=True` from
absence. Modelling a revoked approver as simply missing is the specific mistake
`approval.py:254-257` calls out: it reads as "never had authority" when the truth
is "had it and lost it", and only the second one means a decision already made
under that authority must be re-examined.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.models.organization_grant import (
    ORGANIZATION_ADMINISTER,
    OrganizationGrantRecord,
)
from app.models.workspace_grant import WorkspaceGrantRecord

logger = logging.getLogger(__name__)

# `workspace:administer`, the permission an approver must hold
# (`harness_jobs/approval.py:104`). Deliberately NOT `workspace:provision`:
# requesting an operation and approving one are different authorities, which is
# what makes four-eyes meaningful.
APPROVAL_PERMISSION = "workspace:administer"

# `workspace:provision` (`harness_jobs/identity.py:50`). Spelled here so this
# module does not import the harness at module scope — see `composition.py` on why
# the harness import must stay inside functions.
PROVISION_PERMISSION = "workspace:provision"


@dataclass(frozen=True)
class ActingPrincipal:
    """The authenticated caller an operation is being admitted for.

    Built by the request layer from an already-verified token and an already-bound
    organization, never from a request body. Carries the *domain* org UUID, because
    `app/organization_binding.py:bind_caller` has already exchanged the ADP claim
    for it by the time a route runs, and the grant tables key on the domain id.
    """

    subject: str
    org_id: str
    workspace_id: str
    account_type: str = "human"


# Request-scoped, so concurrent requests cannot observe each other's principal.
# Default `None` means "no authenticated context", which resolves to `None` and
# therefore to a refusal — the correct answer for the boot-time capability probe,
# which runs with no request at all.
_acting: contextvars.ContextVar[ActingPrincipal | None] = contextvars.ContextVar(
    "superplane_acting_principal", default=None
)


def acting_principal() -> ActingPrincipal | None:
    return _acting.get()


def set_acting_principal(principal: ActingPrincipal | None) -> Any:
    """Bind the acting principal for the current context; returns the reset token.

    Returns the token rather than being a context manager because callers are
    FastAPI dependencies and middleware, which reset in their own `finally`.
    """
    return _acting.set(principal)


def reset_acting_principal(token: Any) -> None:
    _acting.reset(token)


class GrantBackedAuthority:
    """Resolves principals and approvals from `workspace_grants` / `organization_grants`.

    Holds a session factory rather than a session: the facade calls this from
    admission and from recovery paths that have no request-scoped session, and a
    captured session would be closed by the time the second caller arrived.
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # PrincipalResolver
    # ------------------------------------------------------------------

    async def resolve(
        self, *, org_id: str, workspace_id: str, permission: str
    ) -> Any | None:
        """The verified caller as a `ResolvedPrincipal`, or `None` to refuse.

        `None` rather than a raise for every "not entitled" outcome: the harness
        turns `None` into `OperationRefused` and turns a *raise* into
        `OperationUnavailable`, so raising here would report a caller's lack of
        authority as an outage and invite a retry that can never succeed.

        The asserted `org_id` / `workspace_id` are checked against what this
        process authenticated whenever they are non-empty, and ignored when empty
        because `report_progress` legitimately passes neither. An assertion that
        disagrees with the authenticated context is a refusal, not a correction.
        """
        from harness_jobs.identity import ResolvedPrincipal

        caller = _acting.get()
        if caller is None:
            return None

        if org_id and org_id != caller.org_id:
            # Never "corrected" to the real tenant. A caller naming another
            # tenant is refused, because a path that reads a caller-supplied
            # tenant and then overrides it is one reordering away from honouring
            # it.
            return None
        workspace = workspace_id or caller.workspace_id
        if workspace_id and caller.workspace_id and workspace_id != caller.workspace_id:
            return None
        if not workspace:
            # No workspace in the authenticated context and none asserted. There
            # is nothing to scope a provisioning permission to.
            return None

        permissions = await self._workspace_permissions(
            org_id=caller.org_id,
            workspace_id=workspace,
            principal=caller.subject,
            account_type=caller.account_type,
        )
        if permissions is None:
            return None

        required = permission or PROVISION_PERMISSION
        if required not in permissions:
            # The requested permission is genuinely absent. Refused here AND
            # re-checked by the harness's `may_provision`; two checks because a
            # resolver that returned a correctly-scoped principal with an empty
            # permission set was once accepted, and the read then returned that
            # tenant's operation state.
            return None

        return ResolvedPrincipal(
            org_id=caller.org_id,
            workspace_id=workspace,
            subject=caller.subject,
            permissions=frozenset(permissions),
        )

    # ------------------------------------------------------------------
    # ApprovalSource
    # ------------------------------------------------------------------

    async def approval_for(self, *, principal: Any, request: Any) -> Any:
        """The approval context for one request.

        Returns `record=None` because the domain holds no approval records — see
        the module docstring on why that refusal is the honest answer and why
        synthesizing a record would defeat the gate it is meant to satisfy.

        `requested_envelope` is derived from the request's own parameters, clamped
        to the workspace's configured ceilings. It is not read from a caller-
        supplied field that could name a larger envelope than the workspace allows:
        the parameters are already digest-bound into the approval binding, so a
        changed envelope changes the plan digest and cannot be replayed against an
        existing approval.
        """
        from harness_jobs.approval import ApprovalContext

        envelope = _requested_envelope(request)
        statuses = await self._approver_statuses(principal)
        return ApprovalContext(
            record=None,
            requested_envelope=envelope,
            approver_statuses=statuses,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _workspace_permissions(
        self,
        *,
        org_id: str,
        workspace_id: str,
        principal: str,
        account_type: str,
    ) -> set[str] | None:
        """The caller's live permissions on one workspace, or `None` if ungranted.

        Mirrors `app/auth.py:load_workspace_authorization` — the same tenant
        filter, the same `revoked_at IS NULL`, and `expand_permissions` so a stored
        `administer` yields its implied closure rather than only itself. Reusing
        the same shape matters because a second, subtly different authorization
        query is a second answer to the same question.
        """
        from superplane_auth.policy import Permission, expand_permissions

        try:
            async with self._session_factory() as session:
                record = (
                    await session.execute(
                        select(WorkspaceGrantRecord).where(
                            WorkspaceGrantRecord.workspace_id == _as_uuid(workspace_id),
                            WorkspaceGrantRecord.org_id == _as_uuid(org_id),
                            WorkspaceGrantRecord.principal == principal,
                            WorkspaceGrantRecord.principal_type == account_type,
                            WorkspaceGrantRecord.revoked_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
        except _IdentityNotUuid:
            # An identifier the grant tables cannot be keyed on. Refused rather
            # than queried with a coerced value.
            return None
        except Exception:
            logger.warning(
                "the workspace grant could not be read while resolving a principal",
                exc_info=False,
            )
            return None

        if record is None:
            return None

        known: set[Permission] = set()
        for value in record.permission_values():
            try:
                known.add(Permission(value))
            except ValueError:
                logger.warning(
                    "workspace grant carries an unrecognized permission; ignoring it"
                )
        return {str(item) for item in expand_permissions(known)}

    async def _approver_statuses(self, principal: Any) -> dict[str, Any]:
        """The current authority of each potential approver in this tenant.

        Read per request rather than stored on a record, because the question is
        who may approve *now*. A revoked approver is reported with `revoked=True`
        and `is_member=True`, never as absent: absence reads as "never had
        authority", and only "had it and lost it" means a decision already taken
        under that authority must be re-examined.
        """
        from harness_jobs.approval import ApproverStatus

        org_id = getattr(principal, "org_id", "") or ""
        workspace_id = getattr(principal, "workspace_id", "") or ""
        statuses: dict[str, Any] = {}

        try:
            async with self._session_factory() as session:
                workspace_grants = (
                    (
                        await session.execute(
                            select(WorkspaceGrantRecord).where(
                                WorkspaceGrantRecord.workspace_id
                                == _as_uuid(workspace_id),
                                WorkspaceGrantRecord.org_id == _as_uuid(org_id),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                org_grants = (
                    (
                        await session.execute(
                            select(OrganizationGrantRecord).where(
                                OrganizationGrantRecord.org_id == _as_uuid(org_id)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        except _IdentityNotUuid:
            return {}
        except Exception:
            logger.warning(
                "approver authority could not be read; reporting no approvers",
                exc_info=False,
            )
            return {}

        from superplane_auth.policy import Permission, expand_permissions

        for record in workspace_grants:
            known: set[Permission] = set()
            for value in record.permission_values():
                try:
                    known.add(Permission(value))
                except ValueError:
                    continue
            statuses[record.principal] = ApproverStatus(
                subject=record.principal,
                is_member=True,
                permissions=frozenset(str(item) for item in expand_permissions(known)),
                revoked=record.revoked_at is not None,
            )

        for record in org_grants:
            # An organization administrator holds workspace administration by
            # implication. Recorded only when the workspace grant did not already
            # answer for this subject, so a workspace-level revocation is not
            # overwritten by an org-level grant.
            if record.principal in statuses:
                continue
            permitted = record.permissions or ""
            if ORGANIZATION_ADMINISTER not in permitted.split():
                continue
            statuses[record.principal] = ApproverStatus(
                subject=record.principal,
                is_member=True,
                permissions=frozenset({APPROVAL_PERMISSION}),
                revoked=record.revoked_at is not None,
            )

        return statuses


class _IdentityNotUuid(Exception):
    """An identifier that cannot key the grant tables."""


def _as_uuid(value: str) -> Any:
    import uuid

    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise _IdentityNotUuid(value) from None


# Conservative per-attempt ceilings, used when a request names none. Chosen to be
# small: an unstated envelope must not be a large one, because the envelope is
# what a later approval is compared against. A caller needing more states more.
DEFAULT_MAX_RESOURCE_UNITS = 1
DEFAULT_MAX_RUNTIME_SECONDS = 3600
DEFAULT_MAX_COST_MICROS = 0


def _requested_envelope(request: Any) -> Any:
    """The spend envelope a request asks for, read from its bound parameters.

    Read from `request.parameters`, which are digest-bound into the approval
    binding, so a changed envelope changes the plan digest. A default of zero cost
    is deliberate: an operation that has not stated a cost has not been approved to
    spend, and defaulting to a permissive number would let an unstated envelope
    authorize real money.
    """
    from harness_jobs.approval import SpendEnvelope

    parameters = getattr(request, "parameters", None) or {}
    return SpendEnvelope(
        max_resource_units=_positive_int(
            parameters.get("max_resource_units"), DEFAULT_MAX_RESOURCE_UNITS
        ),
        max_runtime_seconds=_positive_int(
            parameters.get("max_runtime_seconds"), DEFAULT_MAX_RUNTIME_SECONDS
        ),
        max_cost_micros=_positive_int(
            parameters.get("max_cost_micros"), DEFAULT_MAX_COST_MICROS
        ),
    )


def _positive_int(value: object, default: int) -> int:
    """A non-negative integer from a string parameter, or the default.

    Falls back to the default rather than raising on a malformed value, and the
    default is the conservative direction. A malformed envelope field must not
    become a large envelope.
    """
    if value is None:
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default
