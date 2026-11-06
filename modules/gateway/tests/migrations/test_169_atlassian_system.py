"""A shared Jira connection adds storage without changing existing credentials."""

from datetime import UTC, datetime

from sqlalchemy import create_engine, inspect, select, text

from alembic.script import ScriptDirectory
from src.shared.models.atlassian_system import AtlassianSystemConnection, AtlassianSystemKey
from src.shared.models.vault import UserCredential
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_shared_jira_has_one_additive_head():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("169_atlassian_system").down_revision == "168_merge_security_wa"


def test_additive_migration_retains_credentials_and_supports_provider_journal(pg_url):
    upgrade(pg_url, "168_merge_security_wa")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                UserCredential.__table__.insert().values(
                    id="credential",
                    org_id="tenant",
                    service="atlassian-mcp-system",
                    provider="agentcore-identity",
                    credential_type="api_key",
                    label="Shared Jira",
                    secret_arn="agentcore-identity:api-key:provider",
                )
            )
            before = dict(connection.execute(select(UserCredential.__table__)).mappings().one())
        upgrade(pg_url)
        with engine.begin() as connection:
            assert dict(connection.execute(select(UserCredential.__table__)).mappings().one()) == before
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            connection.execute(
                AtlassianSystemConnection.__table__.insert().values(
                    credential_id="credential",
                    org_id="tenant",
                    revision="revision",
                    site_id="site",
                    workload_name="workload",
                    provider_name="provider",
                    allowed_product_ids=["product"],
                    schemas={},
                    status="unverified",
                    created_by="admin",
                    created_at=datetime.now(UTC),
                )
            )
            connection.execute(
                AtlassianSystemKey.__table__.insert().values(
                    provider_name="provider",
                    org_id="tenant",
                    credential_id="credential",
                    created_at=datetime.now(UTC),
                )
            )
            assert connection.scalar(select(AtlassianSystemKey.provider_name)) == "provider"
            indexes = {item["name"] for item in inspect(connection).get_indexes("atlassian_system_connections")}
            assert "ix_atlassian_system_connections_org_id" in indexes
            binding_before = dict(connection.execute(select(AtlassianSystemConnection.__table__)).mappings().one())
            journal_before = dict(connection.execute(select(AtlassianSystemKey.__table__)).mappings().one())

        refused = run_alembic(pg_url, "downgrade", "168_merge_security_wa")
        assert refused.returncode != 0
        assert "Retain Jira Identity provider references" in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(UserCredential.__table__)).mappings().one()) == before
            assert dict(connection.execute(select(AtlassianSystemConnection.__table__)).mappings().one()) == binding_before
            assert dict(connection.execute(select(AtlassianSystemKey.__table__)).mappings().one()) == journal_before
    finally:
        engine.dispose()
