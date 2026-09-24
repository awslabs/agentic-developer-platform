"""Principal resolution from real grant rows, against real PostgreSQL. Issue #5535.

Set ``SUPERPLANE_TEST_POSTGRES_URL`` to a ``postgresql+asyncpg`` URL. Each test
creates and removes its own random schema; no provider, cloud or B service is
contacted, and a green run here is NOT live acceptance evidence.

## What this suite is really testing

`GrantBackedAuthority.resolve` answers one question — *is this caller entitled to
this operation on this workspace* — and it has three distinct answers, not two:
entitled, refused, and **could not tell**. The harness makes that third answer
reachable: `harness_jobs/facade.py:_resolve` converts a returned ``None`` into
``OperationRefused`` and a *raise* into ``OperationUnavailable``. So the return
type is load-bearing in a way a test can easily miss — ``None`` means "403, you
are not entitled" and is the wrong answer for a database outage.

## Why real PostgreSQL, and why that is not a preference

The resolver's correctness is a property of *queries against the real schema*:

* ``workspace_grants.workspace_id`` is a FOREIGN KEY to ``workspaces.id``. That
  constraint is the entire justification for the organization fallback — a grant
  for a nonexistent workspace is unrepresentable, not merely missing — and SQLite
  as configured for the HTTP double does not enforce it.
* The unreadable-table cases are provoked by really renaming a real table, so the
  failure arrives as the driver error the adapter must classify. A monkeypatched
  exception would assert that the ``except`` clause runs, not that a genuine
  database failure reaches it.
* ``organization_grants``/``workspace_grants`` store permissions as a delimited
  string expanded through ``superplane_auth.policy.expand_permissions``; the
  closure over ``_IMPLIED`` is what makes ``administer`` sufficient for
  ``provision``, and it is asserted here against stored rows rather than literals.

## The four properties, in the order they matter

1. **An organization administrator can create the organization's FIRST workspace.**
   Measured before the fallback existed: they resolved to ``None``, so no
   organization could ever create a workspace. Zero-workspace ordering, failing
   closed in the unusable direction.
2. **The fallback does not widen authority on workspaces that DO exist.** ``POST
   /workspaces`` is the only ``(Scope.ORGANIZATION, Permission.PROVISION)`` route
   in the endpoint inventory; every other workspace route is ``Scope.WORKSPACE``
   and `app/auth.py:authorize_workspace_operation` offers no organization
   fallback. Since ``_IMPLIED[ADMINISTER]`` closes over ``SPEND``, an unbounded
   fallback grants spend on every existing workspace through a port whose own HTTP
   guard refuses it.
3. **A revoked workspace grant is terminal, not missing.** The grant is read
   WITHOUT ``revoked_at IS NULL`` so revocation can be distinguished from absence.
4. **An unreadable table is 503, never 403.** Asserted BY TYPE, never by message.

## What mutation testing established, including one uncomfortable result

Every guard was reverted in place and this suite re-run, because a passing
authorization test proves nothing about whether it would *catch* anything:

* removing the ``_workspace_exists`` gate — 4 failures
* collapsing ``_UNREADABLE`` back into ``None`` — 3 failures
* treating an unlookupable id as absent — 1 failure
* **restoring the ``revoked_at IS NULL`` filter — 0 failures.**

That last one is recorded rather than hidden. The revocation guard is not
independently reachable: a revoked grant implies its workspace exists (foreign
key), and an existing workspace already closes the organization fallback, so with
the existence gate in place the filter changes no outcome. Reverting BOTH guards
together fails 6 tests, which is what pins the pair. The test below is therefore
honest about being a guard against future relaxation rather than a demonstration
of a currently-reachable defect — the alternative was to delete it and lose the
regression, or to leave it implying a strength it does not have on its own.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from app.adapters.operation_authority_source import (
    ActingPrincipal,
    GrantBackedAuthority,
    _AuthorityUnreadable,
    reset_acting_principal,
    set_acting_principal,
)
from app.models.organization_grant import (
    ORGANIZATION_ADMINISTER,
    ORGANIZATION_READ,
    OrganizationGrantRecord,
)
from app.models.workspace_grant import WorkspaceGrantRecord
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.test_installation_postgres import (
    installation_postgres_url as installation_postgres_url,
)
from tests.test_installation_postgres import pytestmark as postgres_available

pytestmark = [] if os.environ.get("CI") else postgres_available

PROVISION = "workspace:provision"
SPEND = "workspace:spend"


@pytest.fixture
async def authority(installation_postgres_url):  # noqa: F811 - pytest fixture injection
    """A resolver over its own schema, with helpers to seed real grant rows.

    Yields a small facade rather than the adapter alone because every test needs
    the same three things: seed an organization, seed a workspace, ask. Building
    them per test invited the subtle divergence where one test's "org admin" row
    differs from another's and the suite stops comparing like with like.

    The tables are created from the declarative models rather than by replaying the
    Alembic chain: unlike the ledger's table these long predate this story, they
    carry the real foreign key this suite depends on, and `tests/test_models.py`
    already pins the declaration against the migrations.
    """
    url = installation_postgres_url
    schema = "authority_test_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    await admin.dispose()

    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    from app.database import Base
    from app.models.cloud_account import CloudAccount
    from app.models.cluster import Cluster
    from app.models.organization import Organization
    from app.models.workspace import Workspace
    from app.models.operation_approval import OperationApproval
    from app.services.operation_approvals import ApprovalService

    tables = [
        Organization.__table__,
        CloudAccount.__table__,
        Cluster.__table__,
        Workspace.__table__,
        OrganizationGrantRecord.__table__,
        WorkspaceGrantRecord.__table__,
        OperationApproval.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=tables)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    tokens: list[object] = []

    class Harness:
        def __init__(self) -> None:
            self.resolver = GrantBackedAuthority(factory)
            self.schema = schema
            self.engine = engine
            self.approvals = ApprovalService(factory)

        async def organization(self, permissions: str = ORGANIZATION_ADMINISTER):
            """An organization and a principal holding `permissions` on it."""
            org_id = uuid.uuid4()
            subject = "user-" + uuid.uuid4().hex[:8]
            async with factory() as session:
                session.add(
                    Organization(id=org_id, name=str(org_id), billing_plan="enterprise")
                )
                session.add(
                    OrganizationGrantRecord(
                        org_id=org_id,
                        principal=subject,
                        principal_type="human",
                        permissions=permissions,
                        granted_by="bootstrap",
                    )
                )
                await session.commit()
            return org_id, subject

        async def workspace(self, org_id, workspace_id=None) -> uuid.UUID:
            workspace_id = workspace_id or uuid.uuid4()
            async with factory() as session:
                session.add(
                    Workspace(
                        id=workspace_id,
                        org_id=org_id,
                        name="ws-" + uuid.uuid4().hex[:8],
                        isolation_mode="dedicated",
                        status="active",
                    )
                )
                await session.commit()
            return workspace_id

        async def grant(
            self, org_id, workspace_id, subject, permissions, *, revoked: bool = False
        ) -> None:
            async with factory() as session:
                session.add(
                    WorkspaceGrantRecord(
                        workspace_id=workspace_id,
                        org_id=org_id,
                        principal=subject,
                        principal_type="human",
                        permissions=permissions,
                        revoked_at=(datetime.now(timezone.utc) if revoked else None),
                    )
                )
                await session.commit()

        async def ask(self, org_id, subject, workspace_id, permission=PROVISION):
            """Resolve as `subject`, with the acting principal bound and reset.

            Bound through the real contextvar rather than by passing an argument,
            because that IS the adapter's contract: the resolver's arguments are an
            assertion to check and the authority comes from the authenticated
            context (`harness_jobs/facade.py:206`).
            """
            token = set_acting_principal(
                ActingPrincipal(
                    subject=subject,
                    org_id=str(org_id),
                    workspace_id=str(workspace_id),
                )
            )
            tokens.append(token)
            return await self.resolver.resolve(
                org_id=str(org_id),
                workspace_id=str(workspace_id),
                permission=permission,
            )

        async def hide(self, table: str) -> None:
            """Rename a table away, so reads against it fail for real."""
            async with engine.begin() as connection:
                await connection.execute(
                    text(f'ALTER TABLE "{schema}".{table} RENAME TO {table}_hidden')
                )

    try:
        yield Harness()
    finally:
        for token in reversed(tokens):
            try:
                reset_acting_principal(token)
            except ValueError:  # pragma: no cover - different context
                pass
        await engine.dispose()
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


# ----------------------------------------------------------------------
# 1. Zero-workspace ordering
# ----------------------------------------------------------------------


async def test_budget_caps_are_read_from_current_workspace_rows(authority):
    org, _ = await authority.organization()
    workspace = await authority.workspace(org)
    async with authority.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE workspaces SET budget_max_daily_usd=12.50, "
                "budget_max_hourly_usd=2.25, budget_max_gpus=4 WHERE id=:id"
            ),
            {"id": workspace},
        )
    limits = await authority.resolver.budget_limits_for(
        org_id=str(org), workspace_id=str(workspace)
    )
    assert limits.max_cost_micros == 2_250_000
    assert limits.max_resource_units == 4
    async with authority.engine.begin() as connection:
        await connection.execute(
            text("UPDATE workspaces SET budget_max_hourly_usd=0 WHERE id=:id"),
            {"id": workspace},
        )
    assert (
        await authority.resolver.budget_limits_for(
            org_id=str(org), workspace_id=str(workspace)
        )
    ).max_cost_micros == 0


async def test_budget_caps_never_read_another_organizations_workspace(authority):
    org, _ = await authority.organization()
    workspace = await authority.workspace(org)
    with pytest.raises(_AuthorityUnreadable):
        await authority.resolver.budget_limits_for(
            org_id=str(uuid.uuid4()), workspace_id=str(workspace)
        )


async def test_unreadable_budget_store_is_not_an_unconfigured_cap(authority):
    org, _ = await authority.organization()
    workspace = await authority.workspace(org)
    await authority.hide("workspaces")
    with pytest.raises(_AuthorityUnreadable):
        await authority.resolver.budget_limits_for(
            org_id=str(org), workspace_id=str(workspace)
        )


async def test_org_admin_can_provision_the_first_workspace(authority):
    """The zero-workspace ordering requirement, as a test.

    The workspace does not exist, so no workspace grant CAN exist for it. Before
    the organization fallback this returned ``None`` and the organization could
    never create a workspace.
    """
    org_id, subject = await authority.organization()

    resolved = await authority.ask(org_id, subject, uuid.uuid4())

    assert resolved is not None, (
        "an organization administrator resolved to None for a workspace that does "
        "not exist yet, so no organization can create its first workspace"
    )
    assert resolved.may_provision is True


async def test_org_admin_provisioning_authority_is_the_implied_closure(authority):
    """`administer` is sufficient for `provision` via `_IMPLIED`, from a real row.

    Asserted as the closure rather than as an equality against a literal set: the
    property that matters is that the stored ``administer`` grant yields the same
    permissions a stored workspace ``administer`` grant would, and pinning an exact
    set here would make this test fail whenever the policy gains a permission
    without anything actually being wrong.
    """
    from superplane_auth.policy import Permission, expand_permissions

    org_id, subject = await authority.organization()

    resolved = await authority.ask(org_id, subject, uuid.uuid4())

    expected = {str(p) for p in expand_permissions({Permission.ADMINISTER})}
    assert set(resolved.permissions) == expected
    assert str(Permission.PROVISION) in resolved.permissions


async def test_organization_read_alone_does_not_provision(authority):
    """`organization:read` is not provisioning authority.

    `authorize_organization_operation` admits ``organization:read`` for
    ``Permission.READ`` alone, and provisioning is not a read.
    """
    org_id, subject = await authority.organization(permissions=ORGANIZATION_READ)

    assert await authority.ask(org_id, subject, uuid.uuid4()) is None


async def test_revoked_organization_grant_does_not_provision(authority):
    org_id = uuid.uuid4()
    subject = "user-" + uuid.uuid4().hex[:8]
    from app.models.organization import Organization

    factory = authority.resolver._session_factory
    async with factory() as session:
        session.add(Organization(id=org_id, name=str(org_id), billing_plan="free"))
        session.add(
            OrganizationGrantRecord(
                org_id=org_id,
                principal=subject,
                principal_type="human",
                permissions=ORGANIZATION_ADMINISTER,
                granted_by="bootstrap",
                revoked_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    assert await authority.ask(org_id, subject, uuid.uuid4()) is None


async def test_no_grant_at_all_is_refused(authority):
    """Absence is a refusal — and specifically a refusal, not an outage."""
    from app.models.organization import Organization

    org_id = uuid.uuid4()
    factory = authority.resolver._session_factory
    async with factory() as session:
        session.add(Organization(id=org_id, name=str(org_id), billing_plan="free"))
        await session.commit()

    assert await authority.ask(org_id, "nobody", uuid.uuid4()) is None


# ----------------------------------------------------------------------
# 2. The fallback is bounded to nonexistent workspaces
# ----------------------------------------------------------------------


async def test_org_admin_has_no_implicit_authority_on_an_existing_workspace(
    authority,
):
    """The widening the `_workspace_exists` gate closes.

    The workspace EXISTS and this principal holds no grant on it. A workspace grant
    is representable here, so its absence is informative — and
    `authorize_workspace_operation` would refuse this caller. The port must agree
    with the guard.
    """
    org_id, subject = await authority.organization()
    workspace_id = await authority.workspace(org_id)

    assert await authority.ask(org_id, subject, workspace_id) is None, (
        "an organization grant conferred authority on an EXISTING workspace, which "
        "the HTTP guard for the same workspace refuses"
    )


async def test_org_admin_does_not_gain_spend_on_an_existing_workspace(authority):
    """The most damaging consequence of an unbounded fallback, named explicitly.

    ``_IMPLIED[ADMINISTER]`` closes over ``SPEND``, so an unbounded organization
    fallback is not only a read widening — it authorizes spend against a
    workspace's budget on a port whose HTTP guard grants no such thing.
    """
    org_id, subject = await authority.organization()
    workspace_id = await authority.workspace(org_id)

    assert await authority.ask(org_id, subject, workspace_id, permission=SPEND) is None


async def test_a_live_workspace_grant_still_resolves(authority):
    """The fallback did not replace the primary path.

    The organization grant here deliberately carries only ``organization:read``, so
    the authority can have come from nowhere but the workspace grant itself.
    """
    org_id, subject = await authority.organization(permissions=ORGANIZATION_READ)
    workspace_id = await authority.workspace(org_id)
    await authority.grant(org_id, workspace_id, subject, PROVISION)

    resolved = await authority.ask(org_id, subject, workspace_id)

    assert resolved is not None
    assert resolved.may_provision is True


async def test_a_workspace_grant_without_provision_is_refused(authority):
    """Holding *a* grant is not holding *this* permission."""
    org_id, subject = await authority.organization(permissions=ORGANIZATION_READ)
    workspace_id = await authority.workspace(org_id)
    await authority.grant(org_id, workspace_id, subject, "workspace:read")

    assert await authority.ask(org_id, subject, workspace_id) is None


async def test_an_unlookupable_workspace_id_does_not_open_the_fallback(authority):
    """A non-UUID id must not be treated as "workspace does not exist".

    ``_workspace_exists`` returns ``True`` for an identifier the table cannot be
    keyed on — not a claim that the workspace exists, but a refusal to widen
    authority for something it could not look up.
    """
    org_id, subject = await authority.organization()

    assert await authority.ask(org_id, subject, "not-a-uuid") is None


# ----------------------------------------------------------------------
# 3. Revocation is terminal
# ----------------------------------------------------------------------


async def test_a_revoked_workspace_grant_is_not_reinstated_by_the_organization(
    authority,
):
    """A revoked grant must not be reinstated by an organization grant.

    The principal is an organization administrator AND holds a revoked workspace
    grant — the combination that was wrong when this story found it, at a point when
    the fallback was unconditional.

    MEASURED: this test does NOT fail if only the ``revoked_at IS NULL`` filter is
    restored, because the ``_workspace_exists`` gate independently refuses the case
    (a revoked grant implies an existing workspace). It fails when both guards are
    reverted. See the module docstring — it is kept as a guard against a future
    relaxation of the existence bound, not as proof of a live defect.
    """
    org_id, subject = await authority.organization()
    workspace_id = await authority.workspace(org_id)
    await authority.grant(
        org_id, workspace_id, subject, "workspace:administer", revoked=True
    )

    assert await authority.ask(org_id, subject, workspace_id) is None, (
        "a revoked workspace grant was reinstated by an organization grant"
    )


async def test_a_grant_cannot_outlive_its_workspace_row(authority):
    """Why the order of the two checks cannot be observed, stated as a constraint.

    The resolver judges the workspace grant BEFORE testing whether the workspace
    exists, and the reverse order would be a real defect: a revoked grant whose
    workspace row had vanished would fall through to the organization fallback and
    be reinstated. This test does not assert that ordering through behaviour,
    because the database makes the state unreachable — and that is the more durable
    fact, so it is what gets pinned.

    Attempting the DELETE is the assertion. ``workspace_grants.workspace_id`` is a
    foreign key, so a grant cannot outlive its workspace; and ``DELETE
    /workspaces/{id}`` is a SOFT delete that leaves the row behind with
    ``status="Teardown"`` (`app/auth.py:513`), so nothing in the application removes
    it either. If a future migration drops this constraint or adds ``ON DELETE
    CASCADE``, this test fails and points at the ordering that then starts to
    matter.
    """
    from sqlalchemy.exc import IntegrityError

    org_id, subject = await authority.organization()
    workspace_id = await authority.workspace(org_id)
    await authority.grant(
        org_id, workspace_id, subject, "workspace:administer", revoked=True
    )

    with pytest.raises(IntegrityError):
        async with authority.engine.begin() as connection:
            await connection.execute(
                text(f'DELETE FROM "{authority.schema}".workspaces WHERE id = :i'),
                {"i": workspace_id},
            )

    # The revoked grant still refuses, with the workspace row intact.
    assert await authority.ask(org_id, subject, workspace_id) is None


# ----------------------------------------------------------------------
# 4. Unreadable is unavailable, never unentitled
# ----------------------------------------------------------------------


async def test_an_unreadable_workspace_grant_table_is_unavailable(authority):
    """503, not 403 — asserted by type.

    ``None`` here would tell a tenant they are not entitled when nothing about
    their entitlement was established, and would point an operator at the grant
    tables rather than at the outage.
    """
    org_id, subject = await authority.organization()
    await authority.hide("workspace_grants")

    with pytest.raises(_AuthorityUnreadable):
        await authority.ask(org_id, subject, uuid.uuid4())


async def test_an_unreadable_organization_grant_table_is_unavailable(authority):
    org_id, subject = await authority.organization()
    await authority.hide("organization_grants")

    with pytest.raises(_AuthorityUnreadable):
        await authority.ask(org_id, subject, uuid.uuid4())


async def test_an_unreadable_workspaces_table_is_unavailable(authority):
    """An outage in the existence check must not decide the fallback either way.

    ``False`` would open the fallback during an outage; ``True`` would report a
    refusal for one. Both are answers manufactured from a non-answer.
    """
    org_id, subject = await authority.organization()
    await authority.hide("workspaces")

    with pytest.raises(_AuthorityUnreadable):
        await authority.ask(org_id, subject, uuid.uuid4())


async def test_no_acting_principal_is_a_refusal_not_an_outage(authority):
    """The boot-time capability probe runs with no request at all.

    It must resolve to ``None`` — a refusal — rather than raising, or process start
    would report the port as unavailable.
    """
    reset = set_acting_principal(None)
    try:
        assert (
            await authority.resolver.resolve(
                org_id="", workspace_id="", permission=PROVISION
            )
            is None
        )
    finally:
        reset_acting_principal(reset)


async def test_a_caller_naming_another_tenant_is_refused(authority):
    """An asserted org that disagrees with the authenticated one is a refusal.

    Never "corrected" to the real tenant: a path that reads a caller-supplied
    tenant and then overrides it is one reordering away from honouring it.
    """
    org_id, subject = await authority.organization()
    other = uuid.uuid4()
    token = set_acting_principal(
        ActingPrincipal(
            subject=subject, org_id=str(org_id), workspace_id=str(uuid.uuid4())
        )
    )
    try:
        assert (
            await authority.resolver.resolve(
                org_id=str(other), workspace_id="", permission=PROVISION
            )
            is None
        )
    finally:
        reset_acting_principal(token)


# ----------------------------------------------------------------------
# 5. The ApprovalSource half of the same adapter
# ----------------------------------------------------------------------
#
# WHY THIS SECTION EXISTS, AND WHAT ITS ABSENCE COST. `approval_for` had NO test
# of any kind, and the suite was green at 1510 with its only reachable line
# raising `ImportError` — `ApprovalContext` was imported from
# `harness_jobs.approval`, where it is not defined (it lives in
# `harness_jobs.facade`). The defect was invisible from every direction:
#
#   * `facade._approval_for` converts ANY exception from this method into
#     `OperationUnavailable`, so the import error surfaced as
#     `ProvisioningUnavailable: the approval for this request could not be
#     established` — a plausible message for a real approval-store outage.
#   * `app/routers/workspaces.py` maps that to **HTTP 503**, so a self-service
#     user saw "try again later" for a condition no retry would ever change.
#   * The module's own docstring described the correct behaviour, so reading the
#     code agreed with the design while the running code did neither.
#
# So a wiring error was indistinguishable from an outage, and the documented
# answer — a refusal for want of approval — was unreachable. Measured end to end
# both before and after: before, `start_provision` raised
# `ProvisioningUnavailable` (503); after, `ProvisioningRefused: no approval record
# for this request; absence is not permission` (400).
#
# These tests therefore assert the CALL SUCCEEDS and returns the harness's real
# type, which is the part that was broken, before asserting anything about its
# contents.


async def _request(action: str = "provision", **parameters):
    """A well-formed `OperationRequest`, built by the harness's own validator."""
    from harness_jobs.identity import OperationRequest

    return OperationRequest(
        action=action,
        idempotency_key="idem-" + uuid.uuid4().hex[:12],
        parameters=parameters or {"workspace_name": "w", "isolation_mode": "shared"},
    )


