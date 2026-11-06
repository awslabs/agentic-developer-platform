"""Linked-org membership must not implicitly delegate parent administrator rights."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.admin.onboarding import handler
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User


@pytest.fixture
async def linked_orgs(db_session, monkeypatch):
    from src.admin.connections import github_client, service

    parent = Organization(id="parent", name="parent-org", github_installation_ids=["100"], member_approval_policy="auto_approve_org_members")
    db_session.add_all([Organization(id="home", name="home"), parent])
    await db_session.flush()
    children = [
        Organization(
            id=f"child-{n}",
            name=f"child-org-{n}",
            parent_tenant_id=parent.id,
            github_installation_ids=[str(200 + n)],
            member_approval_policy="auto_approve_org_members",
        )
        for n in (1, 2)
    ]
    db_session.add_all(children)
    db_session.add(User(id="human", cognito_sub="login", org_id="home", team_id="", email="human@example.test"))
    await db_session.flush()
    responses = {child.name: {"state": "active", "role": "admin", "user": {"id": 123}} for child in children}

    async def get_membership(url, **kwargs):
        membership = responses.get(url.split("/")[2])
        return SimpleNamespace(status_code=200 if membership else 404, json=lambda: membership)

    client = SimpleNamespace(
        get_installation_token=AsyncMock(return_value="test-installation-token"), _http_client=SimpleNamespace(get=get_membership), aclose=AsyncMock()
    )
    monkeypatch.setattr(service, "_get_github_app_credentials", lambda: ("test-app", "test-key"))
    monkeypatch.setattr(github_client, "GitHubAppClient", lambda **kwargs: client)
    return parent, children, responses


async def sync_memberships(db):
    matches = await handler._find_matching_tenants_for_user(db, "human", "123")
    await handler._create_memberships_for_matches(db, "human", matches, "human", "123")
    return list((await db.scalars(select(TenantMembership).where(TenantMembership.user_id == "human"))).all())


@pytest.mark.parametrize(
    "parent_proof",
    [
        None,
        {"state": "active", "role": "member", "user": {"id": 123}},
        {"state": "pending", "role": "admin", "user": {"id": 123}},
        {"state": "active", "role": "admin", "user": {"id": 456}},
    ],
)
async def test_child_admins_join_parent_as_members_without_parent_admin_proof(db_session, linked_orgs, parent_proof):
    parent, _, responses = linked_orgs
    responses[parent.name] = parent_proof
    memberships = await sync_memberships(db_session)
    # Multiple linked orgs resolve to one membership, with no duplicate insert.
    assert len(memberships) == 1
    assert memberships[0].tenant_id == parent.id
    assert memberships[0].role == "member"
    assert memberships[0].is_active


async def test_verified_parent_admin_keeps_admin_rights_when_linked_orgs_also_match(db_session, linked_orgs):
    parent, _, responses = linked_orgs
    responses[parent.name] = {"state": "active", "role": "admin", "user": {"id": 123}}
    memberships = await sync_memberships(db_session)
    assert [(m.tenant_id, m.role) for m in memberships] == [(parent.id, "org_admin")]


async def test_explicit_existing_parent_admin_membership_is_preserved(db_session, linked_orgs):
    parent, _, _ = linked_orgs
    db_session.add(TenantMembership(user_id="human", tenant_id=parent.id, role="org_admin", is_active=True, joined_via="invite"))
    await db_session.flush()
    memberships = await sync_memberships(db_session)
    assert [(m.role, m.joined_via) for m in memberships] == [("org_admin", "invite")]


async def test_parent_approval_policy_still_controls_linked_membership(db_session, linked_orgs):
    parent, _, _ = linked_orgs
    parent.member_approval_policy = "require_admin_approval"
    await db_session.flush()
    assert await sync_memberships(db_session) == []


async def test_unlinked_github_admin_still_administers_own_tenant(db_session, linked_orgs):
    _, children, responses = linked_orgs
    children[0].parent_tenant_id = None
    responses.pop(children[1].name)
    await db_session.flush()
    memberships = await sync_memberships(db_session)
    assert [(m.tenant_id, m.role) for m in memberships] == [(children[0].id, "org_admin")]
