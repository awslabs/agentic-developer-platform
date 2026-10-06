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

## Which grant answers, and the ordering defect that made this necessary

Authority is read from `workspace_grants` first. `organization_grants` are
consulted only when the workspace row **does not exist** — not merely when no
grant row was found. That distinction is the whole correctness argument, and two
earlier revisions of this module got it wrong in two different ways.

The fallback is what makes the zero-workspace ordering implementable:
`workspace_grants.workspace_id` is a foreign key to `workspaces.id`
(`app/models/workspace_grant.py:75`), so for a workspace that does not exist a
grant is not absent but *unrepresentable* — and `POST /workspaces` must admit its
operation before it writes the row, or the operation did not gate anything.
Measured against real PostgreSQL before the fallback existed, an organization
administrator resolved to `None`: no organization could create its first
workspace, ever.

Bounded that way it widens nothing. `POST /workspaces` is the only
`(Scope.ORGANIZATION, Permission.PROVISION)` route in the endpoint inventory, and
`app/auth.py:authorize_organization_operation` already admits it on that same
`organization:administer` row — so for the creating case the fallback turns two
disagreeing answers into one. Unbounded it widened a great deal: every other
workspace route is `Scope.WORKSPACE`, `authorize_workspace_operation` has no
organization fallback at all, and `_IMPLIED[ADMINISTER]` closes over `spend`. An
unconditional fallback therefore handed org admins spend authority on every
*existing* workspace through a port whose own HTTP guard refuses it.

Workspace-grant-first is load-bearing in the other direction too, and a revoked
grant is a terminal refusal rather than a missing one. The row is read WITHOUT a
`revoked_at IS NULL` filter for that reason: filtering turns "revoked" into "no
row", and "no row" is what opens the fallback, so the filter quietly let an
organization grant reinstate a workspace-level revocation.

## Unreadable is not unentitled

Every grant read here distinguishes three outcomes, not two: a row, no row, and
*could not tell*. `None` is this resolver's spelling of refusal, so returning it
for a failed read would report an unreachable database as a denied caller — a 403
for a 503, blaming a tenant for an outage and pointing an operator at the grant
tables. The unreadable case raises `_AuthorityUnreadable`, which the harness
converts to `OperationUnavailable`, because `refused`, `unavailable` and
`unknown` are three different answers and only the first one is about the caller.

## Approval source

Explicit decisions live in `operation_approvals`, bound to the authenticated
requester and exact harness request digest. Current human approver grants are
re-read at admission. A missing decision refuses admission; an unreadable store
raises unavailable. No approval is synthesized from a role or requester flag.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select

from app.current_identity import (
    CurrentIdentityReader,
    IdentityDenied,
    IdentityUnavailable,
    identity_checks_enabled,
    require_current_identity,
)
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

