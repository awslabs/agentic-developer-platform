"""Immutable change references and authorized registry readback."""

import asyncio
import importlib.util
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from src.auth.dependencies import get_current_user
from src.products.models import ProductChange, Project
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member, select_creation_team


@pytest.mark.asyncio
async def changefeed_current_access(test_client, db_session, app, valid_token_context):
    from src.admin.team_memberships import remove_membership
    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import Team, TeamMembership

    owner = valid_token_context
    db_session.add(Organization(id=owner.org_id, name="Access fixture"))
    db_session.add(User(id=owner.user_id, org_id=owner.org_id, team_id=owner.team_id, email="access-owner@example.test", user_kind="human"))
    await add_team_member(db_session, owner)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: owner
    await select_creation_team(db_session, owner, "initial-owner-team")
    created = await test_client.post(
        "/v1/products",
        json={"name": "Shared", "purpose": "Access", "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"}},
        headers={"Idempotency-Key": "scenario_access_create_01"},
    )
    assert created.status_code == 201, created.text
    product_id, first_project = created.json()["product_id"], created.json()["project_id"]
    second_project = "access-second-project"
    editor = owner.model_copy(update={"user_id": "access-team-editor", "team_id": "access-editor-team"})
    db_session.add_all(
        [
            Project(id=second_project, tenant_id=owner.org_id, name="Second", owner_user_id=owner.user_id),
            User(id=editor.user_id, org_id=owner.org_id, team_id=editor.team_id, email="access-editor@example.test", user_kind="human"),
            Team(id=editor.team_id, org_id=owner.org_id, department_id="test", name="Access editor team"),
            TenantMembership(id="access-owner-admin", tenant_id=owner.org_id, user_id=owner.user_id, role="org_admin"),
            TenantMembership(id="access-editor-member", tenant_id=owner.org_id, user_id=editor.user_id, role="member"),
        ]
    )
    await db_session.commit()
    db_session.add_all(
        [
            TeamMembership(user_id=editor.user_id, org_id=owner.org_id, team_id=editor.team_id, is_primary=True),
            TeamMembership(user_id=editor.user_id, org_id=owner.org_id, team_id=owner.team_id, is_primary=False),
        ]
    )
    await db_session.commit()
    bound = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"project_id": second_project},
        headers={"Idempotency-Key": "scenario_access_bind_01", "If-Profile-Revision": "1"},
    )
    assert bound.status_code == 200, bound.text
    first_path = f"/v1/products/{product_id}/projects/{first_project}"
    second_path = f"/v1/products/{product_id}/projects/{second_project}"
    mapped = await test_client.patch(
        first_path,
        json={"team_id": owner.team_id},
        headers={"Idempotency-Key": "scenario_access_share_01", "If-Profile-Revision": "2"},
    )
    assert mapped.status_code == 200, mapped.text

    app.dependency_overrides[get_current_user] = lambda: editor
    first_feed = await test_client.get(f"{first_path}/changes")
    second_feed = await test_client.get(f"{second_path}/changes?limit=1")
    assert first_feed.status_code == second_feed.status_code == 200
    assert [item["revision"] for item in first_feed.json()["items"]] == [1, 3]
    assert [item["revision"] for item in second_feed.json()["items"]] == [2]
    assert first_project not in second_feed.text and second_project not in first_feed.text
    assert second_feed.json()["next_cursor"]
    cursor = second_feed.json()["next_cursor"]
    assert (await test_client.get(f"{first_path}/changes?limit=1&cursor={cursor}")).status_code == 400
    assert (await test_client.get(f"{first_path}/revisions/2")).status_code == 404
    assert (await test_client.get(f"{second_path}/revisions/2")).json()["project_id"] == second_project

    await remove_membership(db_session, user_id=editor.user_id, team_id=owner.team_id, org_id=owner.org_id)
    await db_session.commit()
    for path in (first_path, second_path):
        for suffix in ("/changes", "/revisions/2"):
            denied = await test_client.get(path + suffix)
            assert denied.status_code == 404
            assert first_project not in denied.text and second_project not in denied.text
    denied_cursor = await test_client.get(f"{second_path}/changes?limit=1&cursor={cursor}")
    assert denied_cursor.status_code == 404 and second_project not in denied_cursor.text
    app.dependency_overrides[get_current_user] = lambda: owner
    assert (await test_client.get(f"{second_path}/revisions/2")).status_code == 200
    changes = (await db_session.scalars(select(ProductChange).where(ProductChange.product_id == product_id))).all()
    assert len(changes) == 3


