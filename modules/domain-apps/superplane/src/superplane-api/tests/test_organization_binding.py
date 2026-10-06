import uuid

import pytest
from app.auth import VerifiedCaller
from app.models.organization import Organization
from app.organization_binding import bind_caller
from fastapi import HTTPException
from superplane_auth.policy import DomainPrincipal

from tests.conftest import async_session_test


def caller(org):
    return VerifiedCaller(
        DomainPrincipal("human-subject", org, "adp-client", "human"), {}
    )


async def test_explicit_slug_binding_is_isolated_and_retains_source():
    one, two = uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add_all(
            [
                Organization(id=one, name="one", adp_org_id="aws-e"),
                Organization(id=two, name="two", adp_org_id="another-org"),
            ]
        )
        await db.commit()
        bound = await bind_caller(db, caller("aws-e"))
        assert bound.principal.org_id == str(one)
        assert bound.source_org_id == "aws-e"
        assert (await bind_caller(db, caller("another-org"))).principal.org_id == str(
            two
        )
        with pytest.raises(HTTPException) as denied:
            await bind_caller(db, caller("unknown-org"))
        assert denied.value.status_code == 403
        with pytest.raises(HTTPException):
            await bind_caller(db, caller(str(one)))


async def test_unbound_legacy_uuid_behavior_survives():
    identifier = uuid.uuid4()
    async with async_session_test() as db:
        db.add(Organization(id=identifier, name="legacy"))
        await db.commit()
        original = caller(str(identifier))
        assert await bind_caller(db, original) == original


async def test_uuid_shaped_binding_cannot_select_a_different_legacy_tenant():
    legacy_id, mapped_id = uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add_all([
            Organization(id=legacy_id, name="legacy-ambiguous"),
            Organization(id=mapped_id, name="mapped-ambiguous", adp_org_id=str(legacy_id)),
        ])
        await db.commit()
        with pytest.raises(HTTPException) as denied:
            await bind_caller(db, caller(str(legacy_id)))
        assert denied.value.status_code == 403


async def test_bound_adp_request_uses_server_grant_and_rejects_uuid_bypass(client, monkeypatch):
    from app import auth
    from app.main import app
    from app.models.workspace import Workspace
    from app.models.workspace_grant import WorkspaceGrantRecord
    from app.current_identity import CurrentIdentity
    from superplane_auth.policy import DomainTokenPolicy

    org, workspace = uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add(Organization(id=org, name="fresh", adp_org_id="aws-e"))
        await db.flush()
        db.add(Workspace(id=workspace, org_id=org, name="fresh", isolation_mode="dedicated", status="Ready"))
        await db.flush()
        db.add(WorkspaceGrantRecord(workspace_id=workspace, org_id=org, principal="admin-subject", principal_type="human", permissions="workspace:administer"))
        await db.commit()
    claims = {"sub": "admin-subject", "custom:org_id": "aws-e", "custom:account_type": "human", "token_use": "access", "iss": "https://issuer.example", "client_id": "adp-client"}
    monkeypatch.setattr(auth, "verify_access_token", lambda token: claims)
    monkeypatch.setattr(app.state, "domain_policy", DomainTokenPolicy(allowed_client_ids=["adp-client"], expected_issuer="https://issuer.example"))
    headers = {"Authorization": "Bearer signed-token", "X-Org-Id": "spoofed"}
    from app.config import settings

    monkeypatch.delattr(app.state, "current_identity_reader", raising=False)
    monkeypatch.setattr(settings, "current_identity_enforced", False)
    # Upgrade compatibility: the existing signed-token/live-grant path still works.
    assert (await client.get(f"/workspaces/{workspace}", headers=headers)).status_code == 200
    monkeypatch.setattr(settings, "current_identity_enforced", True)
    assert (await client.get(f"/workspaces/{workspace}", headers=headers)).status_code == 503

    class Memberships:
        enabled = True

        async def read(self, *, subject, principal_type, adp_org_id):
            return CurrentIdentity(subject, principal_type, adp_org_id, "membership-1", True, self.enabled)
    reader = Memberships()
    monkeypatch.setattr(app.state, "current_identity_reader", reader, raising=False)
    assert (await client.get(f"/workspaces/{workspace}", headers=headers)).status_code == 200
    reader.enabled = False
    assert (await client.get(f"/workspaces/{workspace}", headers=headers)).status_code == 403
    reader.enabled = True
    assert (await client.get(f"/workspaces/{uuid.uuid4()}", headers=headers)).status_code == 403
    claims["custom:org_id"] = str(org)
    assert (await client.get(f"/workspaces/{workspace}", headers=headers)).status_code == 403


