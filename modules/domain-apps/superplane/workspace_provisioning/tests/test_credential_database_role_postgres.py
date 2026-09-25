"""Restricted renewal roles cannot gain authority through column-only SQL grants."""
# ruff: noqa: F811

from uuid import uuid4

import pytest
from superplane_bootstrap.errors import BootstrapRefused

from workspace_provisioning.credential_controller.registry import (
    require_runtime_database_role,
)

from .postgres_bridge import requires_harness_postgres
from .test_bootstrap_runtime_postgres import bootstrap_harness  # noqa: F401

pytestmark = requires_harness_postgres


@pytest.mark.parametrize(
    "extra",
    [
        None,
        "UPDATE (sharing_enabled) ON clusters",
        "UPDATE (status) ON workspaces",
        "UPDATE (state) ON cluster_memberships",
        "UPDATE (enabled) ON cluster_credential_authorities",
        "INSERT (authority_id) ON cluster_credential_authorities",
        "TRUNCATE ON cluster_credential_authorities",
        "TRIGGER ON clusters",
    ],
)
def test_runtime_role_checks_effective_column_and_table_writes(
    bootstrap_harness, extra
):
    harness = bootstrap_harness
    role = "credential_test_" + uuid4().hex

    async def run():
        async with harness.connect() as connection:
            transaction = connection.transaction()
            await transaction.start()
            try:
                schema = await connection.fetchval("SELECT current_schema()")
                quoted_schema = '"' + schema.replace('"', '""') + '"'
                await connection.execute(f'CREATE ROLE "{role}" NOLOGIN')
                await connection.execute(
                    f'GRANT USAGE ON SCHEMA {quoted_schema} TO "{role}"'
                )
                await connection.execute(
                    f'GRANT SELECT ON ALL TABLES IN SCHEMA {quoted_schema} TO "{role}"'
                )
                await connection.execute(
                    "GRANT UPDATE (holder,fence_token,lease_expires_at) "
                    f'ON cluster_credential_authorities TO "{role}"'
                )
                if extra:
                    await connection.execute(f'GRANT {extra} TO "{role}"')
                await connection.execute(f'SET LOCAL ROLE "{role}"')
                if extra:
                    with pytest.raises(BootstrapRefused, match="database role"):
                        await require_runtime_database_role(connection)
                else:
                    await require_runtime_database_role(connection)
            finally:
                await transaction.rollback()

    harness.run(run())