async def test_approval_for_returns_the_harness_approval_context(authority):
    """The call completes and returns the real type — the regression that was live.

    `isinstance` against the class imported from `harness_jobs.facade` rather than
    a duck-typed attribute check: the defect was an import of a name that does not
    exist in the module it was taken from, and only identity against the real class
    establishes that the right one is now in hand. A `hasattr` assertion would pass
    against any object with three attributes.
    """
    from harness_jobs.facade import ApprovalContext

    org_id, subject = await authority.organization()
    workspace_id = uuid.uuid4()
    principal = await authority.ask(org_id, subject, workspace_id)
    assert principal is not None, "the fixture must resolve before approval is asked"

    context = await authority.resolver.approval_for(
        principal=principal, request=await _request()
    )

    assert isinstance(context, ApprovalContext)


async def _approval_participants(authority):
    org, requester = await authority.organization()
    workspace = await authority.workspace(org)
    approver = "approver-" + uuid.uuid4().hex
    await authority.grant(org, workspace, requester, PROVISION)
    await authority.grant(org, workspace, approver, "workspace:administer")
    principal = await authority.ask(org, requester, workspace)
    request = await _request(
        max_resource_units="2", max_runtime_seconds="60", max_cost_micros="100"
    )
    ticket = await authority.approvals.issue(
        workspace_id=str(workspace), request=request
    )
    return org, workspace, requester, approver, principal, request, ticket


