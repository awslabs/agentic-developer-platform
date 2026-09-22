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

from app.capability_probes import probe_all
from app.config import require_database_url, settings
from app.database import engine
from app.schema_boundary import connect_args, schema_name


async def capability_details() -> dict[str, dict]:
    """Per-port probe reports: what each configured adapter actually refused.

    The richer form, for the readout an operator reads. Each entry carries the
    gate's verdict (``composed``), the contract suite's stricter one
    (``conformant``), the probe verdicts observed, and the offline-only limitation
    that travels with every conformance report.
    """
    return await probe_all()


def capabilities_from(details: dict[str, dict]) -> dict[str, bool]:
    """Fold probe reports into the four booleans the existing consumers read.

    ``.get("composed") is True`` rather than a truthiness test: a report missing
    the key is a bug in the probe layer, and the safe reading of "I could not tell"
    is False. Defaulting a missing key to True is how a gate silently stops gating.
    """
    return {name: report.get("composed") is True for name, report in details.items()}


async def capabilities_async() -> dict[str, bool]:
    """The capability booleans, for callers already inside an event loop.

    Two of the four consumers — the FastAPI boot gate and the ``/internal/installation``
    router — run in a running loop, where ``asyncio.run`` raises. They call this;
    the CLI calls the sync wrapper below.
    """
    return capabilities_from(await capability_details())


def capabilities() -> dict[str, bool]:
    """The capability booleans, for synchronous callers (the CLI).

    Same four keys and same fail-closed direction as the ``is not None`` check this
    replaces — but each boolean is now established by calling the adapter and
    requiring it to refuse an unauthorized probe, so an object that merely exists
    no longer answers True. See ``app/capability_probes.py`` for why an
    unauthorized read is the safe question to ask on every boot.

    Refuses to run inside an existing loop rather than silently returning something
    wrong: ``asyncio.run`` would raise a confusing "cannot be called from a running
    event loop" from deep in the call, and the correct fix is always to await
    ``capabilities_async`` instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(capabilities_async())
    raise RuntimeError("capabilities() is synchronous; await capabilities_async()")


async def database_check(
    *, migrating: bool = False, verify_role_default: bool = False
) -> dict:
    schema = schema_name(settings.superplane_db_schema)
    if schema is None:
        raise ValueError("isolated schema required")
    if verify_role_default:
        # SkyPilot uses a different driver. Verify the server-side role default,
        # rather than assuming asyncpg's per-connection override applies to it.
        raw_engine = create_async_engine(require_database_url(), connect_args=connect_args(""))
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
        "action", choices=("capabilities", "management-capabilities", "database", "migrate", "readiness")
    )
    args = parser.parse_args(argv)
    try:
        if args.action == "management-capabilities":
            import httpx

            from app.main import app
            from app.management import management_only

            async def probe_management():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://image-local") as client:
                    health = await client.get("/health")
                    registration = await client.post("/internal/controller/reconcile", json={})
                    administration = await client.get("/workspaces")
                    legacy = await client.post("/internal/vault-sync/trigger", json={})
                    return (management_only() and health.json().get("domain_auth_enforced") is True
                            and registration.status_code == administration.status_code == 401
                            and legacy.status_code == 503)

            supported = asyncio.run(probe_management())
            print(json.dumps({"controller_management": supported, "governed_provisioning": False, "database_verified": False}))
            return 0 if supported else 2
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
            details = asyncio.run(capability_details())
            result = capabilities_from(details)
            # `capabilities` keeps the exact shape the installer's preflight parses
            # (`runner.py:288`). `probes` is additive, so a refusal says which probe
            # failed instead of only that something did — the old output left an
            # operator with four booleans and no next step.
            print(json.dumps({"capabilities": result, "probes": details}))
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