@pytest.mark.asyncio
async def changefeed_immutable_refs(test_client, db_session, app, valid_token_context, tmp_path):
    from test_i04 import GatewayClient

    actor = valid_token_context
    db_session.add(Organization(id=actor.org_id, name="CLI readback fixture"))
    db_session.add(User(id=actor.user_id, org_id=actor.org_id, team_id=actor.team_id, email="cli-readback@example.test", user_kind="human"))
    await add_team_member(db_session, actor)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    created = await test_client.post(
        "/v1/products",
        json={"name": "Before", "purpose": "History", "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"}},
        headers={"Idempotency-Key": "scenario_history_create_01"},
    )
    assert created.status_code == 201, created.text
    product_id, first_project = created.json()["product_id"], created.json()["project_id"]
    second_project = "scenario-second-project"
    db_session.add(Project(id=second_project, tenant_id=actor.org_id, name="Second", owner_user_id=actor.user_id))
    await db_session.commit()
    bound = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"project_id": second_project},
        headers={"Idempotency-Key": "scenario_history_bind_01", "If-Profile-Revision": "1"},
    )
    assert bound.status_code == 200, bound.text
    first_path = f"/v1/products/{product_id}/projects/{first_project}"
    edited = await test_client.patch(
        first_path,
        json={"name": "After"},
        headers={"Idempotency-Key": "scenario_history_edit_01", "If-Profile-Revision": "2"},
    )
    assert edited.status_code == 200, edited.text
    second_path = f"/v1/products/{product_id}/projects/{second_project}"
    closed = await test_client.delete(
        second_path,
        headers={"Idempotency-Key": "scenario_history_close_01", "If-Profile-Revision": "3"},
    )
    assert closed.status_code == 200, closed.text

    cli_dir = Path(__file__).parents[2] / "cli"
    prefix = tmp_path / "bin"
    installed = subprocess.run(
        ["bash", str(cli_dir / "install.sh"), "--prefix", str(prefix), "--gateway-url", "https://gw.test", "--no-path-edit"],
        capture_output=True,
        text=True,
        timeout=120,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert installed.returncode == 0, installed.stderr
    spec = importlib.util.spec_from_file_location("installed_history", prefix / "adp-product.py")
    installed_cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installed_cli)
    client = GatewayClient(test_client, asyncio.get_running_loop())
    scope = ("--product", product_id, "--project", second_project)

    async def invoke(*arguments):
        parsed = installed_cli.parser().parse_args(arguments)
        return (await asyncio.to_thread(installed_cli.execute, parsed, client))["detail"]

    cursor = None
    observed = []
    for _ in range(3):
        arguments = ("history", *scope, "--limit", "1")
        page = await invoke(*arguments, *(("--cursor", cursor) if cursor else ()))
        assert len(page["items"]) == 1
        observed.extend(page["items"])
        cursor = page["next_cursor"]
    assert cursor is None
    assert [change["revision"] for change in observed] == [2, 3, 4]
    independent = (await test_client.get(f"{second_path}/changes")).json()["items"]
    assert independent == observed
    for change, expected_name in zip(observed, ("Before", "After", "After")):
        cli_revision = await invoke("show", *scope, "--revision", str(change["revision"]))
        api_revision = await test_client.get(f"{second_path}/revisions/{change['revision']}")
        assert api_revision.status_code == 200 and cli_revision == api_revision.json()
        assert cli_revision["project_id"] == second_project and cli_revision["product"]["name"] == expected_name
    for change in (observed[0], observed[2]):
        relationship = change["relationship_versions"][0]
        assert relationship["from_ref"]["record_id"] == product_id
        assert relationship["to_ref"]["record_id"] == second_project
        assert relationship["from_ref"]["record_revision"] == change["event_id"]
        assert relationship["valid_from"] and relationship["mapping_version"] == "profile-registry-1"
    assert observed[0]["relationship_versions"][0]["relationship_revision"] == "1"
    assert observed[2]["relationship_versions"][0]["relationship_revision"] == "2"
    assert "valid_to" not in observed[0]["relationship_versions"][0]
    assert "valid_to" in observed[2]["relationship_versions"][0]


