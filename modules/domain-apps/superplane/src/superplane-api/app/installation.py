"""Image-local installation checks and migration runner. Never prints tool errors.

Run with the image's Python entrypoint, not its default Uvicorn entrypoint.
Production adapters must be composed in the image: request/config booleans cannot
turn a missing trust provider into an available capability.
"""

import argparse
import asyncio
import json
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.database import engine
from app.schema_boundary import connect_args, schema_name
from app.services.credential_evidence import get_credential_evidence_reader
from app.services.provider_authority import get_provider_authority_validator
from app.services.provider_inventory import get_allocation_inventory_reader
from app.services.provisioning import get_operation_facade


def capabilities() -> dict[str, bool]:
    return {
        "credential_evidence": get_credential_evidence_reader() is not None,
        "provider_authority": get_provider_authority_validator() is not None,
        "allocation_inventory": get_allocation_inventory_reader() is not None,
        "operation_facade": get_operation_facade() is not None,
    }


async def database_check(
    *, migrating: bool = False, verify_role_default: bool = False
) -> dict:
    schema = schema_name(settings.superplane_db_schema)
    if schema is None:
        raise ValueError("isolated schema required")
    if verify_role_default:
        # SkyPilot uses a different driver. Verify the server-side role default,
        # rather than assuming asyncpg's per-connection override applies to it.
        raw_engine = create_async_engine(settings.database_url, connect_args=connect_args(""))
        try:
            async with raw_engine.connect() as raw:
                actual = (
                    await raw.execute(text("SELECT current_schema()"))
                ).scalar_one()
                if actual != schema:
                    raise ValueError("database role default schema is not isolated")
        finally:
            await raw_engine.dispose()
    async with engine.connect() as connection:
        # Search-path isolation alone does not remove a role's right to mutate
        # qualified gateway tables. Verify actual privileges before migration.
        row = (
            await connection.execute(
                text(
                    "SELECT current_database(), current_schema(), current_user, "
                    "rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        if row[1] != schema or any(row[3:]):
            raise ValueError("database role/schema boundary refused")
        escalation = (
            await connection.execute(
                text(
                    "SELECT has_database_privilege(current_user,current_database(),'CREATE') "
                    "OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user))"
                )
            )
        ).scalar_one()
        if escalation:
            raise ValueError("database role can acquire authority outside the schema")
        expected_db = os.environ.get("SUPERPLANE_EXPECTED_DATABASE")
        if expected_db and row[0] != expected_db:
            raise ValueError("wrong database")
        outside = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM pg_namespace n WHERE n.nspname <> :schema "
                    "AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
                    "AND (has_schema_privilege(current_user, n.oid, 'CREATE') OR EXISTS ("
                    "SELECT 1 FROM pg_class c WHERE c.relnamespace=n.oid "
                    "AND c.relkind IN ('r','p','v','m','f') "
                    "AND has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')))"
                ),
                {"schema": schema},
            )
        ).scalar_one()
        if outside:
            raise ValueError("role can mutate another schema")
        if migrating:
            can_create = (
                await connection.execute(
                    text(
                        "SELECT has_schema_privilege(current_user, :schema, 'CREATE')"
                    ),
                    {"schema": schema},
                )
            ).scalar_one()
            if not can_create:
                raise ValueError("migration role lacks domain DDL authority")
        present = (
            await connection.execute(
                text("SELECT to_regclass(:table)"),
                {"table": f"{schema}.alembic_version"},
            )
        ).scalar_one()
        revision = None
        if present:
            revisions = (
                (
                    await connection.execute(
                        text(f'SELECT version_num FROM "{schema}".alembic_version')
                    )
                )
                .scalars()
                .all()
            )
            if len(revisions) != 1:
                raise ValueError("ambiguous deployed migration head")
            revision = revisions[0]
        return {
            "database": row[0],
            "schema": schema,
            "revision": revision,
            "role": row[2],
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=("capabilities", "database", "migrate", "readiness")
    )
    args = parser.parse_args(argv)
    try:
        if args.action == "readiness":
            import httpx

            grant = next(
                x
                for x in json.loads(settings.observation_submitters)
                if "budget_monitor/global" in x.get("lease_scopes", [])
            )
            result = httpx.get(
                "http://127.0.0.1:8000/internal/installation",
                headers={"Authorization": grant["credential"]},
                timeout=10,
                follow_redirects=False,
            )
            result.raise_for_status()
            print(json.dumps(result.json()))
            return 0
        if args.action == "capabilities":
            result = capabilities()
            print(json.dumps({"capabilities": result}))
            return 0 if all(result.values()) else 2
        result = asyncio.run(
            database_check(migrating=args.action == "migrate", verify_role_default=True)
        )
        if args.action == "migrate":
            from alembic import command
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            expected = os.environ["SUPERPLANE_EXPECTED_SCHEMA"]
            config = Config("/app/alembic.ini")
            script = ScriptDirectory.from_config(config)
            if script.get_heads() != [expected]:
                raise ValueError("image schema differs from release")
            if result["revision"] is not None:
                script.get_revision(result["revision"])
            command.upgrade(config, expected)
            # asyncio.run uses a fresh event loop; discard the old asyncpg pool.
            engine.sync_engine.dispose(close=False)
            result = asyncio.run(database_check(migrating=True))
            if result["revision"] != expected:
                raise ValueError("migration did not reach the exact release schema")
        print(json.dumps(result))
        return 0
    except Exception:
        # SQLAlchemy/asyncpg errors can embed connection strings or statement
        # parameters. The named stage plus a refusal is sufficient for receipts.
        print(
            json.dumps(
                {
                    "error": f"{args.action} refused; verify domain database, privileges, image capabilities and schema"
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