# `superplane_auth.policy.Permission.ADMINISTER`, the workspace permission an
# organization administrator holds by implication. The same string as
# `APPROVAL_PERMISSION` above and deliberately a separate constant: that one is
# "what an approver must hold" and this one is "what an org admin administers",
# and collapsing them would make a future divergence in either rule silently
# change the other. `_IMPLIED[ADMINISTER]` closes over PROVISION, which is what
# makes this sufficient to provision without being written as PROVISION.
WORKSPACE_ADMINISTER = "workspace:administer"


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
    adp_org_id: str | None = None
    membership_id: str | None = None
    identity_reader: CurrentIdentityReader | None = None


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

        The converse holds and is why `_AuthorityUnreadable` propagates: a grant
        table that could not be read has established *nothing* about entitlement,
        and returning `None` for it would answer 403 where the truth is 503. The
        two directions are the same rule read from both ends — a refusal must be an
        answer, and an answer must not be manufactured from an outage.

        The asserted `org_id` / `workspace_id` are checked against what this
        process authenticated whenever they are non-empty, and ignored when empty
        because `report_progress` legitimately passes neither. An assertion that
        disagrees with the authenticated context is a refusal, not a correction.
        """
        from harness_jobs.identity import ResolvedPrincipal

        caller = _acting.get()
        if caller is None:
            return None

        if identity_checks_enabled():
            from app.models.organization import Organization

            # Missing optional context must not turn a mapped tenant into a
            # legacy tenant. Resolve the binding from the authoritative store.
            organization = await self._read(
                "current identity organization",
                lambda session: session.execute(
                    select(Organization).where(Organization.id == _as_uuid(caller.org_id))
                ),
            )
            if organization is _UNREADABLE:
                raise _AuthorityUnreadable("current identity organization")
            if organization is None:
                return None
            if organization.adp_org_id:
                if (
                    caller.adp_org_id != organization.adp_org_id
                    or not caller.membership_id
                ):
                    return None
                try:
                    await require_current_identity(
                        caller.identity_reader,
                        subject=caller.subject,
                        principal_type=caller.account_type,
                        adp_org_id=organization.adp_org_id,
                        membership_id=caller.membership_id,
                    )
                except IdentityDenied:
                    return None
                except IdentityUnavailable:
                    raise _AuthorityUnreadable("current identity") from None

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
        """Resolve a persisted decision and freshly recheck its approvers."""
        from harness_jobs.approval import ApprovalBinding
        from harness_jobs.facade import ApprovalContext

        from app.models.operation_approval import OperationApproval
        from app.services.operation_approvals import record_from_row

        binding = ApprovalBinding.for_request(principal, request)
        try:
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(OperationApproval).where(
                            OperationApproval.org_id == binding.org_id,
                            OperationApproval.workspace_id == binding.workspace_id,
                            OperationApproval.requester == binding.requester,
                            OperationApproval.plan_digest == binding.plan_digest,
                        )
                    )
                ).scalar_one_or_none()
                record = None if row is None else record_from_row(row)
        except Exception:
            raise _AuthorityUnreadable("operation approval") from None
        return ApprovalContext(
            record=record,
            requested_envelope=_requested_envelope(request),
            approver_statuses=await self._approver_statuses(
                principal, organization_scope=bool(row and row.organization_scope)
            ),
        )

    async def budget_limits_for(self, *, org_id: str, workspace_id: str) -> Any:
        """Read current tenant-owned caps; uncertainty must retain budget.

        Reserve the whole approved ceiling against both time-window limits. This
        is conservative until actual metered settlement can attribute charges to
        hours/days: a runtime average cannot prove a peak-spend bound.
        """
        from decimal import Decimal

        from app.adapters.operation_budget_ledger import WorkspaceBudgetLimits
        from app.models.workspace import Workspace

        try:
            async with self._session_factory() as session:
                workspace = (
                    await session.execute(
                        select(Workspace).where(Workspace.id == _as_uuid(workspace_id))
                    )
                ).scalar_one_or_none()
                if workspace is None:
                    # The first-workspace operation precedes its workspace row.
                    return None
                if str(workspace.org_id) != org_id:
                    raise _AuthorityUnreadable("workspace budget tenant mismatch")
                caps = [
                    int(Decimal(value) * 1_000_000)
                    for value in (
                        workspace.budget_max_daily_usd,
                        workspace.budget_max_hourly_usd,
                    )
                    if value is not None
                ]
                units = workspace.budget_max_gpus
                if any(value < 0 for value in caps) or (
                    units is not None and units < 0
                ):
                    raise _AuthorityUnreadable("invalid workspace budget")
                return WorkspaceBudgetLimits(
                    max_cost_micros=min(caps) if caps else None,
                    max_resource_units=units,
                )
        except _AuthorityUnreadable:
            raise
        except Exception:
            raise _AuthorityUnreadable("workspace budget") from None

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

        ## Why the organization grant is consulted, and why that is not a widening

        A workspace grant cannot exist for a workspace that does not exist —
        `workspace_grants.workspace_id` is a foreign key to `workspaces.id`, so such
        a grant is unrepresentable, not merely missing. And `POST /workspaces` admits
        its operation *before* the row is written (it has to: an operation opened
        after the row is an operation that did not gate it). So for the first
        workspace in an organization's life the workspace-grant query below returns
        nothing for a reason that says nothing about the principal. Measured against
        real PostgreSQL before this fallback existed: an organization administrator
        with `organization:administer` and no workspace grant resolved to `None`,
        which means no organization could ever create its first workspace. That is
        the zero-workspace ordering requirement, failing closed in the unusable
        direction.

        For that case, reading the organization grant grants no authority that was
        not already granted. `app/auth.py:authorize_organization_operation`
        **already admits this exact request** on this exact row: `POST /workspaces`
        is `(Scope.ORGANIZATION, Permission.PROVISION)` in the endpoint inventory,
        and `_IMPLIED[ADMINISTER]` closes over `PROVISION`. Before the fallback the
        guard said yes and this resolver said no for the same caller on the same
        request — two answers to one question, with the second unreachable by any
        configuration. This makes them one answer, read from one row.

        ## The two ways the fallback is bounded, and why each bound is necessary

        **It applies only when the workspace does not exist.** `POST /workspaces` is
        the only organization-scoped provisioning route; every other workspace route
        is `Scope.WORKSPACE`, and `authorize_workspace_operation` offers no
        organization fallback. Since `_IMPLIED[ADMINISTER]` closes over `SPEND`, an
        unconditional fallback gave organization administrators spend authority on
        every existing workspace via this port while the HTTP guard for the same
        workspace refused them. `_workspace_exists` is therefore consulted before
        the organization is.

        **A revoked workspace grant is terminal.** The query deliberately omits
        `revoked_at IS NULL` and judges the column in Python, because a revocation
        is the most specific answer available about this principal on this workspace
        and so must end the search rather than start a wider one.

        Measured honestly, this second bound is **defense in depth rather than the
        load-bearing guard**, and the comment here used to claim otherwise. A
        revoked grant can only exist for a workspace that exists (foreign key), and
        an existing workspace already closes the fallback by the bound above — so
        mutation-testing the `revoked_at` filter back in, on its own, changed no
        reachable outcome. It is kept for three reasons that are not "it currently
        matters": the two guards fail independently, so relaxing the existence bound
        later cannot silently reinstate revoked authority; a revoked grant produces
        an explicit operator log where a filtered-away row produces silence; and
        `revoked` and `absent` genuinely are different answers, which is the same
        distinction `_read` preserves between `absent` and `unreadable`.
        """
        # Selected WITHOUT a `revoked_at IS NULL` filter, then judged in Python.
        # The filter is what made case 5 wrong: it turns a revoked grant into no
        # row, and no row is what triggers the organization fallback below — so a
        # workspace-level revocation was silently reinstated by the org grant it is
        # supposed to override. `app/auth.py:539` is the precedent and the shape is
        # copied from it deliberately: it too selects the grant unfiltered and
        # refuses on a present-but-revoked row rather than falling through.
        #
        # `load_workspace_authorization` DOES collapse revoked into absent, and that
        # is not a contradiction — nothing follows it, so the two cases are
        # indistinguishable to its caller. Collapsing is safe exactly when no
        # fallback follows. Here one does.
        workspace_grant = await self._read(
            "workspace grant",
            lambda session: session.execute(
                select(WorkspaceGrantRecord).where(
                    WorkspaceGrantRecord.workspace_id == _as_uuid(workspace_id),
                    WorkspaceGrantRecord.org_id == _as_uuid(org_id),
                    WorkspaceGrantRecord.principal == principal,
                    WorkspaceGrantRecord.principal_type == account_type,
                )
            ),
        )
        if workspace_grant is _UNREADABLE:
            raise _AuthorityUnreadable("workspace grant")
        if workspace_grant is not None:
            if workspace_grant.revoked_at is not None:
                # Terminal. A revocation is an answer about this principal on this
                # workspace, and it is the most specific answer available.
                logger.warning(
                    "a revoked workspace grant refused an operation; the "
                    "organization grant is deliberately not consulted"
                )
                return None
            return _expand(workspace_grant.permission_values(), "workspace grant")

        # No workspace grant row at all. The organization is consulted ONLY when the
        # workspace itself does not exist, which is the single case the fallback was
        # added for. See the docstring: for an existing workspace, the shipped
        # `authorize_workspace_operation` has no organization fallback, so honouring
        # one here would grant org admins the full `administer` closure — `spend`
        # included — on every existing workspace, through a port whose HTTP guard
        # refuses exactly that. The fallback's justification was only ever that the
        # row cannot exist yet.
        if await self._workspace_exists(workspace_id):
            return None

        organization_grant = await self._read(
            "organization grant",
            lambda session: session.execute(
                select(OrganizationGrantRecord).where(
                    OrganizationGrantRecord.org_id == _as_uuid(org_id),
                    OrganizationGrantRecord.principal == principal,
                    OrganizationGrantRecord.principal_type == account_type,
                    OrganizationGrantRecord.revoked_at.is_(None),
                )
            ),
        )
        if organization_grant is _UNREADABLE:
            raise _AuthorityUnreadable("organization grant")
        if organization_grant is None:
            return None

        # Only `organization:administer` confers workspace authority.
        # `organization:read` deliberately does not: `authorize_organization_operation`
        # admits it for `Permission.READ` alone, and provisioning is not a read.
        if ORGANIZATION_ADMINISTER not in organization_grant.permission_values():
            return None
        # The organization administrator's workspace authority, stated as the
        # workspace permission it implies rather than as the organization string.
        # `expand_permissions` then closes it over `_IMPLIED`, so this yields
        # exactly what a stored workspace `administer` grant would — the same
        # closure, from the row the guard already honoured.
        #
        # `WORKSPACE_ADMINISTER` rather than `APPROVAL_PERMISSION`, which holds the
        # same string: the two are equal today and mean different things, and
        # writing the approval constant here would say "an org admin may approve"
        # when what is meant is "an org admin administers the workspace". They must
        # be free to diverge — see `_approver_statuses`, which uses the approval
        # spelling for the approval question.
        return _expand((WORKSPACE_ADMINISTER,), "organization grant")

    async def _workspace_exists(self, workspace_id: str) -> bool:
        """Whether the workspace row exists, gating the organization fallback.

        The fallback exists because `workspace_grants.workspace_id` is a foreign key
        to `workspaces.id` (`app/models/workspace_grant.py:75`), so a grant for a
        workspace that does not exist is not merely absent — it is
        *unrepresentable*. That makes non-existence the precise and only condition
        under which the absence of a workspace grant carries no information about
        the principal's authority.

        Fails CLOSED in both non-answers, and the two directions differ:

        - unreadable raises `_AuthorityUnreadable`, so the caller gets 503. Returning
          `False` would open the fallback on an outage, and returning `True` would
          report a refusal for one.
        - an id the table cannot be keyed on returns `True`, closing the fallback.
          Not a claim that the workspace exists; a claim that *this* path may not
          widen authority for an identifier it could not even look up.
        """
        from app.models.workspace import Workspace

        row = await self._read(
            "workspace",
            lambda session: session.execute(
                select(Workspace.id).where(Workspace.id == _as_uuid(workspace_id))
            ),
        )
        if row is _UNREADABLE:
            raise _AuthorityUnreadable("workspace")
        # `_read` maps `_IdentityNotUuid` to `None`, which here would mean "absent"
        # and open the fallback. Distinguish it before trusting the `None`.
        if row is None and not _is_uuid(workspace_id):
            return True
        return row is not None

    async def _read(self, what: str, query: Any) -> Any:
        """Run one authority query, distinguishing "no grant" from "cannot tell".

        Returns the row, `None` for an absent grant, or `_UNREADABLE` when the
        question could not be answered. The three-way return is the point. Every
        one of these reads used to collapse into `return None`, and `None` is how
        this resolver spells *refusal* — so a database that could not be reached
        while checking authority was reported to the caller as "you are not
        entitled", a 403 for what is a 503. That is the specific conflation this
        story forbids: it tells a tenant their authority was rejected when nothing
        about their authority was ever established, and it points an operator at
        the grant tables instead of at the outage.

        `_IdentityNotUuid` stays a refusal, because that one *is* an answer: an
        identifier the grant tables cannot be keyed on is not a grant, and querying
        a coerced value would be answering a different question than the one asked.
        """
        try:
            async with self._session_factory() as session:
                return (await query(session)).scalar_one_or_none()
        except _IdentityNotUuid:
            return None
        except Exception:
            # Deliberately `exc_info=False`: a SQLAlchemy traceback can quote the
            # statement and its bound parameters, and these carry tenant
            # identifiers and principal subjects.
            logger.warning(
                "the %s could not be read while resolving a principal; reporting "
                "the authority as unavailable rather than as absent",
                what,
                exc_info=False,
            )
            return _UNREADABLE

    async def _approver_statuses(
        self, principal: Any, *, organization_scope: bool = False
    ) -> dict[str, Any]:
        """The current authority of each potential approver in this tenant.

        Read per request rather than stored on a record, because the question is
        who may approve *now*. A revoked approver is reported with `revoked=True`
        and `is_member=True`, never as absent: absence reads as "never had
        authority", and only "had it and lost it" means a decision already taken
        under that authority must be re-examined.

        A failed read raises `_AuthorityUnreadable` rather than returning `{}`, for
        the same reason `_read` distinguishes the two. `{}` is a *claim*: it tells
        `evaluate_approval` that no selected approver's authority could be
        established, which it correctly turns into a refusal (step 7, "an unverified
        approver does not authorize"). Refusing is the safe direction, but the
        reason would be a fabrication — "this approver has no authority" asserted
        from a database that never answered. The harness's `_approval_for` converts
        a raise into `OperationUnavailable`, which is what an unreachable grant
        table actually is.
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
                                WorkspaceGrantRecord.principal_type == "human",
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
                                OrganizationGrantRecord.org_id == _as_uuid(org_id),
                                OrganizationGrantRecord.principal_type == "human",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        except _IdentityNotUuid:
            # An answer: identifiers the grant tables cannot be keyed on have no
            # approvers, rather than unknown ones.
            return {}
        except Exception:
            logger.warning(
                "approver authority could not be read; reporting it as unavailable "
                "rather than as an absence of approvers",
                exc_info=False,
            )
            raise _AuthorityUnreadable("approver authority") from None

        for record in workspace_grants:
            statuses[record.principal] = ApproverStatus(
                subject=record.principal,
                is_member=True,
                permissions=frozenset(
                    _expand(record.permission_values(), "workspace grant")
                ),
                revoked=record.revoked_at is not None,
            )

        # Org administration cannot override an existing workspace boundary.
        if not organization_scope and await self._workspace_exists(workspace_id):
            return await self._current_approvers(org_id, statuses)

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

        return await self._current_approvers(org_id, statuses)

    async def _current_approvers(self, org_id: str, statuses: dict[str, Any]) -> dict[str, Any]:
        from app.models.organization import Organization

        if not statuses or not identity_checks_enabled():
            return statuses
        organization = await self._read(
            "approver organization",
            lambda session: session.execute(
                select(Organization).where(Organization.id == _as_uuid(org_id))
            ),
        )
        if organization is _UNREADABLE:
            raise _AuthorityUnreadable("approver organization")
        if organization is None:
            return {}
        if not organization.adp_org_id:
            return statuses
        caller = _acting.get()
        if (
            caller is None
            or caller.org_id != org_id
            or caller.adp_org_id != organization.adp_org_id
        ):
            return {
                subject: replace(status, is_member=False, revoked=True)
                for subject, status in statuses.items()
            }
        for subject, status in statuses.items():
            try:
                await require_current_identity(
                    caller.identity_reader,
                    subject=subject,
                    principal_type="human",
                    adp_org_id=organization.adp_org_id,
                )
            except IdentityUnavailable:
                statuses[subject] = replace(status, is_member=False, revoked=True)
        return statuses


class _IdentityNotUuid(Exception):
    """An identifier that cannot key the grant tables."""


class _AuthorityUnreadable(Exception):
    """The grant tables could not be read, so authority is unknown — not absent.

    Raised rather than returned so it cannot be mistaken for a refusal by a caller
    that forgot to check. `resolve` lets it propagate, and the harness's `_resolve`
    converts a *raise* into `OperationUnavailable` while converting a `None` into
    `OperationRefused` (`harness_jobs/facade.py:_resolve`). That is exactly the
    split this exception exists to reach: `unknown` is not failure and 503 is not
    403, so an unreachable grant table must not be reported as a denied caller.
    """


class _Unreadable:
    """Sentinel for a read that could not be performed.

    A distinct type rather than `None`, because `None` already means "no such
    grant" on these reads and the whole defect being fixed is those two being the
    same value.
    """

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "<authority unreadable>"


_UNREADABLE = _Unreadable()


def _expand(values: Any, source: str) -> set[str]:
    """Close a stored permission set over `_IMPLIED`, dropping what this build
    does not understand.

    An unrecognized value grants nothing rather than passing through: a downgrade
    that removes a permission must not leave a grant asserting authority this code
    cannot reason about. Same rule as `WorkspaceGrantRecord.permission_values`.
    """
    from superplane_auth.policy import Permission, expand_permissions

    known: set[Permission] = set()
    for value in values:
        try:
            known.add(Permission(value))
        except ValueError:
            logger.warning(
                "a %s carries an unrecognized permission; ignoring it", source
            )
    return {str(item) for item in expand_permissions(known)}


def _as_uuid(value: str) -> Any:
    import uuid

    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise _IdentityNotUuid(value) from None


def _is_uuid(value: str) -> bool:
    """Whether `_as_uuid` would accept this identifier.

    Written in terms of `_as_uuid` rather than repeating the parse, so the two can
    never disagree about what keys the grant tables — a second, subtly different
    acceptance rule is the bug this avoids, not the duplication.
    """
    try:
        _as_uuid(value)
    except _IdentityNotUuid:
        return False
    return True


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
