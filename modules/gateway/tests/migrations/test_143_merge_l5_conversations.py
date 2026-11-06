"""Both published schema branches converge without losing retained data."""

from datetime import UTC, datetime

import pytest
from l5_services.models.base import Base as L5Base
from sqlalchemy import MetaData, Table, create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_l5_and_conversation_migrations_converge_without_rewriting_parents():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert set(scripts.get_revision("149_merge_l5_project_budgets").down_revision) == {
        "148_l5_results_knowledge",
        "146_project_budgets",
    }
    assert scripts.get_revision("146_project_budgets").down_revision == "145_conversation_list_indexes"
    assert scripts.get_revision("148_l5_results_knowledge").down_revision == "147_merge_l5_list_indexes"
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert set(scripts.get_revision("147_merge_l5_list_indexes").down_revision) == {
        "146_merge_l5_request_logs",
        "145_conversation_list_indexes",
    }
    assert scripts.get_revision("145_conversation_list_indexes").down_revision == "144_request_logs"
    assert set(scripts.get_revision("146_merge_l5_request_logs").down_revision) == {
        "145_l5_integration_observation",
        "144_request_logs",
    }
    assert scripts.get_revision("144_request_logs").down_revision == "143_flow_read_revisions"
    assert scripts.get_revision("145_l5_integration_observation").down_revision == "144_merge_l5_flow_reads"
    assert set(scripts.get_revision("144_merge_l5_flow_reads").down_revision) == {
        "143_merge_l5_conversations",
        "143_flow_read_revisions",
    }
    assert scripts.get_revision("143_flow_read_revisions").down_revision == "142_conversation_identity"
    assert set(scripts.get_revision("143_merge_l5_conversations").down_revision) == {
        "142_l5_security_release",
        "142_conversation_identity",
    }
    for parent in ("142_l5_security_release", "142_conversation_identity"):
        assert scripts.get_revision(parent).down_revision == "141_normalize_environments"


@pytest.mark.parametrize(
    "previous_head",
    [
        "141_normalize_environments",
        "142_l5_security_release",
        "142_conversation_identity",
        "143_merge_l5_conversations",
        "143_flow_read_revisions",
        "144_merge_l5_flow_reads",
        "147_merge_l5_list_indexes",
    ],
)
def test_upgrade_from_each_parent_preserves_usage_and_l5_history(pg_url, previous_head):
    l5_installed = previous_head in {
        "142_l5_security_release",
        "143_merge_l5_conversations",
        "144_merge_l5_flow_reads",
        "147_merge_l5_list_indexes",
    }
    upgrade(pg_url, previous_head)
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            usage = Table("usage_logs", MetaData(), autoload_with=connection)
            values = {
                "id": "retained-usage",
                "timestamp": datetime.now(UTC),
                "department_id": "department",
                "team_id": "team",
                "user_id": "user",
                "account_type": "shared",
                "model": "example-model",
                "input_tokens": 1,
                "output_tokens": 2,
                "cost_usd": 0,
                "latency_ms": 1,
                "status_code": 200,
                "org_id": "tenant",
            }
            if previous_head in {"142_conversation_identity", "143_merge_l5_conversations", "143_flow_read_revisions", "144_merge_l5_flow_reads"}:
                values.update(conversation_id="retained-conversation", owner_user_id="user", harness="codex")
            connection.execute(usage.insert().values(**values))
            usage_before = dict(connection.execute(text("SELECT * FROM usage_logs")).mappings().one())
            if l5_installed:
                connection.execute(
                    text(
                        "INSERT INTO l5_service_configurations "
                        "(tenant_id, configuration_id, product_id, project_id, service_id, "
                        "relationship_revision, owner_id, current_revision, handoff_state) "
                        "VALUES ('tenant', 'retained-config', 'product', 'project', 'security', '1', 'user', 1, 'linked')"
                    )
                )
                configuration_before = connection.execute(text("SELECT * FROM l5_service_configurations")).one()
            if previous_head in {"143_flow_read_revisions", "144_merge_l5_flow_reads"}:
                connection.execute(
                    text("INSERT INTO orchestration_read_revisions VALUES ('tenant', 'retained-flow', '00000000-0000-0000-0000-000000000001')")
                )
                flow_revision_before = connection.execute(text("SELECT * FROM orchestration_read_revisions")).one()

        upgrade(pg_url, "head")
        upgrade(pg_url, "head")
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert set(L5Base.metadata.tables) <= set(inspector.get_table_names())
            for table in L5Base.metadata.sorted_tables:
                assert {column["name"] for column in inspector.get_columns(table.name)} == set(table.columns.keys())
            assert {"conversation_id", "owner_user_id", "harness", "origin", "conversation_title"} <= {
                column["name"] for column in inspector.get_columns("usage_logs")
            }
            assert {"ix_usage_conversation", "ix_usage_owner_time"} <= {index["name"] for index in inspector.get_indexes("usage_logs")}
            usage_after = dict(connection.execute(text("SELECT * FROM usage_logs")).mappings().one())
            assert {column: usage_after[column] for column in usage_before} == usage_before
            if l5_installed:
                assert connection.execute(text("SELECT * FROM l5_service_configurations")).one() == configuration_before
            assert inspector.has_table("orchestration_read_revisions")
            assert {"adp_flow_revision_insert", "adp_flow_revision_update", "adp_flow_revision_delete", "adp_flow_revision_truncate"} <= set(
                connection.scalars(text("SELECT tgname FROM pg_trigger WHERE tgrelid = 'orchestration_flows'::regclass AND NOT tgisinternal"))
            )
            if previous_head in {"143_flow_read_revisions", "144_merge_l5_flow_reads"}:
                assert connection.execute(text("SELECT * FROM orchestration_read_revisions")).one() == flow_revision_before
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD

        refused = run_alembic(pg_url, "downgrade", "141_normalize_environments")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(text("SELECT * FROM usage_logs")).mappings().one()) == usage_after
            if l5_installed:
                assert connection.execute(text("SELECT * FROM l5_service_configurations")).one() == configuration_before
            if previous_head in {"143_flow_read_revisions", "144_merge_l5_flow_reads"}:
                assert connection.execute(text("SELECT * FROM orchestration_read_revisions")).one() == flow_revision_before
    finally:
        engine.dispose()
