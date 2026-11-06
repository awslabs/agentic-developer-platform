"""Both existing migration branches converge without losing shared credentials."""

import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

from alembic.script import ScriptDirectory
from src.shared.models.base import utcnow
from src.shared.models.vault import UserCredential
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


@pytest.mark.parametrize("initial", ["169_atlassian_system", "180_l5_knowledge_evidence"])
def test_forge_merges_existing_branches_and_preserves_service_method(pg_url, initial):
    upgrade(pg_url, initial)
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            if initial == "169_atlassian_system":
                connection.execute(
                    UserCredential.__table__.insert().values(
                        id="credential",
                        org_id="tenant",
                        service="atlassian-mcp-system",
                        provider="agentcore-identity",
                        credential_type="api_key",
                        label="Shared",
                        secret_arn="provider-reference",
                    )
                )
                table = Table("atlassian_system_connections", MetaData(), autoload_with=connection)
                connection.execute(
                    table.insert().values(
                        credential_id="credential",
                        org_id="tenant",
                        revision="revision",
                        site_id="site",
                        workload_name="workload",
                        provider_name="provider",
                        allowed_product_ids=["product"],
                        schemas={},
                        status="ready",
                        created_by="admin",
                        created_at=utcnow(),
                    )
                )
        upgrade(pg_url)
        scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
        assert scripts.get_heads() == [GATEWAY_HEAD]
        assert scripts.get_revision("181_atlassian_forge").down_revision == ("169_atlassian_system", "180_l5_knowledge_evidence")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert "l5_devops_knowledge_evidence" in inspect(connection).get_table_names()
            table = Table("atlassian_system_connections", MetaData(), autoload_with=connection)
            if initial == "169_atlassian_system":
                row = connection.execute(select(table)).mappings().one()
                assert row["connection_method"] == "service_account"
                assert row["revision"] == "revision" and row["provider_name"] == "provider"
                assert row["allowed_product_ids"] == ["product"]
                assert row["forge_installation_id"] is None and row["pairing_hash"] is None
        refusal = run_alembic(pg_url, "downgrade", initial)
        assert refusal.returncode != 0
        assert "Retain Jira Identity provider references" in refusal.stderr
    finally:
        engine.dispose()
