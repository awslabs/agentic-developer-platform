"""Exercise Product mutation and replay with PostgreSQL audit foreign keys."""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.products.models import Product, ProductChange, ProductOperation
from src.shared.models.base import Base
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.products.test_i02 import archive_and_history as _archive_and_history
from tests.products.test_i02 import edit_race as _edit_race
from tests.products.test_i02 import (
    editors,  # noqa: F401
    new_product,
)


@pytest.fixture
async def test_engine(pg_url):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    tables = {
        table
        for table in Base.metadata.tables.values()
        if table.name.startswith("product_")
        or table.name in {"products", "projects", "organizations", "users", "tenant_memberships", "team_memberships"}
    }
    while True:
        required = {foreign_key.column.table for table in tables for foreign_key in table.foreign_keys}
        if required <= tables:
            break
        tables |= required
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=list(tables)))
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_edit_conflict_and_audit_on_postgres(test_client, db_session, test_engine, app, editors):  # noqa: F811
    await _edit_race(test_client, db_session, test_engine, app, editors)


@pytest.mark.asyncio
async def test_archive_restore_history_and_replay_on_postgres(test_client, db_session, app, editors, monkeypatch):  # noqa: F811
    await _archive_and_history(test_client, db_session, app, editors, monkeypatch)


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_receipt_and_product_before_retry(test_client, db_session, editors):  # noqa: F811
    url = await new_product(test_client)
    await db_session.execute(text("ALTER TABLE product_changes ADD CONSTRAINT audit_fixture_refusal CHECK (revision = 1)"))
    await db_session.commit()
    headers = {"Idempotency-Key": "archive_rollback_12345", "If-Profile-Revision": "1"}
    body = {"action": "archive", "reason": "Transactional audit failure fixture"}

    with pytest.raises(IntegrityError, match="audit_fixture_refusal"):
        await test_client.post(url + "/lifecycle", json=body, headers=headers)

    product = await db_session.scalar(select(Product))
    assert product.revision == 1
    assert product.archived_at is None
    for model in (ProductOperation, ProductChange):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 1

    await db_session.execute(text("ALTER TABLE product_changes DROP CONSTRAINT audit_fixture_refusal"))
    await db_session.commit()
    retried = await test_client.post(url + "/lifecycle", json=body, headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["revision"] == 2
    assert retried.json()["archived_at"]
    assert (await test_client.post(url + "/lifecycle", json=body, headers=headers)).json() == retried.json()
    for model in (ProductOperation, ProductChange):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 2
