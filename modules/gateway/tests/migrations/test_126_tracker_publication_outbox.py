"""The tracker outbox migration leaves old plans and flows intact."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.head_contract import HEAD_DOWNGRADE_REFUSAL

GATEWAY = Path(__file__).resolve().parents[2]


def test_tracker_publication_outbox_is_additive():
    spec = importlib.util.spec_from_file_location("tracker_126", GATEWAY / "alembic/versions/126_tracker_publication_outbox.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    config = Config(str(GATEWAY / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY / "alembic"))
    assert ScriptDirectory.from_config(config).get_revision(migration.revision) is not None
    assert migration.down_revision == "125_flow_planning_references"
    assert len(migration.revision) <= 32

    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE orchestration_flows (id TEXT PRIMARY KEY, product_id TEXT, origin_ref TEXT, tracker_refs TEXT)"))
        connection.execute(sa.text("CREATE TABLE orchestration_accepted_plans (flow_id TEXT, plan_document TEXT, plan_hash TEXT)"))
        connection.execute(sa.text("INSERT INTO orchestration_flows VALUES ('legacy', 'product-one', NULL, NULL)"))
        connection.execute(sa.text("INSERT INTO orchestration_accepted_plans VALUES ('legacy', '{\"nodes\":[]}', 'old-hash')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(sa.text("SELECT product_id, origin_ref, tracker_refs FROM orchestration_flows")).one() == (
            "product-one",
            None,
            None,
        )
        assert connection.execute(sa.text("SELECT plan_document, plan_hash FROM orchestration_accepted_plans")).one() == ('{"nodes":[]}', "old-hash")
        columns = {column["name"] for column in sa.inspect(connection).get_columns("orchestration_tracker_publications")}
        assert {"id", "org_id", "repository_id", "epic_ref", "body_region", "completed_at"} <= columns
        connection.execute(
            sa.text(
                "INSERT INTO orchestration_tracker_publications (id, org_id, flow_id, repository_id, epic_ref, repo, "
                "installation_id, issue_number, body_region, version, watermark, created_at) VALUES "
                "('operation', 'tenant', 'legacy', :repository_id, 'epic', 'example/planning', 77, 42, 'status', 1, 1, CURRENT_TIMESTAMP)"
            ),
            {"repository_id": 2**31 + 7},
        )
        assert connection.execute(sa.text("SELECT repository_id FROM orchestration_tracker_publications")).scalar_one() == 2**31 + 7
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "orchestration_tracker_publications" not in sa.inspect(connection).get_table_names()
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert "orchestration_tracker_publications" in sa.inspect(connection).get_table_names()
    engine.dispose()


def test_postgres_124_126_124_126_preserves_plan_and_wide_repository_id(pg_url):
    from sqlalchemy import create_engine, inspect, text

    from tests.migrations.conftest_postgres import downgrade, upgrade

    upgrade(pg_url, "124_flow_product_repo")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO orchestration_flows (id, org_id, slug, title) VALUES ('flow-legacy', 'tenant', 'legacy', 'Legacy')"))
            connection.execute(
                text(
                    "INSERT INTO orchestration_accepted_plans (id, org_id, flow_id, version, plan_document, plan_hash) "
                    "VALUES ('plan-legacy', 'tenant', 'flow-legacy', 1, '{\"nodes\":[]}'::jsonb, 'old-hash')"
                )
            )

        for cycle in range(2):
            upgrade(pg_url, "126_tracker_publication_outbox")
            with engine.begin() as connection:
                columns = {column["name"]: column for column in inspect(connection).get_columns("orchestration_tracker_publications")}
                assert str(columns["repository_id"]["type"]).upper() == "BIGINT"
                assert {"origin_ref", "tracker_refs"} <= {column["name"] for column in inspect(connection).get_columns("orchestration_flows")}
                connection.execute(
                    text(
                        "INSERT INTO orchestration_tracker_publications "
                        "(id, org_id, flow_id, repository_id, epic_ref, repo, installation_id, "
                        "issue_number, body_region, version, watermark, created_at) "
                        "VALUES (:operation, 'tenant', 'flow-legacy', :repository, 'epic', "
                        "'example/planning', 303, 42, 'status', 1, 1, CURRENT_TIMESTAMP)"
                    ),
                    {"operation": f"operation-{cycle}", "repository": 2**31 + 7},
                )
                assert connection.execute(text("SELECT repository_id FROM orchestration_tracker_publications")).scalar_one() == 2**31 + 7
                assert (
                    connection.execute(text("SELECT plan_hash FROM orchestration_accepted_plans WHERE id = 'plan-legacy'")).scalar_one() == "old-hash"
                )
            if cycle == 0:
                downgrade(pg_url, "124_flow_product_repo")
                with engine.connect() as connection:
                    assert "orchestration_tracker_publications" not in inspect(connection).get_table_names()
                    assert {"origin_ref", "tracker_refs"}.isdisjoint(
                        {column["name"] for column in inspect(connection).get_columns("orchestration_flows")}
                    )
                    assert (
                        connection.execute(text("SELECT plan_hash FROM orchestration_accepted_plans WHERE id = 'plan-legacy'")).scalar_one()
                        == "old-hash"
                    )
    finally:
        engine.dispose()


def test_postgres_head_refusal_and_reupgrade_preserve_plan_and_publication(pg_url):
    from tests.migrations.conftest_postgres import run_alembic, upgrade

    upgrade(pg_url, "124_flow_product_repo")
    engine = sa.create_engine(pg_url)
    config = Config(str(GATEWAY / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY / "alembic"))
    expected_head = ScriptDirectory.from_config(config).get_current_head()

    def snapshot():
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == expected_head
            return {
                table: connection.execute(sa.text(f"SELECT * FROM {table} ORDER BY id")).mappings().all()
                for table in ("orchestration_flows", "orchestration_accepted_plans", "orchestration_tracker_publications")
            }

    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("INSERT INTO orchestration_flows (id, org_id, slug, title) VALUES ('flow-legacy', 'tenant', 'legacy', 'Legacy')")
            )
            connection.execute(
                sa.text(
                    "INSERT INTO orchestration_accepted_plans (id, org_id, flow_id, version, plan_document, plan_hash) "
                    "VALUES ('plan-legacy', 'tenant', 'flow-legacy', 1, '{\"nodes\":[]}'::jsonb, 'old-hash')"
                )
            )
        upgrade(pg_url, "head")
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO orchestration_tracker_publications "
                    "(id, org_id, flow_id, repository_id, epic_ref, repo, installation_id, "
                    "issue_number, body_region, version, watermark, created_at) "
                    "VALUES ('operation', 'tenant', 'flow-legacy', :repository, 'epic', "
                    "'example/planning', 303, 42, 'status', 1, 1, CURRENT_TIMESTAMP)"
                ),
                {"repository": 2**31 + 7},
            )
        before = snapshot()
        assert before["orchestration_accepted_plans"][0]["plan_document"] == {"nodes": []}
        assert before["orchestration_accepted_plans"][0]["plan_hash"] == "old-hash"
        assert before["orchestration_tracker_publications"][0]["repository_id"] == 2**31 + 7
        refused = run_alembic(pg_url, "downgrade", "124_flow_product_repo")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        assert snapshot() == before
        upgrade(pg_url, "head")
        assert snapshot() == before
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_postgres_outbox_sql_failure_keeps_tick_transaction_usable(pg_url, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.orchestration import repository_binding, tick_handler
    from src.orchestration.dispatch_pass import DispatchPassReport
    from src.orchestration.models import OrchestrationFlow, OrchestrationTrackerPublication
    from src.orchestration.tracker_outbox import stage_tracker_publications
    from src.orchestration.tracker_provider import GitHubTrackerProvider
    from tests.migrations.conftest_postgres import to_async_url, upgrade
    from tests.orchestration.test_tick_tracker_projection import _seed

    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _seed(factory, states=["running"])
        monkeypatch.setenv("FEATURE_ORCHESTRATION_ENGINE_ENABLED", "true")
        monkeypatch.setenv("BG_ORCH_DISPATCH_REPO", "aws-e/adp")

        async def verified(_session, **kwargs):
            return repository_binding.VerifiedRepository(
                "github",
                kwargs["repository_id"],
                kwargs["name"],
                kwargs["installation_id"],
                kwargs["name"],
                kwargs["issue"],
                kwargs["operation"],
            )

        async def dispatch(session):
            await session.execute(sa.update(OrchestrationFlow).values(title="Committed dispatch state"))
            return DispatchPassReport()

        async def sql_failure(session, report):
            await stage_tracker_publications(session, report)
            await session.execute(sa.text("INSERT INTO orchestration_tracker_publications (id) VALUES ('invalid')"))

        async def forbidden_provider_read(self, **kwargs):
            raise AssertionError("a failed staging transaction must not flush tracker writes")

        monkeypatch.setattr(repository_binding, "resolve_story_repository", verified)
        monkeypatch.setattr(tick_handler, "run_dispatch_pass", dispatch)
        monkeypatch.setattr(tick_handler, "stage_tracker_publications", sql_failure)
        monkeypatch.setattr(GitHubTrackerProvider, "read_issue_body", forbidden_provider_read)
        result = await tick_handler._run_locked(factory)
        assert result.tracker_projection_report.errors == 1
        assert result.tracker_projection_report.pending == []
        async with factory() as session:
            assert (await session.scalars(sa.select(OrchestrationFlow))).one().title == "Committed dispatch state"
            assert (await session.scalars(sa.select(OrchestrationTrackerPublication))).all() == []
    finally:
        await engine.dispose()