async def test_explicit_approval_is_durable_and_gates_the_exact_request(authority):
    from harness_jobs.approval import evaluate_approval
    from harness_jobs.identity import OperationRequest

    (
        org,
        workspace,
        requester,
        approver,
        principal,
        request,
        ticket,
    ) = await _approval_participants(authority)
    assert ticket["result"] == "pending"
    assert ticket["approvers"] == [approver]
    assert not ticket["can_decide"]
    assert (
        await authority.resolver.approval_for(principal=principal, request=request)
    ).record is None
    assert (
        await authority.approvals.issue(workspace_id=str(workspace), request=request)
    )["approval_id"] == ticket["approval_id"]
    await authority.ask(org, approver, workspace)
    assert (await authority.approvals.read(ticket["approval_id"]))["can_decide"]
    await authority.approvals.decide(ticket["approval_id"], "allowed-once")
    await authority.ask(org, requester, workspace)
    context = await authority.resolver.approval_for(
        principal=principal, request=request
    )
    assert context.record.approval_id == ticket["approval_id"]
    assert evaluate_approval(
        context.record,
        principal=principal,
        request=request,
        requested_envelope=context.requested_envelope,
        approver_statuses=context.approver_statuses,
        now=datetime.now(timezone.utc),
    ).permitted
    changed = OperationRequest(
        action=request.action,
        idempotency_key=request.idempotency_key,
        parameters=dict(request.parameters, max_cost_micros="101"),
    )
    assert (
        await authority.resolver.approval_for(principal=principal, request=changed)
    ).record is None


