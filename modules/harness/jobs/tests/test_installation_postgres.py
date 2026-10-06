"""Operator preparation must prove real PostgreSQL ownership and isolation."""

import uuid

import asyncpg
import pytest
import pytest_asyncio

from harness_jobs.installation import prepare, validate
from harness_jobs.schema import SCHEMA_VERSION

from .conftest import postgres_url, requires_postgres

pytestmark = requires_postgres


@pytest_asyncio.fixture
async def target(postgres_server):
    connection = await asyncpg.connect(postgres_url())
    suffix = uuid.uuid4().hex[:12]
    request = {
        "database": await connection.fetchval("SELECT current_database()"),
        "schema": "ops_" + suffix,
        "owner_role": "owner_" + suffix,
        "runtime_roles": ["gateway_" + suffix, "worker_" + suffix],
        "forbidden_roles": ["domain_" + suffix],
        "forbidden_schemas": ["domain_" + suffix],
        "marker": "adp-harness-installation-v1:" + "a" * 64,
    }
    await connection.execute(f'CREATE ROLE "{request["forbidden_roles"][0]}" LOGIN')
    await connection.execute(
        f'CREATE SCHEMA "{request["forbidden_schemas"][0]}" '
        f'AUTHORIZATION "{request["forbidden_roles"][0]}"'
    )
    passwords = {
        role: "test-password-" + uuid.uuid4().hex for role in request["runtime_roles"]
    }
    try:
        yield connection, request, passwords
    finally:
        await connection.execute("RESET ROLE")
        await connection.execute(f'DROP SCHEMA IF EXISTS "{request["schema"]}" CASCADE')
        await connection.execute(
            f'DROP SCHEMA "{request["forbidden_schemas"][0]}" CASCADE'
        )
        for role in [
            request["owner_role"],
            *request["runtime_roles"],
            *request["forbidden_roles"],
        ]:
            if await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)", role
            ):
                await connection.execute(f'DROP OWNED BY "{role}"')
                await connection.execute(f'DROP ROLE "{role}"')
        await connection.close()


async def test_prepare_replay_and_domain_isolation(target):
    connection, request, passwords = target
    assert (await prepare(connection, request, passwords))[
        "schema_version"
    ] == SCHEMA_VERSION
    assert (await prepare(connection, request, passwords))["status"] == "prepared"
    table = request["schema"] + ".harness_jobs_schema_version"
    for role in request["runtime_roles"]:
        await connection.execute(f'SET ROLE "{role}"')
        assert (
            await connection.fetchval(f"SELECT version FROM {table}") == SCHEMA_VERSION
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                f'CREATE TABLE "{request["schema"]}".unreviewed(id int)'
            )
        await connection.execute("RESET ROLE")
    await connection.execute(f'SET ROLE "{request["forbidden_roles"][0]}"')
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await connection.fetchval(f"SELECT version FROM {table}")


@pytest.mark.parametrize("collision", ["role", "schema"])
async def test_unmarked_resource_refuses_without_partial_changes(target, collision):
    connection, request, passwords = target
    if collision == "role":
        await connection.execute(f'CREATE ROLE "{request["owner_role"]}"')
    else:
        await connection.execute(f'CREATE SCHEMA "{request["schema"]}"')
    with pytest.raises(ValueError, match="ownership|not owned"):
        await prepare(connection, request, passwords)
    assert not await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)",
        request["runtime_roles"][0],
    )


async def test_foreign_grant_and_newer_schema_refuse_without_adoption(target):
    connection, request, passwords = target
    await prepare(connection, request, passwords)
    schema = request["schema"]
    domain = request["forbidden_roles"][0]
    await connection.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{domain}"')
    with pytest.raises(ValueError, match="Domain role"):
        await prepare(connection, request, passwords)
    assert await connection.fetchval(
        "SELECT has_schema_privilege($1,$2,'USAGE')", domain, schema
    )
    await connection.execute(f'REVOKE USAGE ON SCHEMA "{schema}" FROM "{domain}"')
    await connection.execute(
        f'UPDATE "{schema}".harness_jobs_schema_version SET version=$1',
        SCHEMA_VERSION + 1,
    )
    with pytest.raises(ValueError, match="newer"):
        await prepare(connection, request, passwords)


async def test_failing_migration_rolls_back_roles_schema_and_grants(
    target, monkeypatch
):
    connection, request, passwords = target

    async def failed(connection):
        raise RuntimeError("interrupted")

    monkeypatch.setattr("harness_jobs.installation.apply", failed)
    with pytest.raises(RuntimeError, match="interrupted"):
        await prepare(connection, request, passwords)
    assert not await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=$1)", request["schema"]
    )
    assert not await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)", request["owner_role"]
    )


def test_closed_identity_contract():
    with pytest.raises(ValueError, match="Closed"):
        validate({"sql": "arbitrary"})


@pytest.mark.parametrize(
    "drift",
    [
        "create",
        "public",
        "membership",
        "domain",
        "truncate",
        "grant_option",
        "default_grant",
    ],
)
async def test_privilege_drift_is_refused_and_preserved(target, drift):
    connection, request, passwords = target
    await prepare(connection, request, passwords)
    schema, role = request["schema"], request["runtime_roles"][0]
    if drift == "create":
        statement = f'GRANT CREATE ON SCHEMA "{schema}" TO "{role}"'
    elif drift == "public":
        statement = f'GRANT USAGE ON SCHEMA "{schema}" TO PUBLIC'
    elif drift == "membership":
        statement = f'GRANT "{request["forbidden_roles"][0]}" TO "{role}"'
    elif drift == "domain":
        statement = (
            f'GRANT USAGE ON SCHEMA "{request["forbidden_schemas"][0]}" TO "{role}"'
        )
    elif drift == "truncate":
        statement = (
            f'GRANT TRUNCATE ON "{schema}".harness_jobs_schema_version TO "{role}"'
        )
    elif drift == "grant_option":
        statement = (
            f'GRANT SELECT ON "{schema}".harness_jobs_schema_version '
            f'TO "{role}" WITH GRANT OPTION'
        )
    else:
        statement = (
            f'ALTER DEFAULT PRIVILEGES FOR ROLE "{request["owner_role"]}" '
            f'IN SCHEMA "{schema}" GRANT TRUNCATE ON TABLES TO "{role}"'
        )
    await connection.execute(statement)
    with pytest.raises(ValueError):
        await prepare(connection, request, passwords)


async def test_non_superuser_database_operator_can_prepare_and_reconcile(target):
    connection, request, passwords = target
    operator = "operator_" + uuid.uuid4().hex[:12]
    await connection.execute(f'CREATE ROLE "{operator}" LOGIN CREATEROLE')
    await connection.execute(
        f'GRANT CREATE ON DATABASE "{request["database"]}" TO "{operator}"'
    )
    admin = await asyncpg.connect(postgres_url(), user=operator)
    try:
        assert not await admin.fetchval(
            "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
        )
        assert (await prepare(admin, request, passwords))["status"] == "prepared"
        assert (await prepare(admin, request, passwords))["status"] == "prepared"
    finally:
        await admin.close()
        await connection.execute(f'DROP OWNED BY "{operator}"')
        await connection.execute(f'DROP ROLE "{operator}"')
