"""Maintained human revocation API, transactions and later authority on PostgreSQL."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select

from app.auth import VerifiedCaller, authorize_workspace_operation
from app.models.event import Event
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from app.models.workspace_grant_change import WorkspaceGrantChange
from app.services import workspace_access as service
from fastapi import HTTPException
from superplane_auth.policy import DomainPrincipal, Permission
from tests import test_workspace_access_postgres as workspace_fixtures
from tests.test_auth import _mint

enforcing = workspace_fixtures.enforcing
installation_postgres_url = workspace_fixtures.installation_postgres_url
postgres_access = workspace_fixtures.postgres_access
pytestmark = workspace_fixtures.pytestmark
rsa_keys = workspace_fixtures.rsa_keys


def revoke_request(**changes):
    body = {
        "target_subject": "approver",
        "principal_type": "human",
        "reason": "access_revocation",
        "expected_revision": 1,
        "request_id": str(uuid.uuid4()),
    }
    return body | changes


def revoke_path(workspace_id, grant_id):
    return f"/workspaces/{workspace_id}/access/v1/grants/{grant_id}/revoke"


@pytest.fixture
async def revocation_access(postgres_access):
    client, _, workspace_id, _, token = postgres_access
    body = workspace_fixtures.request(permissions=["workspace:administer"])
    response = await client.post(
        f"/workspaces/{workspace_id}/access/v1/grants", json=body, headers=token()
    )
    assert response.status_code == 200, response.text
    yield *postgres_access, response.json(), body


async def test_revocation_preserves_audited_tombstone_and_denies_later_authority(
    revocation_access,
):
    client, sessions, workspace_id, _, token, assigned, grant_request = (
        revocation_access
    )
    path = revoke_path(workspace_id, assigned["grant_id"])
    body = revoke_request()
    response = await client.post(path, json=body, headers=token())
    assert response.status_code == 200, response.text
    revoked = response.json()
    assert revoked["grant_id"] == assigned["grant_id"]
    assert revoked["workspace_id"] == str(workspace_id)
    assert revoked["revision"] == 2 and revoked["effective_permissions"] == []
    assert revoked["revoked_by"] == "owner" and revoked["subject"] == "approver"
    assert revoked["source"] == "explicit_revocation"
    assert revoked["request_id"] == body["request_id"]
    assert revoked["reason"] == body["reason"]
    assert revoked["revocation_effect"] == "future_authority_only"
    async with sessions() as session:
        target = await session.get(
            WorkspaceGrantRecord, uuid.UUID(assigned["grant_id"])
        )
        assert target.permissions == "workspace:administer" and target.revision == 2
        assert target.revoked_at == datetime.fromisoformat(revoked["revoked_at"])
        change = await session.scalar(
            select(WorkspaceGrantChange).where(
                WorkspaceGrantChange.request_id == uuid.UUID(body["request_id"])
            )
        )
        audit = await session.get(Event, change.event_id)
        details = json.loads(audit.details_json)
        assert audit.principal == "owner" and audit.org_id == target.org_id
        assert audit.outcome == "allowed" and audit.action == "revoked"
        assert (
            audit.resource_id == target.id and audit.resource_type == "workspace_grant"
        )
        assert audit.created_at is not None and audit.request_path == path
        assert details == {
            "actor_type": "human",
            "target": "approver",
            "target_type": "human",
            "scope": "workspace",
            "workspace_id": str(workspace_id),
            "org_id": str(target.org_id),
            "before": assigned["effective_permissions"],
            "after": [],
            "reason": "access_revocation",
            "request_id": body["request_id"],
            "before_revision": 1,
            "revision": 2,
            "revoked_at": target.revoked_at.isoformat(),
            "revocation_effect": "future_authority_only",
        }
        event_id = str(audit.id)
        caller = VerifiedCaller(
            principal=DomainPrincipal(
                subject="approver",
                org_id=str(target.org_id),
                account_type="human",
                client_id="fixture",
            ),
            safe_headers={},
        )
        for permission in Permission:
            with pytest.raises(HTTPException) as denied:
                await authorize_workspace_operation(
                    session, caller, workspace_id, permission
                )
            assert denied.value.status_code == 403
    for suffix in ("", "/access/v1/me", "/access/v1/grants"):
        assert (
            await client.get(
                f"/workspaces/{workspace_id}{suffix}", headers=token("approver")
            )
        ).status_code == 403
    assert (
        await client.post(
            f"/workspaces/{workspace_id}/kubeconfig", headers=token("approver")
        )
    ).status_code == 403
    assert (
        await client.post(
            f"/workspaces/{workspace_id}/access/v1/grants",
            json=grant_request,
            headers=token(),
        )
    ).status_code == 409
    assignments = await client.get(
        f"/workspaces/{workspace_id}/access/v1/grants", headers=token()
    )
    row = next(
        row
        for row in assignments.json()["assignments"]
        if row["grant_id"] == assigned["grant_id"]
    )
    assert row["revoked_at"] == revoked["revoked_at"] and row["revision"] == 2
    assert row["source"] == "explicit_revocation" and row["changed_by"] == "owner"
    assert (
        row["reason"] == "access_revocation" and row["request_id"] == body["request_id"]
    )
    stream = await client.get(f"/events/workspaces/{workspace_id}", headers=token())
    assert event_id in [row["id"] for row in stream.json()["events"]]
    assert (
        await client.get(
            f"/events/workspaces/{workspace_id}", headers=token("approver")
        )
    ).status_code == 403


async def test_duplicate_removal_replays_after_reconnect_without_extra_audit(
    revocation_access,
):
    client, sessions, workspace_id, _, token, assigned, _ = revocation_access
    path, body = revoke_path(workspace_id, assigned["grant_id"]), revoke_request()
    first, duplicate = await asyncio.wait_for(
        asyncio.gather(
            client.post(path, json=body, headers=token()),
            client.post(path, json=body, headers=token()),
        ),
        timeout=15,
    )
    assert first.status_code == duplicate.status_code == 200, (
        first.text,
        duplicate.text,
    )
    assert first.json() == duplicate.json()
    await sessions.kw["bind"].dispose()
    restarted = await client.post(path, json=body, headers=token())
    assert restarted.status_code == 200 and restarted.json() == first.json()
    assert (
        await client.post(
            path, json=revoke_request(expected_revision=2), headers=token()
        )
    ).status_code == 409
    assert (
        await client.post(path, json=body | {"expected_revision": 2}, headers=token())
    ).status_code == 409
    async with sessions() as session:
        assert len((await session.scalars(select(WorkspaceGrantChange))).all()) == 2
        assert (
            len(
                (
                    await session.scalars(
                        select(Event).where(
                            Event.event_type == "workspace_access",
                            Event.action == "revoked",
                        )
                    )
                ).all()
            )
            == 1
        )


async def test_assignment_request_identity_cannot_be_reused_for_removal(
    revocation_access,
):
    client, sessions, workspace_id, _, token, assigned, original = revocation_access
    response = await client.post(
        revoke_path(workspace_id, assigned["grant_id"]),
        json=revoke_request(request_id=original["request_id"]),
        headers=token(),
    )
    assert response.status_code == 409, response.text
    async with sessions() as session:
        assert (
            await session.get(WorkspaceGrantRecord, uuid.UUID(assigned["grant_id"]))
        ).revoked_at is None


async def test_removal_replay_is_actor_bound_and_requires_current_authority(
    revocation_access,
):
    client, sessions, workspace_id, members, token, assigned, _ = revocation_access
    members.active.add("other-admin")
    created = await client.post(
        f"/workspaces/{workspace_id}/access/v1/grants",
        json=workspace_fixtures.request(
            target_subject="other-admin", permissions=["workspace:administer"]
        ),
        headers=token(),
    )
    assert created.status_code == 200, created.text
    path, body = revoke_path(workspace_id, assigned["grant_id"]), revoke_request()
    assert (await client.post(path, json=body, headers=token())).status_code == 200
    assert (
        await client.post(path, json=body, headers=token("other-admin"))
    ).status_code == 409
    members.active.remove("owner")
    assert (await client.post(path, json=body, headers=token())).status_code == 403
    async with sessions() as session:
        target = await session.get(
            WorkspaceGrantRecord, uuid.UUID(assigned["grant_id"])
        )
        assert target.revoked_at is not None and target.revision == 2
        assert (
            len(
                (
                    await session.scalars(
                        select(Event).where(
                            Event.event_type == "workspace_access",
                            Event.action == "revoked",
                        )
                    )
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize(
    "kind",
    [
        "viewer",
        "member",
        "removed-actor",
        "removed-target",
        "cross-org-target",
        "service-row",
        "stale-revision",
    ],
)
async def test_unauthorized_or_stale_removal_never_changes_grant(
    revocation_access, kind
):
    client, sessions, workspace_id, members, token, assigned, _ = revocation_access
    body = revoke_request()
    expected = 403
    if kind == "removed-actor":
        members.active.remove("owner")
    elif kind == "removed-target":
        members.active.remove("approver")
    elif kind == "cross-org-target":
        body["target_subject"] = "other-org-person"
    elif kind == "stale-revision":
        body["expected_revision"] = 2
        expected = 409
    else:
        async with sessions() as session:
            if kind == "service-row":
                target = await session.get(
                    WorkspaceGrantRecord, uuid.UUID(assigned["grant_id"])
                )
                target.principal_type = "service"
                expected = 404
            else:
                actor = await session.scalar(
                    select(WorkspaceGrantRecord).where(
                        WorkspaceGrantRecord.principal == "owner"
                    )
                )
                actor.permissions = "workspace:read" if kind == "viewer" else ""
            await session.commit()
    response = await client.post(
        revoke_path(workspace_id, assigned["grant_id"]), json=body, headers=token()
    )
    assert response.status_code == expected, response.text
    async with sessions() as session:
        target = await session.get(
            WorkspaceGrantRecord, uuid.UUID(assigned["grant_id"])
        )
        assert target.revoked_at is None and target.revision == 1
        assert len((await session.scalars(select(WorkspaceGrantChange))).all()) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"principal_type": "service"},
        {"reason": "unreviewed"},
        {"expected_revision": 0},
        {"expected_revision": True},
        {"permissions": ["workspace:administer"]},
        {"target_subject": "person@example.invalid"},
    ],
)
async def test_removal_schema_rejects_substitution_and_unsupported_inputs(
    revocation_access, changes
):
    client, _, workspace_id, _, token, assigned, _ = revocation_access
    response = await client.post(
        revoke_path(workspace_id, assigned["grant_id"]),
        json=revoke_request(**changes),
        headers=token(),
    )
    assert response.status_code == 422, response.text


async def test_service_actor_and_unauthenticated_removal_are_refused(
    revocation_access, enforcing
):
    client, _, workspace_id, _, _, assigned, _ = revocation_access
    path = revoke_path(workspace_id, assigned["grant_id"])
    assert (await client.post(path, json=revoke_request())).status_code == 401
    token = _mint(
        enforcing,
        sub="owner",
        **{"custom:org_id": "adp-transaction-test", "custom:account_type": "service"},
    )
    assert (
        await client.post(
            path, json=revoke_request(), headers={"Authorization": f"Bearer {token}"}
        )
    ).status_code == 403


async def test_foreign_or_other_workspace_grant_is_not_a_removal_target(
    revocation_access,
):
    client, sessions, workspace_id, members, token, _, _ = revocation_access
    members.active.add("foreign-target")
    async with sessions() as session:
        org_id = (await session.get(Workspace, workspace_id)).org_id
        foreign_org = Organization(
            id=uuid.uuid4(), name="foreign", adp_org_id="foreign-adp"
        )
        session.add(foreign_org)
        await session.flush()
        other_workspace = Workspace(
            id=uuid.uuid4(), org_id=org_id, name="other", isolation_mode="research"
        )
        foreign_workspace = Workspace(
            id=uuid.uuid4(),
            org_id=foreign_org.id,
            name="foreign",
            isolation_mode="research",
        )
        session.add_all([other_workspace, foreign_workspace])
        await session.flush()
        grants = [
            WorkspaceGrantRecord(
                workspace_id=workspace.id,
                org_id=workspace.org_id,
                principal="foreign-target",
                principal_type="human",
                permissions="workspace:read",
            )
            for workspace in (other_workspace, foreign_workspace)
        ]
        session.add_all(grants)
        await session.commit()
    responses = [
        await client.post(
            revoke_path(workspace_id, grant_id),
            json=revoke_request(target_subject="foreign-target"),
            headers=token(),
        )
        for grant_id in [*(grant.id for grant in grants), uuid.uuid4()]
    ]
    assert all(response.status_code == 404 for response in responses)
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert (
        await client.post(
            revoke_path(foreign_workspace.id, grants[1].id),
            json=revoke_request(target_subject="foreign-target"),
            headers=token(),
        )
    ).status_code == 403


@pytest.mark.parametrize("kind", ["revoked", "downgraded"])
async def test_removal_rechecks_actor_after_middleware(revocation_access, kind):
    client, sessions, workspace_id, members, token, assigned, _ = revocation_access
    members.target_read, members.continue_read = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(
        client.post(
            revoke_path(workspace_id, assigned["grant_id"]),
            json=revoke_request(),
            headers=token(),
        )
    )
    try:
        await asyncio.wait_for(members.target_read.wait(), timeout=5)
        async with sessions() as session:
            actor = await session.scalar(
                select(WorkspaceGrantRecord).where(
                    WorkspaceGrantRecord.principal == "owner"
                )
            )
            if kind == "revoked":
                actor.revoked_at = datetime.now(UTC)
            else:
                actor.permissions = "workspace:read"
            await session.commit()
    finally:
        members.continue_read.set()
    response = await asyncio.wait_for(pending, timeout=10)
    assert response.status_code == 403, response.text
    async with sessions() as session:
        assert (
            await session.get(WorkspaceGrantRecord, uuid.UUID(assigned["grant_id"]))
        ).revoked_at is None


@pytest.mark.parametrize("subject", ["owner", "approver"])
async def test_removal_rechecks_membership_immediately_before_mutation(
    revocation_access, monkeypatch, subject
):
    client, sessions, workspace_id, members, token, assigned, _ = revocation_access
    original = service._require_current_pair
    calls = 0

    async def membership_changed(reader, caller, target_subject):
        nonlocal calls
        calls += 1
        if calls == 2:
            members.active.remove(subject)
        await original(reader, caller, target_subject)

    monkeypatch.setattr(service, "_require_current_pair", membership_changed)
    response = await client.post(
        revoke_path(workspace_id, assigned["grant_id"]),
        json=revoke_request(),
        headers=token(),
    )
    assert response.status_code == 403, response.text
    assert calls == 2
    async with sessions() as session:
        assert (
            await session.get(WorkspaceGrantRecord, uuid.UUID(assigned["grant_id"]))
        ).revoked_at is None


async def test_concurrent_grant_and_removal_expose_revision_conflict(revocation_access):
    client, sessions, workspace_id, _, token, assigned, _ = revocation_access
    path = revoke_path(workspace_id, assigned["grant_id"])
    removed, changed = await asyncio.wait_for(
        asyncio.gather(
            client.post(path, json=revoke_request(), headers=token()),
            client.post(
                f"/workspaces/{workspace_id}/access/v1/grants",
                json=workspace_fixtures.request(
                    expected_revision=1, permissions=["workspace:spend"]
                ),
                headers=token(),
            ),
        ),
        timeout=15,
    )
    assert sorted([removed.status_code, changed.status_code]) == [200, 409], (
        removed.text,
        changed.text,
    )
    async with sessions() as session:
        target = await session.get(
            WorkspaceGrantRecord, uuid.UUID(assigned["grant_id"])
        )
        assert target.revision == 2
        assert (target.revoked_at is not None) == (removed.status_code == 200)
        assert len((await session.scalars(select(WorkspaceGrantChange))).all()) == 2
    if changed.status_code == 200:
        assert (
            await client.post(
                path, json=revoke_request(expected_revision=2), headers=token()
            )
        ).status_code == 200
    assert (
        await client.get(
            f"/workspaces/{workspace_id}/access/v1/me", headers=token("approver")
        )
    ).status_code == 403


async def test_concurrent_administrators_cannot_remove_each_other(revocation_access):
    client, sessions, workspace_id, _, token, assigned, _ = revocation_access
    async with sessions() as session:
        owner = await session.scalar(
            select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.principal == "owner"
            )
        )
        owner_id = owner.id
    responses = await asyncio.wait_for(
        asyncio.gather(
            client.post(
                revoke_path(workspace_id, assigned["grant_id"]),
                json=revoke_request(),
                headers=token(),
            ),
            client.post(
                revoke_path(workspace_id, owner_id),
                json=revoke_request(target_subject="owner"),
                headers=token("approver"),
            ),
        ),
        timeout=15,
    )
    assert sorted(response.status_code for response in responses) == [200, 403], [
        response.text for response in responses
    ]
    async with sessions() as session:
        live = (
            await session.scalars(
                select(WorkspaceGrantRecord).where(
                    WorkspaceGrantRecord.revoked_at.is_(None)
                )
            )
        ).all()
        assert len(live) == 1 and live[0].permissions == "workspace:administer"


async def test_last_administrator_self_removal_has_explicit_pending_policy_gate(
    postgres_access,
):
    client, sessions, workspace_id, _, token = postgres_access
    async with sessions() as session:
        owner = await session.scalar(select(WorkspaceGrantRecord))
        owner_id = owner.id
    response = await client.post(
        revoke_path(workspace_id, owner_id),
        json=revoke_request(target_subject="owner"),
        headers=token(),
    )
    assert (
        response.status_code == 409
        and "pending last-administrator" in response.json()["detail"]
    )
    async with sessions() as session:
        owner = await session.get(WorkspaceGrantRecord, owner_id)
        assert owner.revoked_at is None and owner.revision == 1
        assert (await session.scalars(select(WorkspaceGrantChange))).all() == []


@pytest.mark.parametrize("failed_store", ["audit", "ledger"])
async def test_removal_audit_failure_rolls_back_and_identical_retry_succeeds(
    revocation_access, failed_store
):
    client, sessions, workspace_id, _, token, assigned, _ = revocation_access
    path, body = revoke_path(workspace_id, assigned["grant_id"]), revoke_request()
    store = Event if failed_store == "audit" else WorkspaceGrantChange

    def unavailable_store(mapper, connection, instance):
        if isinstance(instance, WorkspaceGrantChange) or (
            instance.event_type == "workspace_access" and instance.action == "revoked"
        ):
            raise RuntimeError("revocation persistence unavailable")

    event.listen(store, "before_insert", unavailable_store)
    try:
        with pytest.raises(RuntimeError, match="revocation persistence unavailable"):
            await client.post(path, json=body, headers=token())
    finally:
        event.remove(store, "before_insert", unavailable_store)
    async with sessions() as session:
        target = await session.get(
            WorkspaceGrantRecord, uuid.UUID(assigned["grant_id"])
        )
        assert target.revoked_at is None and target.revision == 1
        assert len((await session.scalars(select(WorkspaceGrantChange))).all()) == 1
        assert (
            await session.scalars(
                select(Event).where(
                    Event.event_type == "workspace_access", Event.action == "revoked"
                )
            )
        ).all() == []
    response = await client.post(path, json=body, headers=token())
    assert response.status_code == 200 and response.json()["revision"] == 2
    assert (
        await client.post(path, json=body, headers=token())
    ).json() == response.json()