async def test_requester_cannot_decide_own_ticket(authority):
    from app.services.operation_approvals import ApprovalDenied

    *_, ticket = await _approval_participants(authority)
    with pytest.raises(ApprovalDenied):
        await authority.approvals.decide(ticket["approval_id"], "allowed-once")
    assert (await authority.approvals.read(ticket["approval_id"]))[
        "result"
    ] == "pending"


async def test_decision_cannot_change_after_a_lost_reply(authority):
    from app.services.operation_approvals import ApprovalDenied

    org, workspace, _, approver, _, _, ticket = await _approval_participants(authority)
    await authority.ask(org, approver, workspace)
    first = await authority.approvals.decide(ticket["approval_id"], "allowed-once")
    again = await authority.approvals.decide(ticket["approval_id"], "allowed-once")
    assert again["decided_at"] == first["decided_at"]
    with pytest.raises(ApprovalDenied):
        await authority.approvals.decide(ticket["approval_id"], "rejected")


async def test_approver_revocation_invalidates_a_stored_allow(authority):
    from harness_jobs.approval import evaluate_approval

    (
        org,
        workspace,
        requester,
        approver,
        principal,
        request,
        ticket,
    ) = await _approval_participants(authority)
    await authority.ask(org, approver, workspace)
    await authority.approvals.decide(ticket["approval_id"], "allowed-once")
    async with authority.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE workspace_grants SET revoked_at=now() WHERE workspace_id=:workspace AND principal=:subject"
            ),
            {"workspace": workspace, "subject": approver},
        )
    await authority.ask(org, requester, workspace)
    context = await authority.resolver.approval_for(
        principal=principal, request=request
    )
    assert not evaluate_approval(
        context.record,
        principal=principal,
        request=request,
        requested_envelope=context.requested_envelope,
        approver_statuses=context.approver_statuses,
        now=datetime.now(timezone.utc),
    ).permitted


