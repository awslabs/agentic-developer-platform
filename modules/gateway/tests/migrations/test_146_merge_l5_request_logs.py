"""Either migration history can join without losing recovery or analytics rows."""

import pytest
from l5_services.models import DevopsSpaceIntent
from sqlalchemy import create_engine, inspect, select, text

from src.admin.models import RequestLog
from src.shared.models.usage import UsageLog
from tests.admin.test_request_log_queue import record
from tests.migrations.conftest_postgres import run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


@pytest.mark.parametrize(
    "previous_head",
    [
        "145_l5_integration_observation",
        "144_request_logs",
        "146_merge_l5_request_logs",
        "145_conversation_list_indexes",
        "148_l5_results_knowledge",
        "146_project_budgets",
    ],
)
def test_merge_preserves_receipts_and_request_logs_from_either_head(pg_url, previous_head):
    upgrade(pg_url, previous_head)
    tables = {"l5": DevopsSpaceIntent.__table__, "requests": RequestLog.__table__, "usage": UsageLog.__table__}
    request = record()
    rows = {
        "l5": {
            "tenant_id": "tenant",
            "operation_id": "retained-operation",
            "selection": {"product_id": "product"},
            "configuration_revision": 1,
            "intent_hash": "a" * 64,
            "state": "ready",
            "native_space_id": "retained-space",
            "resource_observation": {"space_id": "retained-space"},
            "integration_observation": {"integration_id": "retained-integration"},
            "owned_trigger_ids": ["retained-trigger"],
            "resume_trigger_ids": [],
            "active_executions": [],
        },
        "requests": request,
        "usage": {
            "id": "retained-usage",
            "timestamp": request["timestamp"],
            "org_id": "tenant",
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
            "conversation_id": "retained-conversation",
            "owner_user_id": "user",
            "harness": "codex",
        },
    }
    installed = {"usage"}
    if previous_head in {"145_l5_integration_observation", "146_merge_l5_request_logs", "148_l5_results_knowledge"}:
        installed.add("l5")
    if previous_head != "145_l5_integration_observation":
        installed.add("requests")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            for name, table in tables.items():
                assert inspect(connection).has_table(table.name) is (name in installed)
                if name in installed:
                    connection.execute(table.insert().values(rows[name]))
            before = {name: dict(connection.execute(text(f"SELECT * FROM {tables[name].name}")).mappings().one()) for name in installed}

        upgrade(pg_url)
        with engine.begin() as connection:
            for table in (tables["l5"], tables["requests"]):
                assert {column["name"] for column in inspect(connection).get_columns(table.name)} == set(table.columns.keys())
            indexes = {index["name"]: index for index in inspect(connection).get_indexes("usage_logs")}
            for suffix, columns in (("user", ["user_id", "timestamp"]), ("owner", ["owner_user_id", "timestamp"])):
                index = indexes[f"ix_usage_conversation_{suffix}"]
                assert index["column_names"] == columns
                assert "harness IS NOT NULL" in str(index["dialect_options"]["postgresql_where"])
            for name, table in tables.items():
                if name in installed:
                    after = dict(connection.execute(select(table)).mappings().one())
                    assert {column: after[column] for column in before[name]} == before[name]
                else:
                    connection.execute(table.insert().values(rows[name]))
            usage = connection.execute(select(tables["usage"])).mappings().one()
            assert all(usage[column] is None for column in ("product_id", "project_id", "flow_id"))
            snapshots = {name: dict(connection.execute(select(table)).mappings().one()) for name, table in tables.items()}

        upgrade(pg_url)
        refused = run_alembic(pg_url, "downgrade", "141_normalize_environments")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            for name, table in tables.items():
                assert dict(connection.execute(select(table)).mappings().one()) == snapshots[name]
    finally:
        engine.dispose()
