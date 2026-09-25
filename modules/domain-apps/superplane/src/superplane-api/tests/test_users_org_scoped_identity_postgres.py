"""One ADP human subject, two organizations, against real PostgreSQL. Issue #6127.

DESIGN.md 2.2: "One person may have organization-local accounts/memberships in
multiple tenants... The domain user model currently has one `org_id` and a
globally unique nullable `cognito_sub`. It must not be assumed to mirror every
ADP multi-organization user account."

Migration `036_users_cognito_sub_per_org` narrows `users`' cognito_sub
uniqueness from global to `(org_id, cognito_sub)`. This suite proves that
change against a real PostgreSQL unique index rather than SQLite (the offline
HTTP double does not enforce column-level uniqueness the same way, and the
production behaviour this fixes is a UNIQUE VIOLATION on the live index):

1. The same subject can hold a distinct `users` row in two different
   organizations — the case that was previously unrepresentable.
2. A genuine duplicate (same org, same subject) is still rejected.
3. Each organization's row keeps its own role/status; selecting one
   organization's membership never exposes or depends on the other's.

Set ``SUPERPLANE_TEST_POSTGRES_URL`` to a ``postgresql+asyncpg`` URL, or let
the fixture start a disposable ``pgserver`` instance (installed by domain CI).
No provider, cloud or B service is contacted, and a green run here is NOT live
acceptance evidence.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.organization import Organization
from app.models.user import User

from tests.test_installation_postgres import (
    installation_postgres_url as installation_postgres_url,
)
from tests.test_installation_postgres import pytestmark as postgres_available

# CI installs pgserver and must execute these; a missing/broken disposable
# server must fail fixture setup there rather than turn the lane green with
# skips. Same convention as the other `*_postgres.py` suites in this module.
pytestmark = [] if os.environ.get("CI") else postgres_available


@pytest.fixture
async def users_db(installation_postgres_url):  # noqa: F811 - pytest fixture injection
    """Organization and User tables in their own disposable schema."""
    url = installation_postgres_url
    schema = "users_org_scope_test_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    tables = [Organization.__table__, User.__table__]
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all, tables=tables)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        yield sessions
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def _seed_org(session, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Organization(id=org_id, name=name, billing_plan="free"))
    await session.flush()
    return org_id


async def test_one_subject_holds_a_row_in_two_organizations(users_db):
    """The case migration 036 exists for: previously a UNIQUE VIOLATION."""
    subject = "cognito-sub-shared-human"
    async with users_db() as session:
        org_a = await _seed_org(session, "org-a")
        org_b = await _seed_org(session, "org-b")
        session.add(
            User(
                id=uuid.uuid4(),
                org_id=org_a,
                email="person@example.com",
                cognito_sub=subject,
                role="developer",
                status="active",
            )
        )
        await session.commit()

    # A second row for the SAME subject in a DIFFERENT organization must
    # succeed — this is exactly what the old global-unique index refused.
    async with users_db() as session:
        session.add(
            User(
                id=uuid.uuid4(),
                org_id=org_b,
                email="person@example.com",
                cognito_sub=subject,
                role="org-admin",
                status="active",
            )
        )
        await session.commit()

    async with users_db() as session:
        rows = (
            await session.execute(
                text("SELECT org_id, role FROM users WHERE cognito_sub = :sub"),
                {"sub": subject},
            )
        ).all()
    roles_by_org = {str(org): role for org, role in rows}
    assert roles_by_org == {str(org_a): "developer", str(org_b): "org-admin"}, (
        "each organization's membership must keep its own role; selecting one "
        "organization must never carry the other's"
    )


async def test_a_genuine_duplicate_within_one_organization_is_still_rejected(
    users_db,
):
    """The narrowed index must still refuse two rows for the same (org, subject)."""
    subject = "cognito-sub-duplicate"
    async with users_db() as session:
        org_id = await _seed_org(session, "org-solo")
        session.add(
            User(
                id=uuid.uuid4(),
                org_id=org_id,
                email="first@example.com",
                cognito_sub=subject,
                role="developer",
                status="active",
            )
        )
        await session.commit()

    async with users_db() as session:
        session.add(
            User(
                id=uuid.uuid4(),
                org_id=org_id,
                email="second@example.com",
                cognito_sub=subject,
                role="developer",
                status="invited",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_null_cognito_sub_does_not_collide_across_invited_rows(users_db):
    """An invited-but-not-yet-linked user (`cognito_sub IS NULL`) is unaffected.

    PostgreSQL treats NULL as distinct from any other NULL in a unique index by
    default, so two never-linked invitations in the same organization must not
    be refused by this constraint. This was true before migration 036 and must
    remain true after it — the change narrows the non-null case, not the null
    one.
    """
    async with users_db() as session:
        org_id = await _seed_org(session, "org-nulls")
        session.add_all(
            [
                User(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    email="pending-one@example.com",
                    cognito_sub=None,
                    role="developer",
                    status="invited",
                ),
                User(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    email="pending-two@example.com",
                    cognito_sub=None,
                    role="developer",
                    status="invited",
                ),
            ]
        )
        await session.commit()