async def test_unreadable_approval_store_reports_unavailable(authority):
    org, requester = await authority.organization()
    principal = await authority.ask(org, requester, uuid.uuid4())
    await authority.hide("operation_approvals")
    with pytest.raises(_AuthorityUnreadable):
        await authority.resolver.approval_for(
            principal=principal, request=await _request()
        )


async def test_first_workspace_approval_keeps_original_org_scope_after_registration(
    authority,
):
    from harness_jobs.approval import evaluate_approval

    org, requester = await authority.organization()
    workspace, approver = uuid.uuid4(), "distinct-approver"
    async with authority.approvals.sessions() as session:
        session.add(
            OrganizationGrantRecord(
                org_id=org,
                principal=approver,
                principal_type="human",
                permissions=ORGANIZATION_ADMINISTER,
                granted_by="bootstrap",
            )
        )
        await session.commit()
    principal = await authority.ask(org, requester, workspace)
    request = await _request(
        max_resource_units="1", max_runtime_seconds="60", max_cost_micros="100"
    )
    ticket = await authority.approvals.issue(
        workspace_id=str(workspace), request=request
    )
    await authority.ask(org, approver, workspace)
    await authority.approvals.decide(ticket["approval_id"], "allowed-once")
    await authority.workspace(org, workspace)
    await authority.grant(org, workspace, requester, "workspace:administer")
    principal = await authority.ask(org, requester, workspace)
    context = await authority.resolver.approval_for(
        principal=principal, request=request
    )
    assert evaluate_approval(
        context.record,
        principal=principal,
        request=request,
        requested_envelope=context.requested_envelope,
        approver_statuses=context.approver_statuses,
        now=datetime.now(timezone.utc),
    ).permitted
    async with authority.engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE organization_grants SET revoked_at=now() WHERE principal=:principal"
            ),
            {"principal": approver},
        )
    context = await authority.resolver.approval_for(
        principal=principal, request=request
    )
    assert not evaluate_approval(
        context.record,
        principal=principal,
        request=request,
        requested_envelope=context.requested_envelope,
        approver_statuses=context.approver_statuses,
        now=datetime.now(timezone.utc),
    ).permitted
    # The preserved scope belongs only to the pre-creation approval, not all new
    # approvals on the now existing workspace.
    workspace_approver = "current-workspace-approver"
    await authority.grant(org, workspace, workspace_approver, "workspace:administer")
    other = await _request(max_cost_micros="200")
    fresh = await authority.approvals.issue(workspace_id=str(workspace), request=other)
    assert approver not in fresh["approvers"]
    assert workspace_approver in fresh["approvers"]


