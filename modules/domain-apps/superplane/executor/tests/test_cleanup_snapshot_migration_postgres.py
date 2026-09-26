"""Prepared migration statements preserve immutable source snapshot evidence."""

import ast
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from tests.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.mark.parametrize("retained", [False, True])
async def test_cleanup_snapshot_migration_and_retention(postgres_server, retained):
    schema = "cleanup_snapshot_" + uuid4().hex
    c = await asyncpg.connect(postgres_server)
    tree = ast.parse(
        (
            Path(__file__).resolve().parents[2]
            / "src/superplane-api/alembic/versions/042_controller_cleanup_snapshots.py"
        ).read_text()
    )
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    async def execute(statement):
        argument = ast.literal_eval(statement.value.args[0])
        sql = (
            "DROP TABLE " + argument
            if statement.value.func.attr == "drop_table"
            else argument
        )
        await (await c.prepare(sql)).fetch()

    try:
        await c.execute(f'CREATE SCHEMA "{schema}"')
        await c.execute(f'SET search_path TO "{schema}"')
        for statement in functions["upgrade"].body:
            await execute(statement)
        if retained:
            await c.execute(
                "INSERT INTO controller_cleanup_snapshots(snapshot_id,source_operation_id,org_id,workspace_id,allocation_id,source_plan_digest,body,body_sha256,sealed_revision,report_digest,enumeration_binding,report_observations,attempt_id,executor_id,fence_token) VALUES('snapshot','source','org','workspace','allocation','plan','{}','digest','revision','report','enumeration','{}','attempt','executor',1)"
            )
            for sql in (
                "UPDATE controller_cleanup_snapshots SET body='changed'",
                "DELETE FROM controller_cleanup_snapshots",
            ):
                with pytest.raises(asyncpg.RaiseError, match="snapshot is immutable"):
                    await (await c.prepare(sql)).fetch()
            with pytest.raises(asyncpg.RaiseError, match="must be preserved"):
                async with c.transaction():
                    for statement in functions["downgrade"].body:
                        await execute(statement)
            assert (
                await c.fetchval("SELECT count(*) FROM controller_cleanup_snapshots")
                == 1
            )
        else:
            async with c.transaction():
                for statement in functions["downgrade"].body:
                    await execute(statement)
            assert (
                await c.fetchval("SELECT to_regclass('controller_cleanup_snapshots')")
                is None
            )
    finally:
        await c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await c.close()
