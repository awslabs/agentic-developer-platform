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
    with pytest.raises(IdentityDenied):
        await reader.read(subject="alice", principal_type="service", adp_org_id="O1")
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


async def test_composed_reader_resolves_each_selected_organization_without_union():
    import uuid

    from app.current_identity import MappedProducerIdentityReader
    from app.models.organization import Organization
    from tests.conftest import async_session_test

    domain_one, domain_two = uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add_all([
            Organization(id=domain_one, name="mapped-one", adp_org_id="O1"),
            Organization(id=domain_two, name="mapped-two", adp_org_id="O2"),
        ])
        await db.commit()

    class Producer:
        def __init__(self):
            self.calls = []

        async def post(self, route, payload, *, distinguish_denial=False):
            self.calls.append((route, payload, distinguish_denial))
            return dict(
                version=1, subject=payload["subject"], principal_type="human",
                adp_org_id="O1" if payload["org_id"] == str(domain_one) else "O2",
                membership_id="membership-" + payload["org_id"], active=True, enabled=True,
            )

    producer = Producer()
    reader = MappedProducerIdentityReader(producer, async_session_test)
    for adp_org, domain_org in (("O1", domain_one), ("O2", domain_two)):
        identity = await require_current_identity(
            reader, subject="same-human", principal_type="human", adp_org_id=adp_org
        )
        assert identity.membership_id == "membership-" + str(domain_org)
        assert producer.calls[-1] == (
            "/current-identity",
            {"domain": "superplane", "org_id": str(domain_org), "subject": "same-human", "principal_type": "human"},
            True,
        )
    with pytest.raises(IdentityDenied):
        await reader.read(subject="same-human", principal_type="human", adp_org_id="unmapped")
    with pytest.raises(IdentityDenied):
        await reader.read(subject="same-human", principal_type="service", adp_org_id="O1")
    assert len(producer.calls) == 2

    legacy_id = uuid.uuid4()
    async with async_session_test() as db:
        db.add_all([
            Organization(id=legacy_id, name="legacy-collision"),
            Organization(id=uuid.uuid4(), name="mapped-collision", adp_org_id=str(legacy_id)),
        ])
        await db.commit()
    with pytest.raises(IdentityDenied):
        await reader.read(subject="same-human", principal_type="human", adp_org_id=str(legacy_id))
    assert len(producer.calls) == 2


async def test_composed_reader_readiness_checks_signed_transport_and_shutdown(client, monkeypatch):
    from types import SimpleNamespace

    from app.composition import compose
    from app.config import settings
    from app.current_identity import MappedProducerIdentityReader
    from app.main import app
    from app.models.organization import Organization
    from tests.conftest import async_session_test
    from unittest.mock import AsyncMock
    import uuid

    configuration = SimpleNamespace(
        adp_gateway_internal_url="", adp_gateway_internal_api_key="", database_url="",
        superplane_db_schema="", superplane_operation_gateway_url="https://gateway.example",
        superplane_operation_gateway_region="us-east-1",
    )
    composition = compose(configuration)
    assert isinstance(composition.identity_reader, MappedProducerIdentityReader)
    organization_id = uuid.uuid4()
    async with async_session_test() as db:
        db.add(Organization(id=organization_id, name="ready-org", adp_org_id="O1"))
        await db.commit()
    composition.identity_reader.session_factory = async_session_test
    post = AsyncMock(return_value={
        "version": 1, "domain": "superplane", "org_id": str(organization_id), "adp_org_id": "O1"
    })
    monkeypatch.setattr(composition.identity_reader.transport, "post", post)
    monkeypatch.setattr(settings, "current_identity_enforced", True)
    monkeypatch.delattr(app.state, "current_identity_reader", raising=False)
    monkeypatch.setattr(composition.identity_reader.transport, "can_sign", lambda: False)
    try:
        composition.install_identity_reader(app)
        assert app.state.current_identity_reader is composition.identity_reader
        assert (await client.get("/readyz")).status_code == 503
        assert (await client.get("/health")).json()["current_identity_reader_configured"] is False
        monkeypatch.setattr(composition.identity_reader.transport, "can_sign", lambda: True)
        assert (await client.get("/readyz")).status_code == 200
        post.assert_awaited_once_with(
            "/current-identity/readiness",
            {"domain": "superplane", "org_id": str(organization_id)},
            distinguish_denial=True,
        )
        assert (await client.get("/health")).json()["current_identity_reader_configured"] is True
        post.side_effect = RuntimeError("offline")
        assert (await client.get("/readyz")).status_code == 503
        monkeypatch.setattr(composition.identity_reader.transport, "can_sign", lambda: False)
        assert (await client.get("/readyz")).status_code == 503
    finally:
        await composition.aclose()
    assert not hasattr(app.state, "current_identity_reader")


async def test_invalid_identity_transport_never_reports_ready(client, monkeypatch):
    from types import SimpleNamespace

    from app.composition import compose
    from app.config import settings
    from app.main import app

    configuration = SimpleNamespace(
        adp_gateway_internal_url="", adp_gateway_internal_api_key="", database_url="",
        superplane_db_schema="", superplane_operation_gateway_url="http://gateway.example",
        superplane_operation_gateway_region="us-east-1",
    )
    composition = compose(configuration)
    assert composition.identity_reader is None
    monkeypatch.delattr(app.state, "current_identity_reader", raising=False)
    monkeypatch.setattr(settings, "current_identity_enforced", True)
    assert (await client.get("/health")).json()["current_identity_reader_configured"] is False
    assert (await client.get("/readyz")).status_code == 503
    await composition.aclose()


async def test_producer_transport_readiness_requires_actual_sigv4_credentials():
    from types import SimpleNamespace

    from botocore.credentials import Credentials

    from app.adapters.operation_dispatch import ProducerTransport

    transport = ProducerTransport(
        "https://gateway.example", "us-east-1",
        session=SimpleNamespace(
            get_credentials=lambda: Credentials("fixture-access", "fixture-signing")
        ),
    )
    try:
        assert transport.can_sign() is True
        transport.session = SimpleNamespace(get_credentials=lambda: None)
        with pytest.raises(RuntimeError):
            transport.can_sign()
    finally:
        await transport.aclose()


async def test_producer_readiness_verifies_each_mapped_organization():
    import uuid

    from app.current_identity import MappedProducerIdentityReader
    from app.models.organization import Organization
    from tests.conftest import async_session_test

    organizations = [(uuid.uuid4(), "O1"), (uuid.uuid4(), "O2")]
    async with async_session_test() as db:
        db.add_all([
            Organization(id=domain_id, name=f"ready-{adp_id}", adp_org_id=adp_id)
            for domain_id, adp_id in organizations
        ])
        await db.commit()
    authorized = {str(domain_id): adp_id for domain_id, adp_id in organizations}
    calls = []

    class Producer:
        def can_sign(self):
            return True

        async def post(self, route, payload, *, distinguish_denial=False):
            assert route == "/current-identity/readiness" and distinguish_denial
            calls.append(payload["org_id"])
            return {
                "version": 1, "domain": "superplane", "org_id": payload["org_id"],
                "adp_org_id": authorized[payload["org_id"]],
            }

    reader = MappedProducerIdentityReader(Producer(), async_session_test)
    assert await reader.upstream_ready() is True
    assert set(calls) == set(authorized)
    authorized[str(organizations[1][0])] = "substituted"
    assert await reader.upstream_ready() is False
