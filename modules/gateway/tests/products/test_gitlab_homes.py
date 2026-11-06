"""Product visibility and real reconciliation against an in-memory GitLab API."""

import json
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import delete, select

from src.auth.dependencies import get_current_user
from src.gitlab.admin_client import GitlabAdmin, ProvisioningError
from src.gitlab.home_models import ProductGitlabHome
from src.gitlab.home_worker import reconcile_once, reconcile_product
from src.products.models import Product
from src.shared.models.organization import TeamMembership, User
from tests.products.test_i01 import HEADERS, REQUEST

URL = "https://adp.example.test/gitlab"


class GitlabFixture:
    def __init__(self):
        self.groups = {}
        self.users = {}
        self.projects = {}
        self.acl = {}
        self.requests = []
        self.reject_users = False

    def below(self, child, ancestor):
        while child is not None:
            if child == ancestor:
                return True
            child = self.groups[child].get("parent_id")
        return False

    def effective(self, kind, resource_id):
        acl = dict(self.acl[resource_id])
        parent = self.groups[resource_id].get("parent_id") if kind == "groups" else self.projects[resource_id]["namespace"]["id"]
        if parent is not None:
            for uid, level in self.effective("groups", parent).items():
                acl[uid] = max(acl.get(uid, 0), level)
        return acl

    def handle(self, request):
        assert request.headers["PRIVATE-TOKEN"] == "fixture-admin-token"
        path = unquote(request.url.path.removeprefix("/gitlab/api/v4/"))
        method = request.method
        body = json.loads(request.content) if request.content else {}
        self.requests.append((method, path, body))
        result, status = None, 200
        if path == "user":
            result = {"id": 1, "is_admin": True}
        elif path == "users" and method == "GET":
            result = [user for user in self.users.values() if user.get(request.url.params["provider"]) == request.url.params["extern_uid"]]
        elif path == "users" and method == "POST":
            if self.reject_users:
                return httpx.Response(409, json={"error": "email conflict"})
            uid = len(self.users) + 10
            result = {**body, "id": uid, "is_admin": False, "state": "active", body["provider"]: body["extern_uid"]}
            self.users[uid] = result
        elif path.startswith("users/") and method == "PUT":
            result = self.users[int(path.split("/")[1])]
            result.update(body)
            if "provider" in body:
                result[body["provider"]] = body["extern_uid"]
        elif path == "groups" and method == "POST":
            if body.get("parent_id") is not None and any(
                group["name"] == body["name"] and group.get("parent_id") == body["parent_id"] for group in self.groups.values()
            ):
                return httpx.Response(400, json={"message": "Group name has already been taken"})
            gid = len(self.groups) + 20
            parent = self.groups.get(body.get("parent_id"))
            full = f"{parent['full_path']}/{body['path']}" if parent else body["path"]
            result = {**body, "id": gid, "full_path": full}
            self.groups[gid] = result
            self.acl[gid] = {1: 50}
        elif path.endswith("/projects"):
            result = [p for p in self.projects.values() if self.below(p["namespace"]["id"], int(path.split("/")[1]))]
        elif path.endswith("/descendant_groups"):
            gid = int(path.split("/")[1])
            result = [g for g in self.groups.values() if g["id"] != gid and self.below(g["id"], gid)]
        elif "/share/" in path and method == "DELETE":
            kind, rid, _, shared_id = path.split("/")
            obj = (self.groups if kind == "groups" else self.projects)[int(rid)]
            obj["shared_with_groups"] = [row for row in obj["shared_with_groups"] if row["group_id"] != int(shared_id)]
            status = 204
        elif "/members" in path:
            parts = path.split("/")
            acl = self.acl[int(parts[1])]
            if method == "GET":
                if path.endswith("/all"):
                    acl = self.effective(parts[0], int(parts[1]))
                result = [{"id": uid, "access_level": level} for uid, level in acl.items()]
            elif method == "POST":
                acl[body["user_id"]] = body["access_level"]
                result = body
            elif method == "PUT":
                acl[int(parts[3])] = body["access_level"]
                result = body
            else:
                acl.pop(int(parts[3]), None)
                status = 204
        elif path.startswith("projects/"):
            result = self.projects[int(path.split("/")[1])]
            if method == "PUT":
                result.update(body)
        elif path.startswith("groups/"):
            key = path.removeprefix("groups/")
            result = self.groups.get(int(key)) if key.isdecimal() else next((g for g in self.groups.values() if g["full_path"] == key), None)
            if result is None:
                status = 404
            elif method == "PUT":
                result.update(body)
        else:
            raise AssertionError((method, path))
        return httpx.Response(status, json=result) if status != 204 else httpx.Response(204)


