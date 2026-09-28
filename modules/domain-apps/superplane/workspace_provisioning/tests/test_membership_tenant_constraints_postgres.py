"""The database rejects cross-organization membership and issuer registration."""
# ruff: noqa: F811

from uuid import UUID, uuid4

import pytest
from workspace_bootstrap.tests import conftest as identities
from .test_bootstrap_runtime_postgres import bootstrap_harness  # noqa: F401
from .postgres_bridge import requires_harness_postgres

pytestmark = requires_harness_postgres


def test_foreign_membership_and_cluster_authority_fail_database_constraints(
    bootstrap_harness,
):
    async def run():
        import asyncpg

        async with bootstrap_harness.connect() as c:
            org, foreign, workspace, cluster = (
                UUID(identities.ORG_ID),
                uuid4(),
                uuid4(),
                uuid4(),
            )
            await c.execute(
                "INSERT INTO organizations(id,name) VALUES($1,'foreign')", foreign
            )
            await c.execute(
                "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default) VALUES($1,$2,'member','namespace','Provisioning',false)",
                workspace,
                org,
            )
            await c.execute(
                "INSERT INTO clusters(id,org_id,name,status) VALUES($1,$2,'shared','Ready')",
                cluster,
                org,
            )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with c.transaction():
                    await c.execute(
                        "INSERT INTO cluster_memberships(id,org_id,workspace_id,cluster_id,generation,namespace) VALUES($1,$2,$3,$4,$5,'member')",
                        uuid4(),
                        foreign,
                        workspace,
                        cluster,
                        "a" * 64,
                    )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with c.transaction():
                    await c.execute(
                        "INSERT INTO cluster_credential_authorities(authority_id,org_id,cluster_id,document_json) VALUES($1,$2,$3,'{}')",
                        uuid4(),
                        foreign,
                        cluster,
                    )
            assert await c.fetchval("SELECT count(*) FROM cluster_memberships") == 0
            assert (
                await c.fetchval("SELECT count(*) FROM cluster_credential_authorities")
                == 0
            )

    bootstrap_harness.run(run())
