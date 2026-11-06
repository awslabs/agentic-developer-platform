"""Both deployed histories converge without losing Product provider bindings."""

import pytest
from sqlalchemy import create_engine, inspect, text

from tests.migrations.conftest_postgres import upgrade
from tests.migrations.head_contract import GATEWAY_HEAD
from tests.migrations.test_146_project_budgets import seed


@pytest.mark.parametrize("prior", ["gitlab", "native", "both"])
def test_upgrade_from_deployed_gitlab_or_l5_head_retains_product_home(pg_url, prior):
    upgrade(pg_url, "149_merge_l5_project_budgets")
    seed(pg_url)
    if prior in {"gitlab", "both"}:
        upgrade(pg_url, "150_gitlab_product_homes")
    if prior in {"native", "both"}:
        upgrade(pg_url, "150_l5_devops_native_runs")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            if prior in {"gitlab", "both"}:
                connection.execute(
                    text("""INSERT INTO product_gitlab_homes
                        (tenant_id, product_id, instance_url, group_id, full_path, status, members)
                        VALUES ('tenant', 'existing', 'https://gitlab.example.test', 42,
                                'products/existing', 'ready', '{"owner": 40}')""")
                )
                before = dict(connection.execute(text("SELECT * FROM product_gitlab_homes")).mappings().one())
            else:
                before = None

        upgrade(pg_url)
        upgrade(pg_url)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [GATEWAY_HEAD]
            homes = connection.execute(text("SELECT * FROM product_gitlab_homes")).mappings().all()
            assert [dict(row) for row in homes] == ([before] if before is not None else [])
            inspector = inspect(connection)
            assert {"product_gitlab_homes", "l5_devops_native_run_identities", "l5_devops_native_event_receipts"} <= set(inspector.get_table_names())
            assert {"origin", "scope", "next_check_at"} <= {column["name"] for column in inspector.get_columns("l5_service_runs")}
            assert connection.scalar(text("SELECT name FROM projects WHERE id = 'original'")) == "Original"
    finally:
        engine.dispose()