@pytest.fixture
async def home_setup(test_client, db_session, source_member, monkeypatch):
    monkeypatch.setenv("FEATURE_GITLAB_ENABLED", "true")
    monkeypatch.setattr("src.gitlab.home_routes._discover_gitlab_url", lambda: URL)
    user = await db_session.get(User, source_member.user_id)
    user.cognito_sub = "home-test-subject"
    await db_session.commit()
    response = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert response.status_code == 201, response.text
    product = await db_session.get(Product, (source_member.org_id, response.json()["product_id"]))
    fixture = GitlabFixture()
    async with GitlabAdmin(URL, "fixture-admin-token", transport=httpx.MockTransport(fixture.handle)) as admin:
        yield product, fixture, admin


async def test_retry_preserves_group_identity_and_launch_is_product_bound(home_setup, test_client, db_session, source_member, monkeypatch):
    product, fixture, admin = home_setup
    path = f"/v1/products/{product.id}/gitlab"
    assert (await test_client.get(path)).json()["status"] == "preparing"
    home = await reconcile_product(db_session, admin, product, URL)
    first_id = home.group_id
    await db_session.commit()
    await reconcile_product(db_session, admin, product, URL)
    await db_session.commit()
    assert home.group_id == first_id
    assert len(fixture.groups) == 2 and len(fixture.users) == 1
    parent = next(g for g in fixture.groups.values() if g["parent_id"] is None)
    assert fixture.acl[parent["id"]] == {1: 50}
    assert fixture.acl[first_id] == {1: 50, 10: 30}
    assert (await test_client.get(path)).json()["status"] == "ready"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    monkeypatch.setattr("src.gitlab.home_routes._load_signing_key", lambda: pem)
    launch = await test_client.post(path + "/launch")
    assert launch.status_code == 200, launch.text
    token = parse_qs(urlsplit(launch.json()["redirect_url"]).query)["jwt"][0]
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"], audience="adp-gitlab-dev")
    assert claims["sub"] == source_member.user_id
    assert claims["return_path"] == "/gitlab/" + home.full_path
    assert claims["tenant_id"] == source_member.org_id
    assert launch.headers["cache-control"] == "no-store"


async def test_removal_denies_new_launch_and_revokes_native_access(home_setup, test_client, db_session, source_member):
    product, fixture, admin = home_setup
    home = await reconcile_product(db_session, admin, product, URL)
    await db_session.commit()
    assert 10 in fixture.acl[home.group_id]
    await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == source_member.user_id))
    await db_session.commit()
    assert (await test_client.post(f"/v1/products/{product.id}/gitlab/launch")).status_code == 404
    await reconcile_product(db_session, admin, product, URL)
    assert fixture.acl[home.group_id] == {1: 50}
    # Removing one Product membership doesn't block the shared GitLab account.
    assert fixture.users[10]["state"] == "active"


async def test_duplicate_product_names_have_distinct_gitlab_homes(home_setup, db_session):
    product, fixture, admin = home_setup
    second = Product(
        tenant_id=product.tenant_id,
        id=str(uuid4()),
        name=product.name,
        purpose=product.purpose,
        intended_users=product.intended_users,
        owner_user_id=product.owner_user_id,
        created_by=product.created_by,
        team_id=product.team_id,
        starting_point=product.starting_point,
    )
    db_session.add(second)
    await db_session.flush()
    first_home = await reconcile_product(db_session, admin, product, URL)
    second_home = await reconcile_product(db_session, admin, second, URL)
    assert first_home.group_id != second_home.group_id
    assert fixture.groups[first_home.group_id]["name"] != fixture.groups[second_home.group_id]["name"]
    assert first_home.full_path != second_home.full_path


async def test_team_role_downgrade_and_archive_revoke_access(home_setup, db_session, source_member):
    product, fixture, admin = home_setup
    membership = await db_session.scalar(select(TeamMembership).where(TeamMembership.user_id == source_member.user_id))
    membership.role = "lead"
    await db_session.commit()
    home = await reconcile_product(db_session, admin, product, URL)
    assert fixture.acl[home.group_id][10] == 40
    membership.role = "member"
    await db_session.commit()
    await reconcile_product(db_session, admin, product, URL)
    assert fixture.acl[home.group_id][10] == 30
    product.lifecycle_status = "retired"
    await db_session.commit()
    await reconcile_product(db_session, admin, product, URL)
    assert home.status == "archived" and fixture.acl[home.group_id] == {1: 50}