async def test_org_admin_has_no_implicit_approval_on_existing_workspace(authority):
    org, subject = await authority.organization()
    workspace = await authority.workspace(org)
    from harness_jobs.identity import ResolvedPrincipal

    principal = ResolvedPrincipal(
        str(org), str(workspace), subject, frozenset({PROVISION})
    )
    assert subject not in await authority.resolver._approver_statuses(principal)


async def test_approval_is_refused_for_want_of_a_record_not_reported_unavailable(
    authority,
):
    """`record is None` — absence, which the gate turns into a refusal.

    The distinction this pins is the one the import bug destroyed: `None` here
    reaches the caller as `ProvisioningRefused` (a durable answer about this
    request), whereas a raise reaches them as `ProvisioningUnavailable` (an outage,
    inviting a retry). The domain holds no approval table, so absence is the honest
    answer, and it must arrive as absence rather than as a failure to answer.
    """
    org_id, subject = await authority.organization()
    principal = await authority.ask(org_id, subject, uuid.uuid4())

    context = await authority.resolver.approval_for(
        principal=principal, request=await _request()
    )

    assert context.record is None


async def test_the_requested_envelope_is_a_real_spend_envelope(authority):
    """Derived from the request, in the type the gate's envelope check requires.

    `evaluate_approval` step (8) calls `record.envelope.covers(requested_envelope)`,
    which is a `SpendEnvelope` method — so a duck-typed stand-in would fail there
    rather than here, at admission time, on a real provisioning request.
    """
    from harness_jobs.approval import SpendEnvelope

    org_id, subject = await authority.organization()
    principal = await authority.ask(org_id, subject, uuid.uuid4())

    context = await authority.resolver.approval_for(
        principal=principal, request=await _request()
    )

    assert isinstance(context.requested_envelope, SpendEnvelope)