@pytest.mark.asyncio
async def test_binding_versions_are_immutable(test_client, db_session, app, valid_token_context):
    actor = valid_token_context
    db_session.add(Organization(id=actor.org_id, name="History fixture"))
    db_session.add(User(id=actor.user_id, org_id=actor.org_id, team_id=actor.team_id, email="history@example.test", user_kind="human"))
    await add_team_member(db_session, actor)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    created = await test_client.post(
        "/v1/products",
        json={
            "name": "History",
            "purpose": "Immutable history",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "history_create_fixture_01"},
    )
    assert created.status_code == 201, created.text
    product_id = created.json()["product_id"]
    initial_project = created.json()["project_id"]
    second_project = "history-second-project"
    db_session.add(Project(id=second_project, tenant_id=actor.org_id, name="Second scope", owner_user_id=actor.user_id))
    await db_session.commit()
    bound = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"project_id": second_project},
        headers={"Idempotency-Key": "history_bind_fixture_01", "If-Profile-Revision": "1"},
    )
    assert bound.status_code == 200, bound.text
    closed = await test_client.delete(
        f"/v1/products/{product_id}/projects/{second_project}",
        headers={"Idempotency-Key": "history_close_fixture_01", "If-Profile-Revision": "2"},
    )
    assert closed.status_code == 200, closed.text
    changes = (await db_session.scalars(select(ProductChange).where(ProductChange.product_id == product_id).order_by(ProductChange.revision))).all()
    assert [change.revision for change in changes] == [1, 2, 3]
    for change, project_id, relationship_revision in zip(changes, (initial_project, second_project, second_project), ("1", "1", "2")):
        relationship = change.relationship_versions[0]
        assert relationship["relationship_type"] == "product_project"
        assert relationship["relationship_revision"] == relationship_revision
        assert relationship["mapping_version"] == "profile-registry-1"
        assert relationship["from_ref"]["record_id"] == product_id
        assert relationship["to_ref"]["record_id"] == project_id
        assert relationship["from_ref"]["record_revision"] == change.id
        assert relationship["to_ref"]["record_revision"] == change.id
        assert relationship["source_refs"] == [relationship["from_ref"], relationship["to_ref"]]
        assert relationship["recorded_at"].endswith("Z")
        if relationship_revision == "1":
            assert "valid_to" not in relationship
        else:
            assert "valid_to" in relationship
    assert changes[1].relationship_versions[0]["relationship_id"] == changes[2].relationship_versions[0]["relationship_id"]
    assert changes[2].relationship_versions[0]["supersedes_revision"] == "1"
    assert changes[1].relationship_versions[0]["valid_from"] == changes[2].relationship_versions[0]["valid_from"]
    assert changes[1].snapshot["project_id"] == second_project


