"""The joined host head preserves provider bindings and uncertain prevention state."""

import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD
from tests.migrations.test_146_project_budgets import seed


def test_join_preserves_both_revision_histories():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("152_merge_gitlab_prevention").down_revision == (
        "151_merge_gitlab_l5_runs",
        "151_l5_devops_prevention_obs",
    )
    assert scripts.get_revision("151_merge_gitlab_l5_runs").down_revision == (
        "150_gitlab_product_homes",
        "150_l5_devops_native_runs",
    )
    assert scripts.get_revision("151_l5_devops_prevention_obs").down_revision == "150_l5_devops_native_runs"


@pytest.mark.parametrize(
    "prior_revisions",
    [
        ("151_merge_gitlab_l5_runs",),
        ("151_l5_devops_prevention_obs",),
        ("151_merge_gitlab_l5_runs", "151_l5_devops_prevention_obs"),
        ("151_l5_devops_prevention_obs", "151_merge_gitlab_l5_runs"),
    ],
)
def test_upgrade_preserves_prevention_receipts_and_product_homes(pg_url, prior_revisions):
    upgrade(pg_url, "149_merge_l5_project_budgets")
    seed(pg_url)
    for revision in prior_revisions:
        upgrade(pg_url, revision)
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            intents = Table("l5_devops_space_intents", MetaData(), autoload_with=connection)
            values = dict(
                tenant_id="tenant",
                operation_id="setup-operation",
                selection={"project_id": "original"},
                configuration_revision=1,
                intent_hash="a" * 64,
                state="ready",
                native_space_id="space-example",
                ownership="owned",
                owned_trigger_ids=[],
                resume_trigger_ids=[],
                active_executions=["ongoing-run"],
            )
            if "151_l5_devops_prevention_obs" in prior_revisions:
                values.update(
                    prevention_observation={"state": "enabled", "goal_id": "goal-example"},
                    prevention_policy={"enabled": False},
                    prevention_operation_id="pending-pause",
                )
            connection.execute(intents.insert().values(**values))
            before = dict(connection.execute(intents.select()).mappings().one())
            homes_before = []
            if inspect(connection).has_table("product_gitlab_homes"):
                connection.execute(
                    text("""INSERT INTO product_gitlab_homes
                    (tenant_id, product_id, instance_url, group_id, full_path, status, members)
                    VALUES ('tenant', 'existing', 'https://gitlab.example.test', 42,
                            'products/existing', 'ready', '{"owner": 40}')""")
                )
                homes_before = [dict(row) for row in connection.execute(text("SELECT * FROM product_gitlab_homes")).mappings()]

        upgrade(pg_url)
        upgrade(pg_url)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [GATEWAY_HEAD]
            after = dict(connection.execute(text("SELECT * FROM l5_devops_space_intents")).mappings().one())
            assert {key: after[key] for key in before} == before
            for field in ("prevention_observation", "prevention_policy", "prevention_operation_id"):
                assert after[field] == before.get(field)
            assert [dict(row) for row in connection.execute(text("SELECT * FROM product_gitlab_homes")).mappings()] == homes_before
    finally:
        engine.dispose()