async def test_a_self_issued_approval_is_refused_even_though_the_requester_may_approve(
    authority,
):
    """The requester DOES carry approval authority, and still cannot approve itself.

    MEASURED, and two earlier versions of this test asserted the opposite. They
    claimed `may_approve` must be `False` for the requester, on the reasoning that a
    requester able to approve is self-issued authority. That premise is wrong twice
    over:

    * `APPROVAL_PERMISSION` is `workspace:administer`, which an organization
      administrator's closure legitimately contains. Suppressing it here would make
      the requester's *real* authority invisible to the gate — and step (7) of
      `evaluate_approval` re-reads exactly that authority for every selected
      approver, so the same person approving a colleague's request would be refused
      for want of authority they actually hold.
    * The no-self-approval rule is enforced on **identity**, not on permission:
      `requires_distinct_approver` compares `decided_by` against the requester, and
      step (6) refuses before the envelope is ever considered.

    So the property worth pinning is the end-to-end one, built on this adapter's
    real `approver_statuses`: a record decided by the requester is refused, while the
    same record decided by a distinct approver of equal authority is permitted. That
    contrast is what makes the refusal attributable to self-issuance rather than to
    some unrelated check failing first.
    """
    from datetime import timedelta

    from harness_jobs.approval import (
        ApprovalBinding,
        ApprovalRecord,
        ApprovalResult,
        ApproverStatus,
        evaluate_approval,
    )

    org_id, subject = await authority.organization()
    principal = await authority.ask(org_id, subject, uuid.uuid4())
    request = await _request()
    context = await authority.resolver.approval_for(
        principal=principal, request=request
    )

    status = context.approver_statuses.get(subject)
    assert status is not None, (
        "the requester's own authority must still be read: a gate that cannot see "
        "it cannot re-check it for a request this person approves for someone else"
    )
    assert status.may_approve, (
        "an organization administrator's closure contains APPROVAL_PERMISSION; "
        "hiding it would misreport real authority to the gate"
    )

    now = datetime.now(timezone.utc)
    binding = ApprovalBinding.for_request(principal, request)
    envelope = context.requested_envelope

    def _record(decided_by: str, approval_id: str) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=approval_id,
            binding=binding,
            envelope=envelope,
            result=ApprovalResult.ALLOWED_ONCE,
            approvers=frozenset({decided_by}),
            decided_by=decided_by,
            decided_at=now,
            expires_at=now + timedelta(hours=1),
        )

    def _decide(record: ApprovalRecord, statuses: dict) -> object:
        return evaluate_approval(
            record,
            principal=principal,
            request=request,
            requested_envelope=envelope,
            approver_statuses=statuses,
            now=now,
        )

    self_issued = _decide(_record(subject, "self"), dict(context.approver_statuses))
    assert not self_issued.permitted
    assert "its own requester" in self_issued.reason

    # The control. Identical in every respect except who decided, so the refusal
    # above is attributable to self-issuance and to nothing else.
    other = "approver-" + uuid.uuid4().hex[:8]
    statuses = dict(context.approver_statuses)
    statuses[other] = ApproverStatus(
        subject=other,
        is_member=True,
        permissions=frozenset(status.permissions),
        revoked=False,
    )
    distinct = _decide(_record(other, "distinct"), statuses)
    assert distinct.permitted, (
        f"the control was refused for an unrelated reason: {distinct.reason!r} — "
        "so the refusal above does not establish the no-self-approval rule"
    )


