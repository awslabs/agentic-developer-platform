"""The ADP identity reader is required in addition to the domain grant."""

import pytest
from app.adapters.operation_authority_source import (
    ActingPrincipal,
    GrantBackedAuthority,
    reset_acting_principal,
    set_acting_principal,
)
from app.current_identity import (
    CurrentIdentity,
    IdentityDenied,
    IdentityUnavailable,
    ProducerIdentityReader,
    require_current_identity,
)


class ProducerFixture:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def post(self, route, payload, *, distinguish_denial=False):
        self.calls.append((route, payload, distinguish_denial))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_registered_producer_reader_checks_version_scope_and_membership():
    response = dict(
        version=1, subject="alice", principal_type="human", adp_org_id="O1",
        membership_id="member-one", active=True, enabled=True,
    )
    transport = ProducerFixture(response)
    reader = ProducerIdentityReader(transport, domain_org_id="domain-one", adp_org_id="O1")
    identity = await require_current_identity(reader, subject="alice", principal_type="human", adp_org_id="O1")
    assert identity.membership_id == "member-one"
    assert transport.calls == [(
        "/current-identity",
        {"domain": "superplane", "org_id": "domain-one", "subject": "alice", "principal_type": "human"},
        True,
    )]
    with pytest.raises(IdentityDenied):
        await reader.read(subject="alice", principal_type="human", adp_org_id="O2")
    assert len(transport.calls) == 1
    transport.result = {**response, "version": 2}
    with pytest.raises(IdentityUnavailable):
        await reader.read(subject="alice", principal_type="human", adp_org_id="O1")
    transport.result = {**response, "enabled": False}
    with pytest.raises(IdentityDenied):
        await require_current_identity(reader, subject="alice", principal_type="human", adp_org_id="O1")


@pytest.mark.parametrize("failure", ["denied", "unavailable"])
async def test_registered_producer_reader_distinguishes_denial_from_outage(failure):
    from app.adapters.operation_dispatch import ProducerRefusedError

    transport = ProducerFixture(ProducerRefusedError() if failure == "denied" else RuntimeError("offline"))
    reader = ProducerIdentityReader(transport, domain_org_id="domain-one", adp_org_id="O1")
    with pytest.raises(IdentityDenied if failure == "denied" else IdentityUnavailable):
        await require_current_identity(reader, subject="alice", principal_type="human", adp_org_id="O1")


class Reader:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def read(self, *, subject, principal_type, adp_org_id):
        self.calls += 1
        return self.result


@pytest.mark.parametrize(
    "result",
    [
        None,
        CurrentIdentity("alice", "human", "O2", "m1", True, True),
        CurrentIdentity("bob", "human", "O1", "m1", True, True),
        CurrentIdentity("alice", "service", "O1", "m1", True, True, "d1"),
        CurrentIdentity("alice", "human", "O1", "m1", False, True),
        CurrentIdentity("alice", "human", "O1", "m1", True, False),
        CurrentIdentity("alice", "human", "O1", "", True, True),
    ],
)
async def test_unproven_current_identity_is_refused(result):
    with pytest.raises(IdentityUnavailable):
        await require_current_identity(
            Reader(result), subject="alice", principal_type="human", adp_org_id="O1"
        )


async def test_no_reader_and_service_without_delegation_are_refused():
    with pytest.raises(IdentityUnavailable):
        await require_current_identity(
            None, subject="alice", principal_type="human", adp_org_id="O1"
        )
    with pytest.raises(IdentityUnavailable):
        await require_current_identity(
            Reader(CurrentIdentity("worker", "service", "O1", "m1", True, True)),
            subject="worker",
            principal_type="service",
            adp_org_id="O1",
        )


async def test_membership_changed_between_request_and_admission_is_refused():
    reader = Reader(CurrentIdentity("alice", "human", "O1", "m1", True, True))
    first = await require_current_identity(
        reader, subject="alice", principal_type="human", adp_org_id="O1"
    )
    reader.result = CurrentIdentity("alice", "human", "O1", "m2", True, True)
    with pytest.raises(IdentityUnavailable):
        await require_current_identity(
            reader,
            subject="alice",
            principal_type="human",
            adp_org_id="O1",
            membership_id=first.membership_id,
        )
    assert reader.calls == 2


async def test_operation_resolver_refuses_revoked_membership_before_grant_read(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "current_identity_enforced", True)
    reader = Reader(CurrentIdentity("alice", "human", "O1", "m1", True, False))

    from types import SimpleNamespace

    class BoundAuthority(GrantBackedAuthority):
        async def _read(self, what, query):
            assert what == "current identity organization"
            return SimpleNamespace(adp_org_id="O1")

        async def _workspace_permissions(self, **kwargs):
            raise AssertionError("no grant read or executor effect is allowed")

    token = set_acting_principal(
        ActingPrincipal(
            subject="alice",
            org_id="domain-org",
            workspace_id="workspace-1",
            adp_org_id="O1",
            membership_id="m1",
            identity_reader=reader,
        )
    )
    try:
        assert (
            await BoundAuthority(None).resolve(
                org_id="domain-org",
                workspace_id="workspace-1",
                permission="workspace:spend",
            )
            is None
        )
        assert reader.calls == 1
    finally:
        reset_acting_principal(token)


