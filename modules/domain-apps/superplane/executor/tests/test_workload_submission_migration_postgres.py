"""Migration DDL must work through the production prepared-statement boundary."""

import ast
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from tests.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.mark.parametrize("retained", [False, True])
async def test_submission_migration_prepared_ddl_and_evidence_retention(
    postgres_server, retained
):
    schema = "submission_" + uuid4().hex
    connection = await asyncpg.connect(postgres_server)
    tree = ast.parse(
        (
            Path(__file__).resolve().parents[2]
            / "src/superplane-api/alembic/versions/041_controller_workload_submissions.py"
        ).read_text()
    )
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }

    async def execute(statement):
        argument = ast.literal_eval(statement.value.args[0])
        sql = (
            "DROP TABLE " + argument
            if statement.value.func.attr == "drop_table"
            else argument
        )
        # Raw execute(sql) accepts multiple commands; Alembic+SQLAlchemy+asyncpg
        # prepares each op.execute instead, and must reject combined top-level DDL.
        await (await connection.prepare(sql)).fetch()

    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        await connection.execute(f'SET search_path TO "{schema}"')
        for statement in functions["upgrade"].body:
            await execute(statement)
        if retained:
            await connection.execute(
                "INSERT INTO controller_workload_submissions VALUES('operation','Job','namespace','name',"
                "'org','workspace','allocation','plan','step','attempt',1,'{}','digest')"
            )
            for sql in (
                "UPDATE controller_workload_submissions SET body='changed'",
                "DELETE FROM controller_workload_submissions",
            ):
                with pytest.raises(
                    asyncpg.RaiseError,
                    match="original workload submission is immutable",
                ):
                    await (await connection.prepare(sql)).fetch()
            with pytest.raises(asyncpg.RaiseError, match="must be preserved"):
                async with connection.transaction():
                    for statement in functions["downgrade"].body:
                        await execute(statement)
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM controller_workload_submissions"
                )
                == 1
            )
        else:
            async with connection.transaction():
                for statement in functions["downgrade"].body:
                    await execute(statement)
            assert (
                await connection.fetchval(
                    "SELECT to_regclass('controller_workload_submissions')"
                )
                is None
            )
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
