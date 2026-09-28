"""Execute generated preparation SQL on a disposable PostgreSQL server."""

import asyncio
import importlib.util
import uuid
from pathlib import Path

import asyncpg
import pgserver
import pytest

from installation.config import prepare_database_sql


def test_prepared_credentials_use_real_password_authentication(
    preparation_server, environment
):
    path = (
        Path(__file__).resolve().parents[2]
        / "src/superplane-api/app/database_preparation.py"
    )
    spec = importlib.util.spec_from_file_location("sp_database_preparation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def scenario(conn, env):
        passwords = {
            f"superplane_{env['environment']}_{kind}": uuid.uuid4().hex
            + uuid.uuid4().hex
            for kind in ("runtime", "migration", "skypilot")
        }
        await module.apply_preparation(conn, prepare_database_sql(env), passwords)
        hba = Path(await conn.fetchval("SHOW hba_file"))
        original = hba.read_text()
        # Existing disposable admin remains on its original trust rule. Every
        # prepared role must complete SCRAM password authentication instead.
        hba.write_text(
            "local all " + ",".join(passwords) + " scram-sha-256\n" + original
        )
        try:
            assert await conn.fetchval("SELECT pg_reload_conf()")
            await asyncio.sleep(0.2)
            for role, password in passwords.items():
                with pytest.raises(asyncpg.InvalidPasswordError):
                    await asyncpg.connect(
                        preparation_server,
                        user=role,
                        password="wrong",
                        database=env["database"]["database"],
                    )
                authenticated = await asyncpg.connect(
                    preparation_server,
                    user=role,
                    password=password,
                    database=env["database"]["database"],
                )
                try:
                    assert await authenticated.fetchval("SELECT current_user") == role
                    expected = (
                        "skypilot" if role.endswith("_skypilot") else "superplane"
                    )
                    assert (
                        await authenticated.fetchval("SELECT current_schema()")
                        == expected
                    )
                finally:
                    await authenticated.close()
            # Reapplying the owned preparation preserves the stored passwords.
            await module.apply_preparation(conn, prepare_database_sql(env), passwords)
        finally:
            hba.write_text(original)
            await conn.execute("SELECT pg_reload_conf()")

    exercise(preparation_server, environment, scenario)


@pytest.fixture(scope="module")
def preparation_server(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("superplane-preparation-pg"))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


def exercise(server, environment, scenario, *, database_suffix=""):
    async def run():
        admin = await asyncpg.connect(server)
        name = "prepare_" + uuid.uuid4().hex[:12] + database_suffix
        quoted = '"' + name.replace('"', '""') + '"'
        environment["environment"] = "t" + uuid.uuid4().hex[:12]
        environment["database"]["database"] = name
        await admin.execute(f"CREATE DATABASE {quoted}")
        connection = await asyncpg.connect(server, database=name)
        try:
            await scenario(connection, environment)
        finally:
            await connection.close()
            await admin.execute(f"DROP DATABASE {quoted}")
            for kind in ("migration", "runtime", "skypilot"):
                role = f"superplane_{environment['environment']}_{kind}"
                await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
            await admin.close()

    asyncio.run(run())


@pytest.mark.parametrize("suffix", ["", '";--', "'quoted"])
def test_preparation_is_executable_idempotent_and_scoped(
    preparation_server, environment, suffix
):
    async def scenario(conn, env):
        sql = prepare_database_sql(env)
        await conn.execute(
            "CREATE SCHEMA core; CREATE TABLE core.sentinel (value text)"
        )
        await conn.execute("INSERT INTO core.sentinel VALUES ('preserved')")
        await conn.execute(sql)
        migration = f"superplane_{env['environment']}_migration"
        runtime = f"superplane_{env['environment']}_runtime"
        role_id = await conn.fetchval(
            "SELECT oid FROM pg_roles WHERE rolname=$1", runtime
        )
        await conn.execute(f'SET ROLE "{migration}"')
        await conn.execute(
            "CREATE TABLE superplane.sample (id serial PRIMARY KEY, value text)"
        )
        await conn.execute("RESET ROLE")
        await conn.execute(sql)
        assert (
            await conn.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", runtime)
            == role_id
        )
        await conn.execute(f'SET ROLE "{runtime}"')
        await conn.execute("INSERT INTO superplane.sample (value) VALUES ('retained')")
        assert await conn.fetchval("SELECT value FROM superplane.sample") == "retained"
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("UPDATE core.sentinel SET value='changed'")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("CREATE TABLE superplane.forbidden (id int)")
        await conn.execute("RESET ROLE")
        assert await conn.fetchval("SELECT value FROM core.sentinel") == "preserved"

    exercise(preparation_server, environment, scenario, database_suffix=suffix)


@pytest.mark.parametrize("collision", ["role", "schema"])
def test_existing_unowned_resources_are_preserved(
    preparation_server, environment, collision
):
    async def scenario(conn, env):
        role = f"superplane_{env['environment']}_runtime"
        if collision == "role":
            await conn.execute(f'CREATE ROLE "{role}" LOGIN CREATEDB')
        else:
            await conn.execute(
                "CREATE SCHEMA superplane; CREATE TABLE superplane.sentinel (value text)"
            )
            await conn.execute("INSERT INTO superplane.sentinel VALUES ('preserved')")
        with pytest.raises(asyncpg.RaiseError, match="Existing database"):
            await conn.execute(prepare_database_sql(env))
        await conn.execute("ROLLBACK")
        assert await conn.fetchval("SELECT to_regnamespace('skypilot')") is None
        if collision == "role":
            assert await conn.fetchval(
                "SELECT rolcreatedb FROM pg_roles WHERE rolname=$1", role
            )
            assert await conn.fetchval("SELECT to_regnamespace('superplane')") is None
        else:
            assert (
                await conn.fetchval("SELECT value FROM superplane.sentinel")
                == "preserved"
            )
            assert (
                await conn.fetchval("SELECT oid FROM pg_roles WHERE rolname=$1", role)
                is None
            )

    exercise(preparation_server, environment, scenario)


def test_wrong_database_is_refused_before_changes(preparation_server, environment):
    async def scenario(conn, env):
        env["database"]["database"] = "another_database"
        with pytest.raises(asyncpg.RaiseError, match="target mismatch"):
            await conn.execute(prepare_database_sql(env))
        await conn.execute("ROLLBACK")
        assert await conn.fetchval("SELECT to_regnamespace('superplane')") is None

    exercise(preparation_server, environment, scenario)


def test_shared_public_grants_are_refused_without_changing_them(
    preparation_server, environment
):
    async def scenario(conn, env):
        await conn.execute("GRANT CREATE ON SCHEMA public TO PUBLIC")
        with pytest.raises(asyncpg.RaiseError, match="outside its domain schema"):
            await conn.execute(prepare_database_sql(env))
        await conn.execute("ROLLBACK")
        assert await conn.fetchval("SELECT to_regnamespace('superplane')") is None
        assert "=UC/" in await conn.fetchval(
            "SELECT nspacl::text FROM pg_namespace WHERE nspname='public'"
        )

    exercise(preparation_server, environment, scenario)


def test_preparation_with_non_superuser_database_owner(preparation_server, environment):
    async def scenario(conn, env):
        owner = "preparer_" + uuid.uuid4().hex[:12]
        original = await conn.fetchval("SELECT current_user")
        database = env["database"]["database"]
        await conn.execute(f'CREATE ROLE "{owner}" LOGIN CREATEROLE')
        await conn.execute(f'ALTER DATABASE "{database}" OWNER TO "{owner}"')
        try:
            await conn.execute(f'SET ROLE "{owner}"')
            await conn.execute(prepare_database_sql(env))
            await conn.execute(prepare_database_sql(env))
            assert (
                await conn.fetchval("SELECT current_setting('createrole_self_grant')")
                == ""
            )
        finally:
            await conn.execute("ROLLBACK")
            await conn.execute("RESET ROLE")
            await conn.execute(f'ALTER DATABASE "{database}" OWNER TO "{original}"')
            await conn.execute(f'DROP OWNED BY "{owner}"')
            await conn.execute(f'DROP ROLE "{owner}"')

    exercise(preparation_server, environment, scenario)