async def test_approver_grant_cannot_outlive_adp_membership(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "current_identity_enforced", True)
    from types import SimpleNamespace

    from harness_jobs.approval import ApproverStatus

    class BoundAuthority(GrantBackedAuthority):
        async def _read(self, what, query):
            return SimpleNamespace(adp_org_id="O1")

    reader = Reader(CurrentIdentity("approver", "human", "O1", "m1", False, True))
    token = set_acting_principal(
        ActingPrincipal(
            subject="requester",
            org_id="domain-org",
            workspace_id="workspace-1",
            adp_org_id="O1",
            membership_id="requester-membership",
            identity_reader=reader,
        )
    )
    try:
        statuses = await BoundAuthority(None)._current_approvers(
            "domain-org",
            {
                "approver": ApproverStatus(
                    "approver", True, frozenset({"workspace:administer"})
                )
            },
        )
        assert statuses["approver"].revoked
        assert not statuses["approver"].may_approve
    finally:
        reset_acting_principal(token)


async def test_enabled_worker_uses_stored_binding_and_current_evidence(monkeypatch):
    import uuid

    from app.config import settings
    from app.models.organization import Organization
    from app.models.workspace import Workspace
    from app.models.workspace_grant import WorkspaceGrantRecord
    from tests.conftest import async_session_test

    org, workspace = uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add(Organization(id=org, name="identity-worker", adp_org_id="O1"))
        db.add(Workspace(id=workspace, org_id=org, name="W1", status="Ready", isolation_mode="dedicated"))
        db.add(WorkspaceGrantRecord(
            org_id=org, workspace_id=workspace, principal="alice",
            principal_type="human", permissions="workspace:spend",
        ))
        await db.commit()

    monkeypatch.setattr(settings, "current_identity_enforced", True)
    authority = GrantBackedAuthority(async_session_test)
    reader = Reader(CurrentIdentity("alice", "human", "O1", "m1", True, True))

    async def resolve(**extra):
        token = set_acting_principal(ActingPrincipal(
            "alice", str(org), str(workspace), **extra
        ))
        try:
            return await authority.resolve(
                org_id=str(org), workspace_id=str(workspace), permission="workspace:spend"
            )
        finally:
            reset_acting_principal(token)

    # An ordinary old worker context cannot bypass the new check when enabled.
    assert await resolve() is None
    assert await resolve(adp_org_id="O2", membership_id="m1", identity_reader=reader) is None
    assert await resolve(adp_org_id="O1", identity_reader=reader) is None
    from app.adapters.operation_authority_source import _AuthorityUnreadable

    with pytest.raises(_AuthorityUnreadable):
        await resolve(adp_org_id="O1", membership_id="m1")
    assert await resolve(adp_org_id="O1", membership_id="m1", identity_reader=reader) is not None
    reader.result = CurrentIdentity("alice", "human", "O1", "m1", False, True)
    assert await resolve(adp_org_id="O1", membership_id="m1", identity_reader=reader) is None

    # Disabling the not-yet-composed integration preserves existing grant checks,
    # rather than marking current upstream membership as verified.
    monkeypatch.setattr(settings, "current_identity_enforced", False)
    assert await resolve() is not None


async def test_worker_identity_outage_is_unavailable_before_grant_read(monkeypatch):
    from types import SimpleNamespace

    from app.adapters.operation_authority_source import _AuthorityUnreadable
    from app.config import settings

    class UnreachableReader:
        async def read(self, **kwargs):
            raise RuntimeError("identity provider unavailable")

    class BoundAuthority(GrantBackedAuthority):
        async def _read(self, what, query):
            assert what == "current identity organization"
            return SimpleNamespace(adp_org_id="O1")

        async def _workspace_permissions(self, **kwargs):
            raise AssertionError("no grant lookup may follow an upstream outage")

    monkeypatch.setattr(settings, "current_identity_enforced", True)
    token = set_acting_principal(ActingPrincipal(
        "alice", "domain-org", "workspace-1",
        adp_org_id="O1", membership_id="m1", identity_reader=UnreachableReader(),
    ))
    try:
        with pytest.raises(_AuthorityUnreadable, match="current identity"):
            await BoundAuthority(None).resolve(
                org_id="domain-org", workspace_id="workspace-1", permission="workspace:spend",
            )
    finally:
        reset_acting_principal(token)