async def test_current_identity_enforcement_never_falls_back_without_domain_policy(client, monkeypatch):
    from app.config import settings
    from app.main import app

    monkeypatch.delattr(app.state, "domain_policy", raising=False)
    monkeypatch.setattr(settings, "current_identity_enforced", True)
    response = await client.get(f"/workspaces/{uuid.uuid4()}")
    assert response.status_code == 503


async def test_selected_organization_and_disjoint_human_grants_reach_only_own_workspace(client, monkeypatch):
    from app import auth
    from app.config import settings
    from app.current_identity import CurrentIdentity
    from app.main import app
    from app.models.workspace import Workspace
    from app.models.workspace_grant import WorkspaceGrantRecord
    from superplane_auth.policy import DomainTokenPolicy

    first_org, second_org = uuid.uuid4(), uuid.uuid4()
    first_workspace, peer_workspace, second_workspace = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add_all([
            Organization(id=first_org, name="two-humans-first", adp_org_id="O1"),
            Organization(id=second_org, name="two-humans-second", adp_org_id="O2"),
        ])
        await db.flush()
        db.add_all([
            Workspace(id=first_workspace, org_id=first_org, name="alice-first", isolation_mode="dedicated", status="Ready"),
            Workspace(id=peer_workspace, org_id=first_org, name="bob-first", isolation_mode="dedicated", status="Ready"),
            Workspace(id=second_workspace, org_id=second_org, name="alice-second", isolation_mode="dedicated", status="Ready"),
        ])
        await db.flush()
        db.add_all([
            WorkspaceGrantRecord(workspace_id=first_workspace, org_id=first_org, principal="alice", principal_type="human", permissions="workspace:read"),
            WorkspaceGrantRecord(workspace_id=peer_workspace, org_id=first_org, principal="bob", principal_type="human", permissions="workspace:read"),
            WorkspaceGrantRecord(workspace_id=second_workspace, org_id=second_org, principal="alice", principal_type="human", permissions="workspace:read"),
        ])
        await db.commit()

    selected = {"org": "O1"}
    monkeypatch.setattr(auth, "verify_access_token", lambda token: {
        "sub": token, "custom:org_id": selected["org"], "custom:account_type": "human",
        "token_use": "access", "iss": "https://issuer.example", "client_id": "adp-client",
    })
    monkeypatch.setattr(app.state, "domain_policy", DomainTokenPolicy(
        allowed_client_ids=["adp-client"], expected_issuer="https://issuer.example",
    ))
    monkeypatch.setattr(settings, "current_identity_enforced", True)

    class CurrentMemberships:
        def __init__(self):
            self.calls = []
            self.enabled = True

        async def read(self, *, subject, principal_type, adp_org_id):
            self.calls.append((subject, principal_type, adp_org_id))
            return CurrentIdentity(subject, principal_type, adp_org_id, f"member-{subject}-{adp_org_id}", True, self.enabled)

    reader = CurrentMemberships()
    monkeypatch.setattr(app.state, "current_identity_reader", reader, raising=False)

    async def status_for(subject, workspace):
        response = await client.get(f"/workspaces/{workspace}", headers={"Authorization": f"Bearer {subject}"})
        return response.status_code

    assert await status_for("alice", first_workspace) == 200
    assert await status_for("alice", peer_workspace) == 403
    assert await status_for("bob", peer_workspace) == 200
    assert await status_for("bob", first_workspace) == 403
    selected["org"] = "O2"
    assert await status_for("alice", second_workspace) == 200
    assert await status_for("alice", first_workspace) == 403
    assert await status_for("bob", second_workspace) == 403
    assert reader.calls == [
        ("alice", "human", "O1"), ("alice", "human", "O1"),
        ("bob", "human", "O1"), ("bob", "human", "O1"),
        ("alice", "human", "O2"), ("alice", "human", "O2"),
        ("bob", "human", "O2"),
    ]
    reader.enabled = False
    assert await status_for("alice", second_workspace) == 403
    assert reader.calls[-1] == ("alice", "human", "O2")


