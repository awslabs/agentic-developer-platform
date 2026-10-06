"""Explicit operator preparation of an isolated harness store.

This module never opens a connection, reads a credential, or runs at service
startup. The installation owner supplies its privileged connection and exact
reviewed identities. Domain migrations must not call this path.
"""

from __future__ import annotations

import re

from .schema import SCHEMA_VERSION, apply, check_schema_version, current_version


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
        raise ValueError("Exact PostgreSQL identifier required")
    if value.startswith("pg_") or value in {"public", "information_schema"}:
        raise ValueError("System schema or role is forbidden")
    return '"' + value + '"'


def validate(request: dict) -> None:
    if set(request) != {
        "database",
        "schema",
        "owner_role",
        "runtime_roles",
        "forbidden_roles",
        "forbidden_schemas",
        "marker",
    }:
        raise ValueError("Closed shared store preparation request required")
    for key in ("database", "schema", "owner_role"):
        identifier(request[key])
    roles = request["runtime_roles"]
    forbidden = request["forbidden_roles"]
    schemas = request["forbidden_schemas"]
    if not isinstance(schemas, list) or not schemas or request["schema"] in schemas:
        raise ValueError("Explicit distinct domain schemas required")
    for schema in schemas:
        identifier(schema)
    if not isinstance(roles, list) or len(roles) != 2 or len(set(roles)) != 2:
        raise ValueError("Two distinct shared runtime roles required")
    if (
        not isinstance(forbidden, list)
        or not forbidden
        or len(set(forbidden)) != len(forbidden)
    ):
        raise ValueError("Explicit excluded domain roles required")
    for role in roles + forbidden:
        identifier(role)
    if set(roles) & set(forbidden) or request["owner_role"] in roles + forbidden:
        raise ValueError("Shared and domain authorities must be distinct")
    if not isinstance(request["marker"], str) or not re.fullmatch(
        r"adp-harness-installation-v1:[a-f0-9]{64}", request["marker"]
    ):
        raise ValueError("Immutable ownership marker required")


