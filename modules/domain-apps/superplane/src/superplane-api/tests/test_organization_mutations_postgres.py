"""Organization mutation transactions and scope isolation through PostgreSQL/API."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select

from app.auth import VerifiedCaller
from app.cluster_authorization import authorized_cluster_ids
from app.config import settings
from app.main import app
from app.models.cluster import Cluster
from app.models.cluster_grant_scope import OrganizationGrantClusterScope
from app.models.event import Event
from app.models.organization import Organization
from app.models.organization_grant import (
    ORGANIZATION_ADMINISTER,
    ORGANIZATION_READ,
    OrganizationGrantRecord,
)
from app.models.organization_grant_change import OrganizationGrantChange
from app.models.workspace_grant import WorkspaceGrantRecord
from app.services import organization_access as service
from superplane_auth.policy import DomainPrincipal
from tests import test_organization_access_postgres as read_fixtures
from tests.test_auth import _mint

enforcing = read_fixtures.enforcing
installation_postgres_url = read_fixtures.installation_postgres_url
postgres_access = read_fixtures.postgres_access
organization_access = read_fixtures.organization_access
pytestmark = read_fixtures.pytestmark
rsa_keys = read_fixtures.rsa_keys
PATH = "/orgs/current/access/v1"


def assignment(**changes):
    return {
        "target_subject": "approver",
        "principal_type": "human",
        "permissions": [ORGANIZATION_READ],
        "expected_revision": 0,
        "request_id": str(uuid.uuid4()),
        "reason": "access_assignment",
    } | changes


def revocation(**changes):
    return {
        "target_subject": "approver",
        "principal_type": "human",
        "expected_revision": 1,
        "request_id": str(uuid.uuid4()),
        "reason": "access_revocation",
    } | changes


def revoke_path(grant_id):
    return f"{PATH}/grants/{grant_id}/revoke"


async def create_target(client, token, **changes):
    body = assignment(**changes)
    response = await client.post(f"{PATH}/grants", json=body, headers=token())
    assert response.status_code == 200, response.text
    return response.json(), body


async def test_assign_replace_revoke_preserves_provenance_and_independent_scopes(
    organization_access,
):
    client, sessions, org_id, workspace_id, members, token = organization_access
    granted, original = await create_target(client, token)
    assert granted["revision"] == 1 and granted["effective_permissions"] == [
        ORGANIZATION_READ
    ]
    assert (
        granted["organization_id"] == str(org_id) and granted["granted_by"] == "owner"
    )
    assert (
        granted["source"] == "explicit_assignment"
        and granted["request_id"] == original["request_id"]
    )
    assert granted["changed_at"] and granted["changed_by"] == "owner"
    assert (await client.get(f"{PATH}/me", headers=token("approver"))).json() == granted
    assert (
        await client.get(f"{PATH}/grants", headers=token("approver"))
    ).status_code == 403
    updated = await client.post(
        f"{PATH}/grants",
        json=assignment(expected_revision=1, permissions=[ORGANIZATION_ADMINISTER]),
        headers=token(),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    assert updated.json()["effective_permissions"] == [
        ORGANIZATION_ADMINISTER,
        ORGANIZATION_READ,
    ]
    assert updated.json()["granted_at"] == granted["granted_at"]
    assert (
        await client.get(f"/workspaces/{workspace_id}", headers=token("approver"))
    ).status_code == 403
    caller = VerifiedCaller(
        principal=DomainPrincipal(
            subject="approver",
            org_id=str(org_id),
            client_id="test-client",
            account_type="human",
        ),
        safe_headers={},
        source_org_id="adp-transaction-test",
    )
    async with sessions() as session:
        connection = await session.connection()
        await connection.run_sync(
            lambda sync: OrganizationGrantClusterScope.__table__.create(sync)
        )
        cluster = Cluster(
            org_id=org_id, name="explicit-cluster", status="Ready", sharing_enabled=True
        )
        session.add(cluster)
        await session.flush()
        assert (
            await authorized_cluster_ids(
                session, org_id, caller, "cluster:use", identity_reader=members
            )
            == frozenset()
        )
        child = OrganizationGrantClusterScope(
            org_id=org_id,
            grant_id=uuid.UUID(granted["grant_id"]),
            cluster_id=cluster.id,
            permissions="cluster:use",
            generation="a" * 64,
        )
        session.add(child)
        await session.commit()
        assert await authorized_cluster_ids(
            session, org_id, caller, "cluster:use", identity_reader=members
        ) == frozenset({cluster.id})
        child_id = child.id
    body = revocation(expected_revision=2)
    response = await client.post(
        revoke_path(granted["grant_id"]), json=body, headers=token()
    )
    assert response.status_code == 200, response.text
    removed = response.json()
    assert removed["revision"] == 3 and removed["effective_permissions"] == []
    assert removed["assigned_permissions"] == [ORGANIZATION_ADMINISTER]
    assert removed["source"] == "explicit_revocation" and removed["revoked_at"]
    assert removed["revocation_effect"] == "future_authority_only"
    assert (
        await client.post(f"{PATH}/grants", json=original, headers=token())
    ).status_code == 409
    assert (
        await client.post(
            f"{PATH}/grants", json=assignment(expected_revision=3), headers=token()
        )
    ).status_code == 409
    for path in (f"{PATH}/me", f"{PATH}/grants", "/orgs/current", "/events"):
        assert (await client.get(path, headers=token("approver"))).status_code == 403
    async with sessions() as session:
        assert (
            await authorized_cluster_ids(
                session, org_id, caller, "cluster:use", identity_reader=members
            )
            == frozenset()
        )
        assert (
            await session.get(OrganizationGrantClusterScope, child_id)
        ).revoked_at is None
        change = await session.scalar(
            select(OrganizationGrantChange).where(
                OrganizationGrantChange.request_id == uuid.UUID(body["request_id"])
            )
        )
        audit = await session.get(Event, change.event_id)
        details = json.loads(audit.details_json)
        assert (
            audit.org_id == org_id
            and audit.principal == "owner"
            and audit.action == "revoked"
        )
        assert audit.resource_id == uuid.UUID(granted["grant_id"]) and audit.created_at
        assert details == {
            "actor_type": "human",
            "target": "approver",
            "target_type": "human",
            "scope": "organization",
            "org_id": str(org_id),
            "before": updated.json()["effective_permissions"],
            "after": [],
            "reason": body["reason"],
            "request_id": body["request_id"],
            "before_revision": 2,
            "revision": 3,
            "revoked_at": datetime.fromisoformat(removed["revoked_at"]).isoformat(),
            "revocation_effect": "future_authority_only",
        }
    listing = await client.get(f"{PATH}/grants", headers=token())
    target = next(
        row
        for row in listing.json()["assignments"]
        if row["grant_id"] == granted["grant_id"]
    )
    assert target["source"] == "explicit_revocation" and target["revision"] == 3


@pytest.mark.parametrize("operation", ["assign", "revoke"])
async def test_duplicate_requests_replay_after_reconnect_and_conflict_on_changed_payload(
    organization_access, operation
):
    client, sessions, _, _, _, token = organization_access
    body, path = assignment(), f"{PATH}/grants"
    if operation == "revoke":
        target, _ = await create_target(client, token)
        body, path = revocation(), revoke_path(target["grant_id"])
    first, second = await asyncio.wait_for(
        asyncio.gather(
            client.post(path, json=body, headers=token()),
            client.post(path, json=body, headers=token()),
        ),
        timeout=15,
    )
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json() == second.json()
    await sessions.kw["bind"].dispose()
    replay = await client.post(path, json=body, headers=token())
    assert replay.status_code == 200 and replay.json() == first.json()
    assert (
        await client.post(path, json=body | {"expected_revision": 7}, headers=token())
    ).status_code == 409
    async with sessions() as session:
        assert len((await session.scalars(select(OrganizationGrantChange))).all()) == (
            1 if operation == "assign" else 2
        )


@pytest.mark.parametrize(
    "kind",
    [
        "viewer",
        "workspace-admin",
        "removed-actor",
        "removed-target",
        "foreign-target",
        "service-row",
        "self",
        "stale",
    ],
)
async def test_organization_assignment_denials_do_not_create_authority(
    organization_access, kind
):
    client, sessions, org_id, workspace_id, members, token = organization_access
    body, expected = assignment(), 403
    if kind == "removed-actor":
        members.active.remove("owner")
    elif kind == "removed-target":
        members.active.remove("approver")
    elif kind == "foreign-target":
        body["target_subject"] = "other-org-person"
    elif kind == "self":
        body["target_subject"] = "owner"
    elif kind == "stale":
        body["expected_revision"] = 1
        expected = 409
    else:
        async with sessions() as session:
            actor = await session.scalar(
                select(OrganizationGrantRecord).where(
                    OrganizationGrantRecord.principal == "owner"
                )
            )
            if kind == "service-row":
                session.add(
                    OrganizationGrantRecord(
                        org_id=org_id,
                        principal="approver",
                        principal_type="service",
                        permissions=ORGANIZATION_READ,
                        granted_by="fixture",
                    )
                )
                expected = 409
            elif kind == "viewer":
                actor.permissions = ORGANIZATION_READ
            else:
                await session.delete(actor)
                session.add(
                    WorkspaceGrantRecord(
                        workspace_id=workspace_id,
                        org_id=org_id,
                        principal="owner",
                        principal_type="human",
                        permissions="workspace:administer",
                    )
                )
            await session.commit()
    response = await client.post(f"{PATH}/grants", json=body, headers=token())
    assert response.status_code == expected, response.text
    async with sessions() as session:
        assert (await session.scalars(select(OrganizationGrantChange))).all() == []


@pytest.mark.parametrize(
    "changes",
    [
        {"principal_type": "service"},
        {"permissions": ["workspace:administer"]},
        {"permissions": ["cluster:use"]},
        {"permissions": ["unknown"]},
        {"permissions": []},
        {"permissions": [ORGANIZATION_READ, ORGANIZATION_READ]},
        {"preset": "org-admin"},
        {"reason": "unreviewed"},
        {"target_subject": "person@example.invalid"},
        {"expected_revision": True},
    ],
)
async def test_schema_rejects_service_presets_and_permission_widening(
    organization_access, changes
):
    client, _, _, _, _, token = organization_access
    response = await client.post(
        f"{PATH}/grants", json=assignment(**changes), headers=token()
    )
    assert response.status_code == 422, response.text


async def test_actor_and_operation_bound_replay_and_last_admin_gate(
    organization_access,
):
    client, sessions, org_id, _, members, token = organization_access
    target, original = await create_target(client, token)
    members.active.add("other-admin")
    async with sessions() as session:
        owner = await session.scalar(
            select(OrganizationGrantRecord).where(
                OrganizationGrantRecord.principal == "owner"
            )
        )
        owner_id = owner.id
        session.add(
            OrganizationGrantRecord(
                org_id=org_id,
                principal="other-admin",
                principal_type="human",
                permissions=ORGANIZATION_ADMINISTER,
                granted_by="fixture",
            )
        )
        await session.commit()
    assert (
        await client.post(f"{PATH}/grants", json=original, headers=token("other-admin"))
    ).status_code == 409
    assert (
        await client.post(
            revoke_path(target["grant_id"]),
            json=revocation(request_id=original["request_id"]),
            headers=token(),
        )
    ).status_code == 409
    response = await client.post(
        revoke_path(owner_id), json=revocation(target_subject="owner"), headers=token()
    )
    assert (
        response.status_code == 409
        and "pending last-administrator" in response.json()["detail"]
    )
    async with sessions() as session:
        assert (await session.get(OrganizationGrantRecord, owner_id)).revoked_at is None


@pytest.mark.parametrize("operation", ["assign", "revoke"])
@pytest.mark.parametrize("kind", ["downgrade", "revoke"])
async def test_stale_administrator_is_revalidated_after_middleware(
    organization_access, operation, kind
):
    client, sessions, _, _, members, token = organization_access
    body, path = assignment(), f"{PATH}/grants"
    if operation == "revoke":
        target, _ = await create_target(client, token)
        body, path = revocation(), revoke_path(target["grant_id"])
    members.target_read, members.continue_read = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(client.post(path, json=body, headers=token()))
    try:
        await asyncio.wait_for(members.target_read.wait(), timeout=5)
        async with sessions() as session:
            actor = await session.scalar(
                select(OrganizationGrantRecord).where(
                    OrganizationGrantRecord.principal == "owner"
                )
            )
            if kind == "revoke":
                actor.revoked_at = datetime.now(UTC)
            else:
                actor.permissions = ORGANIZATION_READ
            await session.commit()
    finally:
        members.continue_read.set()
    response = await asyncio.wait_for(pending, timeout=10)
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("operation", ["assign", "revoke"])
@pytest.mark.parametrize("subject", ["owner", "approver"])
async def test_current_membership_is_checked_again_before_commit(
    organization_access, monkeypatch, subject, operation
):
    client, sessions, _, _, members, token = organization_access
    path, body, existing_changes = f"{PATH}/grants", assignment(), 0
    if operation == "revoke":
        target, _ = await create_target(client, token)
        path, body, existing_changes = revoke_path(target["grant_id"]), revocation(), 1
    original, calls = service._require_current_pair, 0

    async def changed_membership(reader, caller, target_subject):
        nonlocal calls
        calls += 1
        if calls == 2:
            members.active.remove(subject)
        await original(reader, caller, target_subject)

    monkeypatch.setattr(service, "_require_current_pair", changed_membership)
    assert (await client.post(path, json=body, headers=token())).status_code == 403
    assert calls == 2
    async with sessions() as session:
        assert (
            len((await session.scalars(select(OrganizationGrantChange))).all())
            == existing_changes
        )


async def test_only_administrator_cannot_self_revoke_while_policy_is_pending(
    organization_access,
):
    client, sessions, _, _, _, token = organization_access
    async with sessions() as session:
        owner = await session.scalar(select(OrganizationGrantRecord))
        owner_id = owner.id
    response = await client.post(
        revoke_path(owner_id), json=revocation(target_subject="owner"), headers=token()
    )
    assert (
        response.status_code == 409
        and "pending last-administrator" in response.json()["detail"]
    )
    async with sessions() as session:
        owner = await session.get(OrganizationGrantRecord, owner_id)
        assert owner.revoked_at is None and owner.revision == 1
        assert (await session.scalars(select(OrganizationGrantChange))).all() == []


@pytest.mark.parametrize("operation", ["assign", "revoke"])
async def test_identity_reader_is_required_even_with_ingress_flag_disabled(
    organization_access, monkeypatch, operation
):
    client, _, _, _, _, token = organization_access
    path, body = f"{PATH}/grants", assignment()
    if operation == "revoke":
        target, _ = await create_target(client, token)
        path, body = revoke_path(target["grant_id"]), revocation()
    monkeypatch.setattr(settings, "current_identity_enforced", False)
    monkeypatch.setattr(app.state, "current_identity_reader", None)
    response = await client.post(path, json=body, headers=token())
    assert response.status_code == 503, response.text


async def test_assignment_evidence_does_not_invent_an_unaudited_revocation_actor(
    organization_access,
):
    client, sessions, _, _, _, token = organization_access
    target, original = await create_target(client, token)
    async with sessions() as session:
        stored = await session.get(
            OrganizationGrantRecord, uuid.UUID(target["grant_id"])
        )
        stored.revoked_at = datetime.now(UTC)
        await session.commit()
    response = await client.get(f"{PATH}/grants", headers=token())
    row = next(
        row
        for row in response.json()["assignments"]
        if row["grant_id"] == target["grant_id"]
    )
    assert row["revoked_at"] and row["source"] == "explicit_assignment"
    assert (
        row["reason"] == "access_assignment"
        and row["request_id"] == original["request_id"]
    )


async def test_concurrent_replacement_and_revocation_expose_conflict(
    organization_access,
):
    client, sessions, _, _, _, token = organization_access
    target, _ = await create_target(client, token)
    responses = await asyncio.wait_for(
        asyncio.gather(
            client.post(
                f"{PATH}/grants",
                json=assignment(
                    expected_revision=1, permissions=[ORGANIZATION_ADMINISTER]
                ),
                headers=token(),
            ),
            client.post(
                revoke_path(target["grant_id"]), json=revocation(), headers=token()
            ),
        ),
        timeout=15,
    )
    assert sorted(response.status_code for response in responses) == [200, 409], [
        response.text for response in responses
    ]
    async with sessions() as session:
        stored = await session.get(
            OrganizationGrantRecord, uuid.UUID(target["grant_id"])
        )
        assert stored.revision == 2
        assert bool(stored.revoked_at) == (responses[1].status_code == 200)
        assert len((await session.scalars(select(OrganizationGrantChange))).all()) == 2


async def test_concurrent_admin_removal_preserves_one_current_administrator(
    organization_access,
):
    client, sessions, _, _, _, token = organization_access
    target, _ = await create_target(
        client, token, permissions=[ORGANIZATION_ADMINISTER]
    )
    async with sessions() as session:
        owner = await session.scalar(
            select(OrganizationGrantRecord).where(
                OrganizationGrantRecord.principal == "owner"
            )
        )
        owner_id = owner.id
    results = await asyncio.wait_for(
        asyncio.gather(
            client.post(
                revoke_path(target["grant_id"]), json=revocation(), headers=token()
            ),
            client.post(
                revoke_path(owner_id),
                json=revocation(target_subject="owner"),
                headers=token("approver"),
            ),
        ),
        timeout=15,
    )
    assert sorted(result.status_code for result in results) == [200, 403], [
        result.text for result in results
    ]
    async with sessions() as session:
        assert (
            len(
                (
                    await session.scalars(
                        select(OrganizationGrantRecord).where(
                            OrganizationGrantRecord.revoked_at.is_(None)
                        )
                    )
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize("operation", ["create", "replace", "revoke"])
@pytest.mark.parametrize("store", [Event, OrganizationGrantChange])
async def test_audit_and_ledger_failure_roll_back_and_retry(
    organization_access, operation, store
):
    client, sessions, _, _, _, token = organization_access
    path, body, previous_count = f"{PATH}/grants", assignment(), 0
    if operation != "create":
        target, _ = await create_target(client, token)
        previous_count = 1
        if operation == "replace":
            body = assignment(
                expected_revision=1, permissions=[ORGANIZATION_ADMINISTER]
            )
        else:
            path, body = revoke_path(target["grant_id"]), revocation()

    def unavailable(mapper, connection, instance):
        if (
            isinstance(instance, OrganizationGrantChange)
            or instance.event_type == "organization_access"
        ):
            raise RuntimeError("organization persistence unavailable")

    event.listen(store, "before_insert", unavailable)
    try:
        with pytest.raises(RuntimeError, match="organization persistence unavailable"):
            await client.post(path, json=body, headers=token())
    finally:
        event.remove(store, "before_insert", unavailable)
    async with sessions() as session:
        assert (
            len((await session.scalars(select(OrganizationGrantChange))).all())
            == previous_count
        )
        assert (
            len(
                (
                    await session.scalars(
                        select(Event).where(Event.event_type == "organization_access")
                    )
                ).all()
            )
            == previous_count
        )
        stored = await session.scalar(
            select(OrganizationGrantRecord).where(
                OrganizationGrantRecord.principal == "approver"
            )
        )
        if previous_count:
            assert (
                stored.revoked_at is None
                and stored.revision == 1
                and stored.permissions == ORGANIZATION_READ
            )
        else:
            assert stored is None
    result = await client.post(path, json=body, headers=token())
    assert result.status_code == 200, result.text
    assert (await client.post(path, json=body, headers=token())).json() == result.json()


async def test_audit_visibility_and_foreign_grant_rejection(organization_access):
    client, sessions, _, _, _, token = organization_access
    target, _ = await create_target(client, token)
    async with sessions() as session:
        foreign_org = Organization(
            id=uuid.uuid4(), name="foreign", adp_org_id="foreign-adp"
        )
        session.add(foreign_org)
        await session.flush()
        foreign_grant = OrganizationGrantRecord(
            org_id=foreign_org.id,
            principal="approver",
            principal_type="human",
            permissions=ORGANIZATION_ADMINISTER,
            granted_by="fixture",
        )
        foreign_event = Event(
            org_id=foreign_org.id,
            principal="foreign-owner",
            action="assigned",
            resource_type="organization_grant",
            event_type="organization_access",
        )
        session.add_all([foreign_grant, foreign_event])
        await session.commit()
        foreign_id, event_id = foreign_grant.id, foreign_event.id
    for grant_id in (foreign_id, uuid.uuid4()):
        assert (
            await client.post(revoke_path(grant_id), json=revocation(), headers=token())
        ).status_code == 404
    response = await client.get(
        "/events?event_type=organization_access", headers=token("approver")
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["events"]) == 1
    audit = response.json()["events"][0]
    assert audit["resource_id"] == target["grant_id"]
    assert (await client.get(f"/events/{event_id}", headers=token())).status_code == 404
    async with sessions() as session:
        assert (
            await session.get(OrganizationGrantRecord, foreign_id)
        ).revoked_at is None


async def test_service_actor_and_missing_auth_cannot_mutate(
    organization_access, enforcing
):
    client, _, _, _, _, _ = organization_access
    assert (await client.post(f"{PATH}/grants", json=assignment())).status_code == 401
    token = _mint(
        enforcing,
        sub="owner",
        **{"custom:org_id": "adp-transaction-test", "custom:account_type": "service"},
    )
    assert (
        await client.post(
            f"{PATH}/grants",
            json=assignment(),
            headers={"Authorization": f"Bearer {token}"},
        )
    ).status_code == 403