async def test_a_revoked_workspace_grant_reports_revoked_rather_than_absent(authority):
    """`revoked=True` with `is_member=True`, never omission from the mapping.

    `harness_jobs/approval.py:254-257` names this precisely: modelling revocation as
    absence reads as "never had authority" when the truth is "had it and lost it",
    and only the second means a decision already taken under that authority must be
    re-examined. Pinned against a really-revoked row.

    Read through a workspace the principal is scoped to, because
    `_approver_statuses` keys its workspace-grant query on
    `principal.workspace_id` — asking about a different workspace would return no
    workspace grants at all and the assertion would pass for the wrong reason.
    """
    org_id, subject = await authority.organization()
    workspace_id = await authority.workspace(org_id)
    revoked_subject = "revoked-" + uuid.uuid4().hex[:8]
    await authority.grant(
        org_id, workspace_id, revoked_subject, "workspace:administer", revoked=True
    )
    await authority.grant(org_id, workspace_id, subject, "workspace:provision")

    principal = await authority.ask(org_id, subject, workspace_id)
    assert principal is not None
    context = await authority.resolver.approval_for(
        principal=principal, request=await _request()
    )

    status = context.approver_statuses.get(revoked_subject)
    assert status is not None, (
        "a revoked approver was omitted from the mapping, which the gate reads as "
        "'authority could not be established' rather than as a revocation"
    )
    assert status.revoked is True
    assert status.is_member is True
    assert not status.may_approve