async def prepare(connection, request: dict, passwords: dict) -> dict:
    """Create or verify owned roles/schema, apply canonical DDL, prove isolation.

    The caller supplies a connection outside a transaction. All database changes
    are atomic, including passwords and grants. Existing unmarked resources,
    incompatible privileges, extra memberships and newer schema versions refuse.
    """
    validate(request)
    runtimes = request["runtime_roles"]
    if set(passwords) != set(runtimes) or any(
        not isinstance(p, str) or len(p) < 32 for p in passwords.values()
    ):
        raise ValueError("Exact strong runtime passwords required")
    schema, owner = request["schema"], request["owner_role"]
    marker = request["marker"]
    async with connection.transaction():
        await connection.execute("SET LOCAL lock_timeout = '10s'")
        await connection.execute("SET LOCAL statement_timeout = '60s'")
        if (
            await connection.fetchval("SELECT current_database()")
            != request["database"]
        ):
            raise ValueError("Shared store database target mismatch")
        await connection.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", marker
        )
        for role in [owner, *runtimes]:
            expected_login = role != owner
            existing = await connection.fetchrow(
                "SELECT oid, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                "rolreplication, rolbypassrls, "
                "shobj_description(oid, 'pg_authid') AS marker "
                "FROM pg_roles WHERE rolname=$1",
                role,
            )
            if existing:
                if (
                    existing["marker"] != marker
                    or existing["rolcanlogin"] != expected_login
                    or any(
                        existing[key]
                        for key in (
                            "rolsuper",
                            "rolcreatedb",
                            "rolcreaterole",
                            "rolreplication",
                            "rolbypassrls",
                        )
                    )
                    or await connection.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_auth_members WHERE member=$1 "
                        "OR (roleid=$1 AND member<>(SELECT oid FROM pg_roles "
                        "WHERE rolname=current_user)))",
                        existing["oid"],
                    )
                ):
                    raise ValueError(
                        "Existing shared role ownership or authority differs"
                    )
            else:
                login = "LOGIN" if expected_login else "NOLOGIN"
                await connection.execute(
                    f"CREATE ROLE {identifier(role)} {login} NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                )
                statement = await connection.fetchval(
                    "SELECT format('COMMENT ON ROLE %I IS %L', $1::text, $2::text)",
                    role,
                    marker,
                )
                await connection.execute(statement)
            if expected_login:
                statement = await connection.fetchval(
                    "SELECT format('ALTER ROLE %I PASSWORD %L', $1::text, $2::text)",
                    role,
                    passwords[role],
                )
                await connection.execute(statement)
        existing_schema = await connection.fetchrow(
            "SELECT n.oid, pg_get_userbyid(n.nspowner) AS owner, "
            "obj_description(n.oid, 'pg_namespace') AS marker "
            "FROM pg_namespace n WHERE n.nspname=$1",
            schema,
        )
        if existing_schema:
            if existing_schema["owner"] != owner or existing_schema["marker"] != marker:
                raise ValueError(
                    "Existing shared schema is not owned by this installation"
                )
            if await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace n "
                "CROSS JOIN LATERAL aclexplode(n.nspacl) a "
                "WHERE n.nspname=$1 AND a.grantee=0)",
                schema,
            ):
                raise ValueError("Existing shared schema has public grants")
        else:
            await connection.execute(
                f"CREATE SCHEMA {identifier(schema)} AUTHORIZATION {identifier(owner)}"
            )
            statement = await connection.fetchval(
                "SELECT format('COMMENT ON SCHEMA %I IS %L', $1::text, $2::text)",
                schema,
                marker,
            )
            await connection.execute(statement)
        # Never repair a pre-existing foreign table by adopting it.
        foreign = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 "
            "AND (pg_get_userbyid(c.relowner)<>$2 "
            "OR c.relname NOT LIKE 'harness_%'))",
            schema,
            owner,
        )
        if foreign:
            raise ValueError("Shared schema contains unowned objects")
        await connection.execute(
            f"REVOKE ALL ON SCHEMA {identifier(schema)} FROM PUBLIC"
        )
        await connection.execute(
            f"SET LOCAL search_path TO {identifier(schema)}, pg_catalog"
        )
        if await current_version(connection) > SCHEMA_VERSION:
            raise ValueError("Shared schema is newer than the selected release")
        await connection.execute(f"SET LOCAL ROLE {identifier(owner)}")
        await apply(connection)
        await check_schema_version(connection)
        await connection.execute("RESET ROLE")
        for role in runtimes:
            quoted = identifier(role)
            if await connection.fetchval(
                "SELECT has_schema_privilege($1,$2,'CREATE')", role, schema
            ):
                raise ValueError("Runtime role can create shared store objects")
            for excluded in request["forbidden_schemas"]:
                if await connection.fetchval(
                    "SELECT has_schema_privilege($1,$2,'USAGE') "
                    "OR has_schema_privilege($1,$2,'CREATE')",
                    role,
                    excluded,
                ):
                    raise ValueError("Shared runtime can reach domain schema")
            await connection.execute(
                f"GRANT CONNECT ON DATABASE {identifier(request['database'])} "
                f"TO {quoted}"
            )
            await connection.execute(
                f"GRANT USAGE ON SCHEMA {identifier(schema)} TO {quoted}"
            )
            await connection.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                f"{identifier(schema)} TO {quoted}"
            )
            await connection.execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "
                f"{identifier(schema)} TO {quoted}"
            )
            await connection.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {identifier(owner)} "
                f"IN SCHEMA {identifier(schema)} "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {quoted}"
            )
            await connection.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {identifier(owner)} "
                f"IN SCHEMA {identifier(schema)} "
                f"GRANT USAGE, SELECT ON SEQUENCES TO {quoted}"
            )
            await connection.execute(
                f"ALTER ROLE {quoted} IN DATABASE {identifier(request['database'])} "
                f"SET search_path TO {identifier(schema)}, pg_catalog"
            )
        for role in request["forbidden_roles"]:
            if not await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=$1)", role
            ):
                raise ValueError(
                    "Excluded domain role is missing; isolation cannot be proved"
                )
            if await connection.fetchval(
                "SELECT has_schema_privilege($1, $2, 'USAGE') "
                "OR has_schema_privilege($1, $2, 'CREATE')",
                role,
                schema,
            ):
                raise ValueError("Domain role can reach shared store")
        # Reject unexpected direct grants, including PUBLIC table access. We do
        # not silently remove someone else's grants to make preparation pass.
        allowed = [owner, *runtimes]
        if await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace n "
            "CROSS JOIN LATERAL aclexplode(n.nspacl) a WHERE n.nspname=$1 "
            "AND (a.grantee=0 OR pg_get_userbyid(a.grantee)<>ALL($2::text[]))) "
            "OR EXISTS (SELECT 1 FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE n.nspname=$1 "
            "AND (a.grantee=0 OR pg_get_userbyid(a.grantee)<>ALL($2::text[])))",
            schema,
            allowed,
        ):
            raise ValueError("Shared store has unexpected grants")
    return {
        "status": "prepared",
        "schema": schema,
        "schema_version": SCHEMA_VERSION,
        "owner_role": owner,
        "runtime_roles": runtimes,
    }