@pytest.mark.asyncio
async def test_scoped_history_readback_survives_rebind(test_client, db_session, app, valid_token_context):
    actor = valid_token_context
    db_session.add(Organization(id=actor.org_id, name="Readback fixture"))
    db_session.add(User(id=actor.user_id, org_id=actor.org_id, team_id=actor.team_id, email="readback@example.test", user_kind="human"))
    await add_team_member(db_session, actor)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    created = await test_client.post(
        "/v1/products",
        json={
            "name": "Before",
            "purpose": "History",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "readback_create_fixture_01"},
    )
    assert created.status_code == 201, created.text
    product_id, first_project = created.json()["product_id"], created.json()["project_id"]
    second_project = "readback-second-project"
    db_session.add(Project(id=second_project, tenant_id=actor.org_id, name="Second scope", owner_user_id=actor.user_id))
    await db_session.commit()
    path = f"/v1/products/{product_id}/projects/{second_project}"
    bound = await test_client.post(
        f"/v1/products/{product_id}/projects",
        json={"project_id": second_project},
        headers={"Idempotency-Key": "readback_bind_fixture_01", "If-Profile-Revision": "1"},
    )
    assert bound.status_code == 200, bound.text
    edited = await test_client.patch(
        path,
        json={"name": "After"},
        headers={"Idempotency-Key": "readback_edit_fixture_01", "If-Profile-Revision": "2"},
    )
    assert edited.status_code == 200, edited.text
    closed = await test_client.delete(
        path,
        headers={"Idempotency-Key": "readback_close_fixture_01", "If-Profile-Revision": "3"},
    )
    assert closed.status_code == 200, closed.text
    feed = await test_client.get(f"{path}/changes")
    assert feed.status_code == 200, feed.text
    assert feed.headers["cache-control"] == "no-store"
    assert [item["revision"] for item in feed.json()["items"]] == [2, 3, 4]
    bound_version, edited_change, closed_version = feed.json()["items"]
    assert bound_version["relationship_versions"][0]["relationship_revision"] == "1"
    assert edited_change["relationship_versions"] == []
    assert closed_version["relationship_versions"][0]["relationship_revision"] == "2"
    assert bound_version["event_id"] != closed_version["event_id"]
    assert bound_version["recorded_at"].endswith("Z")
    for revision, expected_name in ((2, "Before"), (3, "After"), (4, "After")):
        readback = await test_client.get(f"{path}/revisions/{revision}")
        assert readback.status_code == 200, readback.text
        assert readback.headers["profile-revision"] == str(revision)
        assert readback.json()["project_id"] == second_project
        assert readback.json()["product"]["name"] == expected_name
    first_readback = await test_client.get(f"/v1/products/{product_id}/projects/{first_project}/revisions/3")
    assert first_readback.status_code == 200, first_readback.text
    assert first_readback.json()["project_id"] == first_project
    assert first_readback.json()["product"]["name"] == "After"
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{first_project}/revisions/2")).status_code == 404
    unrelated_project = "never-bound-project"
    db_session.add(Project(id=unrelated_project, tenant_id=actor.org_id, name="Other scope", owner_user_id=actor.user_id))
    await db_session.commit()
    unrelated_path = f"/v1/products/{product_id}/projects/{unrelated_project}"
    assert (await test_client.get(f"{unrelated_path}/changes")).json() == {"items": [], "next_cursor": None}
    assert (await test_client.get(f"{unrelated_path}/revisions/3")).status_code == 404
    later = await test_client.patch(
        f"/v1/products/{product_id}/projects/{first_project}",
        json={"name": "Later"},
        headers={"Idempotency-Key": "readback_later_fixture_01", "If-Profile-Revision": "4"},
    )
    assert later.status_code == 200, later.text
    assert [change["revision"] for change in (await test_client.get(f"{path}/changes")).json()["items"]] == [2, 3, 4]
    assert (await test_client.get(f"{path}/revisions/5")).status_code == 404
    assert (await test_client.get(f"{unrelated_path}/changes")).json()["items"] == []
    global_change = await db_session.scalar(select(ProductChange).where(ProductChange.product_id == product_id, ProductChange.revision == 3))
    local_ref = {
        "tenant_id": actor.org_id,
        "project_id": second_project,
        "source_connection_id": "adp.profile",
        "record_type": "project",
        "record_id": second_project,
        "record_revision": global_change.id,
    }
    foreign_ref = {**local_ref, "project_id": unrelated_project, "record_id": "private-source-id"}
    global_change.source_revisions = [local_ref, foreign_ref]
    historical = json.loads(json.dumps(global_change.snapshot))
    historical["scope_snapshots"][second_project]["configuration"] = {"source_ref": foreign_ref}
    global_change.snapshot = historical
    await db_session.commit()
    scoped_change = (await test_client.get(f"{path}/changes")).json()["items"][1]
    assert scoped_change["source_revisions"] == [local_ref]
    inaccessible = await test_client.get(f"{path}/revisions/3")
    assert inaccessible.status_code == 404 and "private-source-id" not in inaccessible.text
    assert (await test_client.get(f"/v1/products/{product_id}/projects/{first_project}/revisions/3")).status_code == 200


