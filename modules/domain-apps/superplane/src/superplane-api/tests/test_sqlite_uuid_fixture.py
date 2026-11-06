"""The SQLite HTTP fixture must preserve every PostgreSQL UUID representation."""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql

from app.models.organization import Organization
from app.models.user import User
from tests.conftest import async_session_test


@pytest.mark.parametrize(
    "value",
    [
        "11111111-1111-4111-8111-111111111111",
        "11111111-1111-4111-8111-11111111e111",
        "00000000-0000-4000-8000-000000000001",
    ],
    ids=["decimal-hex", "scientific-notation-hex", "leading-zero-hex"],
)
async def test_uuid_bulk_insert_and_round_trip_preserve_exact_values(value):
    organization_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    expected = {
        uuid.UUID(value),
        uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    }
    async with async_session_test() as session:
        session.add(Organization(id=organization_id, name="uuid-fixture"))
        await session.commit()
        session.add_all(
            User(
                id=identifier,
                org_id=organization_id,
                email=f"{identifier}@example.invalid",
            )
            for identifier in expected
        )
        # Exercise the bulk-insert RETURNING path used by workspace fixtures.
        await session.commit()

    async with async_session_test() as session:
        assert set(await session.scalars(select(User.id))) == expected
        stored = (await session.execute(text("SELECT typeof(id), id FROM users"))).all()
        assert {(kind, value) for kind, value in stored} == {
            ("text", identifier.hex) for identifier in expected
        }


def test_sqlite_fixture_adapter_preserves_native_postgresql_uuid():
    assert User.__table__.c.id.type.compile(dialect=postgresql.dialect()) == "UUID"
