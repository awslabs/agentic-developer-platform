"""``person_user_ids``: which ``users`` rows are provably one person (#8011)."""

from datetime import UTC, datetime

import pytest

from src.shared.identity.workspaces import person_user_ids
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _identity(user_id, org_id, provider, provider_user_id, method):
    return UserIdentity(
        user_id=user_id,
        org_id=org_id,
        team_id="team",
        provider=provider,
        provider_user_id=provider_user_id,
        verification_method=method,
        verified_at=NOW,
    )


@pytest.fixture
async def people(db_session):
    db_session.add_all(
        [
            Organization(id="home", name="Home"),
            Organization(id="tenant", name="Tenant"),
            Organization(id="other", name="Other"),
            User(id="home-row", cognito_sub="owner-sub", org_id="home", team_id="team", email="owner@example.test"),
            User(id="tenant-row", cognito_sub=None, org_id="tenant", team_id="team", email="owner@example.test"),
            User(id="other-row", cognito_sub=None, org_id="other", team_id="team", email="owner@example.test"),
            User(id="stranger", cognito_sub="stranger-sub", org_id="tenant", team_id="team", email="owner@example.test"),
        ]
    )
    await db_session.commit()
    return db_session


async def test_a_proven_github_identity_fuses_rows_across_orgs(people):
    people.add_all(
        [
            _identity("home-row", "home", "github", "20402445", "oauth"),
            _identity("tenant-row", "tenant", "github", "20402445", "admin_attested"),
            # Same numeric id in another provider's namespace is a different person.
            _identity("stranger", "tenant", "directory", "20402445", "oauth"),
        ]
    )
    await people.commit()
    home = await people.get(User, "home-row")
    assert await person_user_ids(people, home) == {"home-row", "tenant-row"}
    # Fusion is symmetric: starting from the tenant row reaches the login row.
    tenant = await people.get(User, "tenant-row")
    assert await person_user_ids(people, tenant) == {"home-row", "tenant-row"}


async def test_unproven_claims_and_email_never_fuse(people):
    people.add_all(
        [
            _identity("home-row", "home", "github", "20402445", "oauth"),
            _identity("tenant-row", "tenant", "github", "20402445", "self_asserted"),
            _identity("other-row", "other", "github", "20402445", "admin_manual"),
        ]
    )
    await people.commit()
    home = await people.get(User, "home-row")
    # Rows sharing only an email (and unproven claims) do not count.
    assert await person_user_ids(people, home) == {"home-row"}


async def test_an_org_placement_link_fuses_the_placed_row(people):
    people.add(_identity("tenant-row", "tenant", "cognito", "owner-sub", "org_placement"))
    await people.commit()
    home = await people.get(User, "home-row")
    assert await person_user_ids(people, home) == {"home-row", "tenant-row"}


async def test_a_row_without_links_is_only_itself(people):
    stranger = await people.get(User, "stranger")
    assert await person_user_ids(people, stranger) == {"stranger"}
