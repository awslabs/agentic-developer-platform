"""Product people use the same live memberships and tenant boundary as Product reads."""

import pytest

from src.auth.dependencies import get_current_user
from src.products.models import Product
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.vault import UserIdentity


@pytest.fixture
async def product_people_fixture(test_client, db_session, source_member):
    org = source_member.org_id
    db_session.add_all(
        [
            Team(id="product-team", org_id=org, department_id="department", name="Platform"),
            Team(id="other-team", org_id=org, department_id="department", name="Other"),
            User(id="member", org_id=org, team_id="other-team", email="member@example.test", name="Member", user_kind="human"),
            User(id="legacy", org_id=org, team_id="product-team", email="legacy@example.test", name="Legacy", user_kind="human"),
            User(id="revoked", org_id=org, team_id="product-team", email="revoked@example.test", user_kind="human"),
            User(id="shadow", org_id=org, team_id="product-team", email="shadow@example.test", user_kind="human", is_shadow=True),
            User(id="robot", org_id=org, team_id="product-team", email="robot@example.test", user_kind="service"),
            Organization(id="foreign-org", name="Foreign"),
            User(id="foreign", org_id="foreign-org", team_id="product-team", email="foreign@example.test", user_kind="human"),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            TeamMembership(org_id=org, user_id=source_member.user_id, team_id="product-team"),
            TeamMembership(org_id=org, user_id="member", team_id="product-team"),
            TeamMembership(org_id=org, user_id="revoked", team_id="other-team"),
            UserIdentity(
                org_id=org,
                user_id="member",
                team_id="product-team",
                provider="github",
                provider_user_id="10",
                provider_username="member-github",
                verification_method="oauth",
                is_primary=True,
            ),
            UserIdentity(
                org_id=org,
                user_id="legacy",
                team_id="product-team",
                provider="github",
                provider_user_id="20",
                provider_username="unverified",
                verification_method="self_asserted",
            ),
            UserIdentity(
                org_id="foreign-org",
                user_id="legacy",
                team_id="product-team",
                provider="github",
                provider_user_id="30",
                provider_username="foreign-identity",
                verification_method="oauth",
            ),
        ]
    )
    await db_session.commit()
    response = await test_client.post(
        "/v1/products",
        json={
            "name": "Planner",
            "purpose": "Plan releases",
            "intended_users": ["Maintainers"],
            "starting_point": "idea",
            "project": {"mode": "create"},
        },
        headers={"Idempotency-Key": "people_fixture_create"},
    )
    assert response.status_code == 201, response.text
    profile = response.json()
    product = await db_session.get(Product, (org, profile["product_id"]))
    product.team_id = "product-team"
    await db_session.commit()
    return f"/v1/products/{profile['product_id']}/projects/{profile['project_id']}", product


async def test_people_exposes_only_current_humans_and_proven_same_org_identities(test_client, product_people_fixture):
    path, _ = product_people_fixture
    response = await test_client.get(path + "/people")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    people = response.json()
    assert people["team"] == {"id": "product-team", "name": "Platform"}
    assert people["is_owner"] is True
    assert people["permissions"] == {"manage_team": False, "manage_people": False, "enroll_github": False}
    assert people["members"] == [
        {"id": "member", "name": "Member", "github_username": "member-github"},
        {"id": "user-test-123", "name": "Organization member", "github_username": None},
    ]
    first = (await test_client.get(path + "/people?limit=1")).json()
    second = (await test_client.get(path + f"/people?limit=1&offset={first['next_offset']}")).json()
    assert first["members"] + second["members"] == people["members"]
    assert second["next_offset"] is None


@pytest.mark.parametrize(
    "user_id,org_id,expected",
    [("member", "org-test-123", 200), ("legacy", "org-test-123", 404), ("revoked", "org-test-123", 404), ("foreign", "foreign-org", 404)],
)
async def test_people_rechecks_scope(test_client, app, source_member, product_people_fixture, user_id, org_id, expected):
    path, _ = product_people_fixture
    app.dependency_overrides[get_current_user] = lambda: source_member.model_copy(update={"user_id": user_id, "org_id": org_id})
    response = await test_client.get(path + "/people")
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json()["is_owner"] is False
    else:
        assert "Platform" not in response.text


async def test_people_permissions_follow_archive_and_admin(test_client, app, source_member, db_session, product_people_fixture):
    from src.shared.models.base import utcnow

    path, product = product_people_fixture
    app.dependency_overrides[get_current_user] = lambda: source_member.model_copy(update={"is_admin": True})
    response = await test_client.get(path + "/people")
    assert response.status_code == 200, response.text
    assert all(response.json()["permissions"].values())
    product.archived_at = utcnow()
    await db_session.commit()
    archived = await test_client.get(path + "/people")
    assert not any(archived.json()["permissions"].values())
