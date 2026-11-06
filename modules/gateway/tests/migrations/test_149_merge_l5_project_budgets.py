"""Both release histories retain financial attribution through the joined head."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text

from src.products.models import ProjectRun
from src.shared.models.usage import UsageLog
from tests.migrations.conftest_postgres import run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL
from tests.migrations.test_146_project_budgets import seed


@pytest.mark.parametrize("previous_head", ["148_l5_results_knowledge", "146_project_budgets"])
def test_join_retains_project_attribution_and_unattributed_history(pg_url, previous_head):
    upgrade(pg_url, "145_conversation_list_indexes")
    seed(pg_url)
    upgrade(pg_url, previous_head)
    attributed = previous_head == "146_project_budgets"
    usage_values = {
        "id": "retained-usage",
        "timestamp": datetime.now(UTC),
        "org_id": "tenant",
        "department_id": "department",
        "team_id": "team",
        "user_id": "owner",
        "account_type": "shared",
        "model": "example-model",
        "input_tokens": 1,
        "output_tokens": 2,
        "cost_usd": Decimal("0.125"),
        "latency_ms": 1,
        "status_code": 200,
    }
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            if attributed:
                usage_values.update(product_id="existing", project_id="original", flow_id="flow", agent_run_id="retained-run")
                connection.execute(
                    ProjectRun.__table__.insert().values(
                        tenant_id="tenant",
                        run_id="retained-run",
                        product_id="existing",
                        project_id="original",
                        flow_id="flow",
                        owner_user_id="owner",
                        relationship_revision=1,
                    )
                )
                run_before = dict(connection.execute(select(ProjectRun)).mappings().one())
            connection.execute(UsageLog.__table__.insert().values(usage_values))
            usage_before = dict(connection.execute(text("SELECT * FROM usage_logs")).mappings().one())

        upgrade(pg_url)
        with engine.connect() as connection:
            usage_after = dict(connection.execute(select(UsageLog)).mappings().one())
            assert {column: usage_after[column] for column in usage_before} == usage_before
            assert [usage_after[column] for column in ("product_id", "project_id", "flow_id")] == (
                ["existing", "original", "flow"] if attributed else [None, None, None]
            )
            if attributed:
                assert dict(connection.execute(select(ProjectRun)).mappings().one()) == run_before
            else:
                assert connection.execute(select(ProjectRun)).first() is None
            assert connection.execute(
                text("SELECT product_id, project_id FROM product_project_bindings WHERE is_base AND product_id = 'existing'")
            ).one() == ("existing", "original")
            assert connection.scalar(text("SELECT count(*) FROM product_project_bindings WHERE is_base")) == 2
            assert connection.scalar(text("SELECT project_id FROM orchestration_flows WHERE id = 'flow'")) == "original"
            assert connection.scalar(text("SELECT name FROM projects WHERE id = 'original'")) == "Original"

        upgrade(pg_url)
        refused = run_alembic(pg_url, "downgrade", "145_conversation_list_indexes")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(UsageLog)).mappings().one()) == usage_after
            if attributed:
                assert dict(connection.execute(select(ProjectRun)).mappings().one()) == run_before
    finally:
        engine.dispose()
