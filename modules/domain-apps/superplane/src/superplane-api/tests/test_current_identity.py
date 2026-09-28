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
    IdentityUnavailable,
    require_current_identity,
)


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
    assert await resolve(adp_org_id="O1", membership_id="m1") is None
    assert await resolve(adp_org_id="O1", membership_id="m1", identity_reader=reader) is not None
    reader.result = CurrentIdentity("alice", "human", "O1", "m1", False, True)
    assert await resolve(adp_org_id="O1", membership_id="m1", identity_reader=reader) is None

    # Disabling the not-yet-composed integration preserves existing grant checks,
    # rather than marking current upstream membership as verified.
    monkeypatch.setattr(settings, "current_identity_enforced", False)
    assert await resolve() is not None


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