@pytest.mark.asyncio
async def changefeed_replay_retention(test_client, db_session, app, valid_token_context, monkeypatch):
    started_at = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr("src.products.routes.utcnow", lambda: started_at)
    actor = valid_token_context
    db_session.add(Organization(id=actor.org_id, name="Replay fixture"))
    db_session.add(User(id=actor.user_id, org_id=actor.org_id, team_id=actor.team_id, email="replay@example.test", user_kind="human"))
    await add_team_member(db_session, actor)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: actor
    created = await test_client.post(
        "/v1/products",
        json={"name": "One", "purpose": "Replay", "intended_users": ["Maintainers"], "starting_point": "idea", "project": {"mode": "create"}},
        headers={"Idempotency-Key": "replay_create_fixture_01"},
    )
    assert created.status_code == 201, created.text
    path = f"/v1/products/{created.json()['product_id']}/projects/{created.json()['project_id']}"

    async def edit(revision, name):
        return await test_client.patch(
            path,
            json={"name": name},
            headers={"Idempotency-Key": f"replay_edit_{revision:02d}_fixture", "If-Profile-Revision": str(revision)},
        )

    for revision, name in ((1, "Two"), (2, "Three")):
        changed = await edit(revision, name)
        assert changed.status_code == 200, changed.text
        assert (await edit(revision, name)).json() == changed.json()
    before_concurrent = (
        await db_session.scalars(
            select(ProductChange).where(ProductChange.product_id == created.json()["product_id"]).order_by(ProductChange.revision)
        )
    ).all()
    assert [change.revision for change in before_concurrent] == [1, 2, 3]
    first = await test_client.get(f"{path}/changes?limit=1")
    assert first.status_code == 200, first.text
    first_event = first.json()["items"][0]
    assert first_event["revision"] == 1
    cursor = first.json()["next_cursor"]
    assert cursor
    assert (await edit(3, "Four")).status_code == 200
    replay = await test_client.get(f"{path}/changes?limit=1&cursor={cursor}")
    assert replay.status_code == 200, replay.text
    assert (await test_client.get(f"{path}/changes?limit=1&cursor={cursor}")).json() == replay.json()
    assert replay.json()["items"][0]["revision"] == 2
    last = await test_client.get(f"{path}/changes?limit=1&cursor={replay.json()['next_cursor']}")
    assert [item["revision"] for item in last.json()["items"]] == [3]
    assert last.json()["next_cursor"] is None
    assert [first_event["event_id"], replay.json()["items"][0]["event_id"], last.json()["items"][0]["event_id"]] == [
        change.id for change in before_concurrent
    ]
    fresh = await test_client.get(f"{path}/changes")
    assert [item["revision"] for item in fresh.json()["items"]] == [1, 2, 3, 4]
    assert fresh.json()["items"][0]["event_id"] == first_event["event_id"]
    after_concurrent = (
        await db_session.scalars(
            select(ProductChange).where(ProductChange.product_id == created.json()["product_id"]).order_by(ProductChange.revision)
        )
    ).all()
    assert [item["event_id"] for item in fresh.json()["items"]] == [change.id for change in after_concurrent]
    assert (await test_client.get(f"{path}/changes?limit=2&cursor={cursor}")).status_code == 400
    assert (await test_client.get(f"{path}/changes?limit=1&cursor={cursor}x")).status_code == 400
    with monkeypatch.context() as clock:
        clock.setattr("src.products.routes.utcnow", lambda: started_at + timedelta(minutes=16))
        expired = await test_client.get(f"{path}/changes?limit=1&cursor={cursor}")
        assert expired.status_code == 410 and expired.json()["detail"]["code"] == "revision_expired"
        expired_second_page = await test_client.get(f"{path}/changes?limit=1&cursor={replay.json()['next_cursor']}")
        assert expired_second_page.status_code == 410
    initial_change = await db_session.scalar(
        select(ProductChange).where(ProductChange.product_id == created.json()["product_id"], ProductChange.revision == 1)
    )
    initial_change.recorded_at = started_at - timedelta(days=91)
    await db_session.commit()
    old = await test_client.get(f"{path}/revisions/1")
    assert old.status_code == 410 and old.json()["detail"]["code"] == "revision_expired"