@pytest.mark.parametrize("change", [{"org_id": "other-tenant"}, {"user_id": "not-a-member"}, {"account_type": "service"}])
async def test_foreign_tenant_nonmember_and_service_cannot_launch(home_setup, app, test_client, source_member, change):
    product, _, _ = home_setup
    app.dependency_overrides[get_current_user] = lambda: source_member.model_copy(update=change)
    response = await test_client.post(f"/v1/products/{product.id}/gitlab/launch")
    assert response.status_code == 404
    assert product.name not in response.text


async def test_namespace_collision_is_not_adopted(home_setup, db_session):
    product, fixture, admin = home_setup
    home = await reconcile_product(db_session, admin, product, URL)
    fixture.groups[home.group_id]["description"] = "Unrelated owner-managed group"
    with pytest.raises(ProvisioningError, match="namespace_ownership_conflict"):
        await reconcile_product(db_session, admin, product, URL)


async def test_matching_label_without_service_ownership_is_not_adopted(home_setup, db_session):
    product, fixture, admin = home_setup
    home = await reconcile_product(db_session, admin, product, URL)
    fixture.acl[home.group_id] = {123: 50}
    with pytest.raises(ProvisioningError, match="namespace_ownership_conflict"):
        await reconcile_product(db_session, admin, product, URL)
    assert fixture.acl[home.group_id] == {123: 50}


async def test_email_collision_never_links_an_unproven_identity(home_setup, db_session):
    product, fixture, admin = home_setup
    fixture.reject_users = True
    with pytest.raises(ProvisioningError, match="identity_link_required"):
        await reconcile_product(db_session, admin, product, URL)
    assert not fixture.users
    assert all(acl == {1: 50} for acl in fixture.acl.values())


async def test_disabled_feature_does_not_discover_or_launch(home_setup, test_client, monkeypatch):
    product, _, _ = home_setup
    monkeypatch.setenv("FEATURE_GITLAB_ENABLED", "false")
    monkeypatch.setattr("src.gitlab.home_routes._discover_gitlab_url", lambda: pytest.fail("Optional feature must not discover resources"))
    path = f"/v1/products/{product.id}/gitlab"
    assert (await test_client.get(path)).json()["status"] == "disabled"
    assert (await test_client.post(path + "/launch")).status_code == 503


async def test_offboarding_clears_descendant_project_grants_and_shares(home_setup, db_session, source_member):
    product, fixture, admin = home_setup
    home = await reconcile_product(db_session, admin, product, URL)
    child = await admin.group("legacy", "Legacy", "Nested content", parent_id=(home.group_id, home.full_path))
    fixture.acl[child["id"]][10] = 40
    fixture.groups[child["id"]]["shared_with_groups"] = [{"group_id": 999}]
    fixture.projects[900] = {"id": 900, "namespace": {"id": child["id"]}, "visibility": "internal", "shared_with_groups": [{"group_id": 999}]}
    fixture.acl[900] = {10: 40, 123: 30}
    await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == source_member.user_id))
    await db_session.commit()
    await reconcile_product(db_session, admin, product, URL)
    assert fixture.effective("projects", 900) == {1: 50}
    assert fixture.acl[900] == {}
    assert fixture.projects[900]["visibility"] == "private"
    assert fixture.projects[900]["shared_with_groups"] == []
    assert fixture.groups[child["id"]]["shared_with_groups"] == []


async def test_worker_keeps_binding_on_failure_then_recovers(home_setup, db_session_factory, db_session, test_client):
    product, fixture, admin = home_setup
    fixture.reject_users = True
    await reconcile_once(db_session_factory, admin, URL)
    home = await db_session.get(ProductGitlabHome, (product.tenant_id, product.id), populate_existing=True)
    group_id = home.group_id
    assert home.status == "error" and home.error_code == "identity_link_required"
    assert (await test_client.get(f"/v1/products/{product.id}/gitlab")).json()["status"] == "error"
    fixture.reject_users = False
    await reconcile_once(db_session_factory, admin, URL)
    await db_session.refresh(home)
    assert home.status == "ready" and home.group_id == group_id
    assert len(fixture.groups) == 2 and fixture.acl[group_id][10] == 30
