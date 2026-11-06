"""Fresh/existing ledger upgrades, failed concurrent builds, and data-safe rollback."""

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration.models import OrchestrationDecision
from tests.migrations.conftest_postgres import downgrade, to_async_url, upgrade
from tests.migrations.legacy_orchestration import seed_flow_node

INDEXES = ("ix_decision_display_category", "ix_decision_display_clearer", "ix_decision_display_observed")


async def test_projection_migration_fresh_existing_and_invalid_index_recovery(pg_url):
    upgrade(pg_url, "104_decision_display_projection")
    connection = psycopg2.connect(pg_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            for name in INDEXES:
                cursor.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(%s)", (name,))
                assert cursor.fetchone() == (True,)
        downgrade(pg_url, "103_dev_box_consolidation")
        engine = create_async_engine(to_async_url(pg_url))
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                flow, node = await seed_flow_node(session, slug="migration-owned", node_ref="story", kind="story")
                for number in range(2):
                    session.add(
                        OrchestrationDecision(
                            id=f"{number:036d}",
                            org_id=flow.org_id,
                            flow_id=flow.id,
                            node_id=node.id,
                            kind="node_dispatched",
                            actor_id="fixture",
                            actor_role="service",
                            actor_kind="service",
                            reason='{"attempt":1}',
                        )
                    )
                await session.commit()
        finally:
            await engine.dispose()

        with connection.cursor() as cursor:
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cursor.execute("CREATE UNIQUE INDEX CONCURRENTLY ix_decision_display_category ON orchestration_decisions (kind)")
            cursor.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass('ix_decision_display_category')")
            assert cursor.fetchone() == (False,)
        upgrade(pg_url, "104_decision_display_projection")
        with connection.cursor() as cursor:
            for name in INDEXES:
                cursor.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(%s)", (name,))
                assert cursor.fetchone() == (True,)
            cursor.execute("SELECT id, reason FROM orchestration_decisions ORDER BY id")
            before = cursor.fetchall()
            assert len(before) == 2
        downgrade(pg_url, "103_dev_box_consolidation")
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, reason FROM orchestration_decisions ORDER BY id")
            assert cursor.fetchall() == before
            for name in INDEXES:
                cursor.execute("SELECT to_regclass(%s)", (name,))
                assert cursor.fetchone() == (None,)
        upgrade(pg_url, "104_decision_display_projection")
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, reason FROM orchestration_decisions ORDER BY id")
            assert cursor.fetchall() == before
    finally:
        connection.close()