async def test_current_human_routes_refuse_service_and_monitor_credentials(client, monkeypatch):
    import json

    from app import auth
    from app.adapters.operation_authority_source import (
        ActingPrincipal,
        GrantBackedAuthority,
        reset_acting_principal,
        set_acting_principal,
    )
    from app.config import settings
    from app.current_identity import CurrentIdentity
    from app.main import app
    from app.models.workspace import Workspace
    from app.models.workspace_grant import WorkspaceGrantRecord
    from superplane_auth.policy import DomainTokenPolicy

    org_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add(Organization(id=org_id, name="typed-boundary", adp_org_id="selected-org"))
        await db.flush()
        db.add(Workspace(id=workspace_id, org_id=org_id, name="W1", isolation_mode="dedicated", status="Active"))
        await db.flush()
        db.add_all([
            WorkspaceGrantRecord(workspace_id=workspace_id, org_id=org_id, principal="alice", principal_type="human", permissions="workspace:read"),
            WorkspaceGrantRecord(workspace_id=workspace_id, org_id=org_id, principal="service-worker", principal_type="service", permissions="workspace:read"),
        ])
        await db.commit()

    monkeypatch.setattr(auth, "verify_access_token", lambda token: {
        "sub": token, "custom:org_id": "selected-org",
        "custom:account_type": "service" if token == "service-worker" else "human",
        "token_use": "access", "iss": "https://issuer.example", "client_id": "adp-client",
    })
    monkeypatch.setattr(app.state, "domain_policy", DomainTokenPolicy(
        allowed_client_ids=["adp-client"], expected_issuer="https://issuer.example",
    ), raising=False)
    monkeypatch.setattr(settings, "current_identity_enforced", True)

    class CurrentMemberships:
        def __init__(self):
            self.calls = []
            self.substitute_subject = None

        async def read(self, *, subject, principal_type, adp_org_id):
            self.calls.append((subject, principal_type, adp_org_id))
            return CurrentIdentity(
                self.substitute_subject or subject,
                principal_type, adp_org_id, "membership", True, True,
                "claimed-delegation" if principal_type == "service" else None,
            )

    reader = CurrentMemberships()
    monkeypatch.setattr(app.state, "current_identity_reader", reader, raising=False)
    service_headers = {"Authorization": "Bearer service-worker"}
    human_headers = {"Authorization": "Bearer alice"}
    assert (await client.get(f"/workspaces/{workspace_id}", headers=service_headers)).status_code == 403
    assert reader.calls == []
    assert (await client.get(f"/workspaces/{workspace_id}", headers=human_headers)).status_code == 200
    assert reader.calls == [("alice", "human", "selected-org")]
    reader.substitute_subject = "service-worker"
    assert (await client.get(f"/workspaces/{workspace_id}", headers=human_headers)).status_code == 403
    reader.substitute_subject = None

    token = set_acting_principal(ActingPrincipal(
        subject="service-worker", org_id=str(org_id), workspace_id=str(workspace_id),
        account_type="service", adp_org_id="selected-org", membership_id="membership",
        identity_reader=reader,
    ))
    try:
        assert await GrantBackedAuthority(async_session_test).resolve(
            org_id=str(org_id), workspace_id=str(workspace_id), permission="workspace:read",
        ) is None
    finally:
        reset_acting_principal(token)
    assert reader.calls == [("alice", "human", "selected-org")] * 2

    substituted = set_acting_principal(ActingPrincipal(
        subject="service-worker", org_id=str(org_id), workspace_id=str(workspace_id),
        account_type="human", adp_org_id="selected-org", membership_id="membership",
        identity_reader=reader,
    ))
    try:
        assert await GrantBackedAuthority(async_session_test).resolve(
            org_id=str(org_id), workspace_id=str(workspace_id), permission="workspace:read",
        ) is None
    finally:
        reset_acting_principal(substituted)

    monkeypatch.setattr(settings, "observation_submitters", json.dumps([{
        "submitter_id": "monitor", "credential": "monitor-only", "signing_key": "test-monitor-key",
        "workspaces": [str(workspace_id)],
    }]))
    assert (await client.get("/internal/observations/clusters", headers=human_headers)).status_code == 401
    assert (await client.get("/internal/observations/clusters", headers=service_headers)).status_code == 401
    assert (await client.get("/internal/observations/clusters", headers={"Authorization": "monitor-only"})).status_code == 200