@pytest.mark.parametrize(
    ("granted", "expected"),
    [
        ("workspace:read", {"workspace:read"}),
        ("workspace:spend", {"workspace:read", "workspace:spend"}),
        ("workspace:provision", {"workspace:read", "workspace:provision"}),
        ("workspace:renew_credential", {"workspace:read", "workspace:renew_credential"}),
        ("workspace:administer", {
            "workspace:read", "workspace:spend", "workspace:provision",
            "workspace:renew_credential", "workspace:administer",
        }),
    ],
)
async def test_current_identity_and_explicit_grant_apply_same_implications_to_api_and_worker(monkeypatch, granted, expected):
    import uuid

    from fastapi import HTTPException
    from superplane_auth.policy import DomainPrincipal, Permission

    from app.auth import VerifiedCaller, authorize_workspace_operation
    from app.config import settings
    from app.models.organization import Organization
    from app.models.workspace import Workspace
    from app.models.workspace_grant import WorkspaceGrantRecord
    from tests.conftest import async_session_test

    org_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add(Organization(id=org_id, name=f"implication-{granted}", adp_org_id="O1"))
        await db.flush()
        db.add(Workspace(id=workspace_id, org_id=org_id, name="scoped", status="Ready", isolation_mode="dedicated"))
        await db.flush()
        db.add(WorkspaceGrantRecord(
            org_id=org_id, workspace_id=workspace_id, principal="alice",
            principal_type="human", permissions=granted,
        ))
        await db.commit()

    reader = Reader(CurrentIdentity("alice", "human", "O1", "m1", True, True))
    authority = GrantBackedAuthority(async_session_test)
    caller = VerifiedCaller(DomainPrincipal("alice", str(org_id), "adp-client", "human"), {})
    monkeypatch.setattr(settings, "current_identity_enforced", True)
    for required in Permission:
        async with async_session_test() as db:
            if required.value in expected:
                assert await authorize_workspace_operation(db, caller, workspace_id, required) is not None
            else:
                with pytest.raises(HTTPException) as denied:
                    await authorize_workspace_operation(db, caller, workspace_id, required)
                assert denied.value.status_code == 403
        token = set_acting_principal(ActingPrincipal(
            "alice", str(org_id), str(workspace_id), adp_org_id="O1",
            membership_id="m1", identity_reader=reader,
        ))
        try:
            result = await authority.resolve(
                org_id=str(org_id), workspace_id=str(workspace_id), permission=required.value,
            )
            assert (result is not None) == (required.value in expected)
        finally:
            reset_acting_principal(token)
    assert reader.calls == len(Permission)


async def test_revoked_workspace_grant_never_restores_first_workspace_authority(client, monkeypatch):
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.main import app
    from app.models.workspace import Workspace
    from app.models.workspace_grant import WorkspaceGrantRecord
    from tests.conftest import async_session_test
    from tests.test_organization_grants import seed

    org_id, _, headers = await seed(monkeypatch)
    workspace_id = uuid.uuid4()
    reader = Reader(CurrentIdentity("verified-human", "human", "selected-adp-org", "membership-1", True, True))
    monkeypatch.setattr(app.state, "current_identity_reader", reader)

    async def worker_resolution(authority, candidate):
        token = set_acting_principal(ActingPrincipal(
            "verified-human", str(org_id), str(candidate), adp_org_id="selected-adp-org",
            membership_id="membership-1", identity_reader=reader,
        ))
        try:
            return await authority.resolve(
                org_id=str(org_id), workspace_id=str(candidate), permission="workspace:provision",
            )
        finally:
            reset_acting_principal(token)

    authority = GrantBackedAuthority(async_session_test)
    assert (await client.get("/workspaces", headers=headers)).status_code == 200
    assert await worker_resolution(authority, workspace_id) is not None

    async with async_session_test() as db:
        db.add(Workspace(
            id=workspace_id, org_id=org_id, name="initial", status="Ready", isolation_mode="dedicated",
        ))
        await db.flush()
        db.add(WorkspaceGrantRecord(
            org_id=org_id, workspace_id=workspace_id, principal="verified-human",
            principal_type="human", permissions="workspace:provision",
        ))
        await db.commit()

    assert (await client.get(f"/workspaces/{workspace_id}", headers=headers)).status_code == 200
    assert await worker_resolution(authority, workspace_id) is not None

    async with async_session_test() as db:
        grant = await db.scalar(select(WorkspaceGrantRecord).where(
            WorkspaceGrantRecord.workspace_id == workspace_id,
        ))
        grant.revoked_at = datetime.now(UTC)
        await db.commit()

    assert (await client.get(f"/workspaces/{workspace_id}", headers=headers)).status_code == 403
    assert await worker_resolution(authority, workspace_id) is None
    restarted = GrantBackedAuthority(async_session_test)
    assert await worker_resolution(restarted, workspace_id) is None
    reader.result = CurrentIdentity("verified-human", "human", "selected-adp-org", "membership-1", False, True)
    assert await worker_resolution(restarted, uuid.uuid4()) is None
    assert reader.calls >= 5


async def test_identity_readiness_exposes_unconfigured_opt_in(client, monkeypatch):
    from app.config import settings
    from app.main import app

    monkeypatch.delattr(app.state, "current_identity_reader", raising=False)
    monkeypatch.setattr(settings, "current_identity_enforced", False)
    health = (await client.get("/health")).json()
    assert health["current_identity_required"] is False
    assert health["current_identity_reader_configured"] is False
    monkeypatch.setattr(settings, "current_identity_enforced", True)
    assert (await client.get("/readyz")).status_code == 503
    health = (await client.get("/health")).json()
    assert health["current_identity_required"] is True
    assert health["current_identity_reader_configured"] is False
