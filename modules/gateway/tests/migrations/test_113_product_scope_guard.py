"""Last-scope migration safety without creating a validation database."""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.shared.models.organization import Organization
from tests.migrations.conftest_postgres import downgrade, upgrade


def load_migration():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/113_product_scope_guard.py"
    spec = importlib.util.spec_from_file_location("product_scope_guard_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_last_scope_migration_is_deferred_and_reversible(monkeypatch):
    migration = load_migration()
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert len(heads) == 1
    onboarding = scripts.get_revision("115_product_onboarding_sessions")
    catalog = scripts.get_revision("114_catalog_portfolios")
    assert onboarding.down_revision == catalog.revision
    assert catalog.down_revision == migration.revision
    assert {migration.revision, catalog.revision, onboarding.revision} <= {
        revision.revision for revision in scripts.iterate_revisions(heads[0], migration.down_revision)
    }
    assert migration.down_revision == "112_product_profile_foundation"
    commands = Mock()
    monkeypatch.setattr(migration, "op", commands)
    migration.upgrade()
    statements = [call.args[0] for call in commands.execute.call_args_list]
    assert len(statements) == 3
    assert "FOR UPDATE" in statements[0]
    assert "valid_to IS NULL" in statements[0]
    assert "ERRCODE = '23514'" in statements[0]
    for statement in statements[1:]:
        assert "DEFERRABLE INITIALLY DEFERRED" in statement
    assert "AFTER INSERT OR UPDATE ON products" in statements[1]
    assert "AFTER UPDATE OR DELETE ON product_project_bindings" in statements[2]
    commands.reset_mock()
    migration.downgrade()
    assert [call.args[0] for call in commands.execute.call_args_list] == [
        "DROP TRIGGER product_scope_binding_guard ON product_project_bindings",
        "DROP TRIGGER product_scope_product_guard ON products",
        "DROP FUNCTION product_scope_current_guard()",
    ]


def test_organization_fixture_includes_required_columns():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            Organization.__table__.create(connection)
            connection.execute(Organization.__table__.insert().values(id="fixture-org", name="Fixture organization"))
            actual = connection.execute(
                select(
                    Organization.aws_accounts,
                    Organization.role_mappings,
                    Organization.settings,
                    Organization.github_installation_ids,
                    Organization.cognito_client_ids,
                )
            ).one()
            assert actual == ([], {}, {}, [], [])
    finally:
        engine.dispose()


@pytest.mark.skipif(
    sys.version_info >= (3, 13) and not os.environ.get("BG_TEST_POSTGRES_URI"),
    reason="Python 3.13 needs an already-running PostgreSQL test server",
)
def test_postgres_rejects_orphan_at_commit(pg_url):
    upgrade(pg_url, "113_product_scope_guard")
    engine = create_engine(pg_url)
    try:
        with pytest.raises(IntegrityError, match="nonarchived Product must retain a current Project") as create_error:
            with engine.begin() as connection:
                connection.execute(Organization.__table__.insert().values(id="scope-guard-org", name="Scope guard org"))
                connection.execute(
                    text("""
                    INSERT INTO products (tenant_id, id, name, purpose, intended_users, owner_user_id, starting_point, created_by)
                    VALUES ('scope-guard-org', 'scope-guard-product', 'Planner', 'Test', '[]', 'scope-guard-owner', 'idea', 'scope-guard-owner')
                """)
                )
        assert create_error.value.orig.pgcode == "23514"
        assert create_error.value.orig.diag.constraint_name == "ck_product_current_project"
        with engine.begin() as connection:
            connection.execute(Organization.__table__.insert().values(id="scope-guard-org", name="Scope guard org"))
            connection.execute(
                text("""
                INSERT INTO products (tenant_id, id, name, purpose, intended_users, owner_user_id, starting_point, created_by)
                VALUES ('scope-guard-org', 'scope-guard-product', 'Planner', 'Test', '[]', 'scope-guard-owner', 'idea', 'scope-guard-owner')
            """)
            )
            connection.execute(
                text("""
                INSERT INTO projects (tenant_id, id, name, owner_user_id)
                VALUES ('scope-guard-org', 'scope-guard-project', 'Scope', 'scope-guard-owner')
            """)
            )
            connection.execute(
                text("""
                INSERT INTO product_project_bindings (tenant_id, id, product_id, project_id, provenance)
                VALUES ('scope-guard-org', 'scope-guard-binding', 'scope-guard-product', 'scope-guard-project', '{}')
            """)
            )
        with engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO projects (tenant_id, id, name, owner_user_id)
                VALUES ('scope-guard-org', 'scope-guard-second', 'Second', 'scope-guard-owner')
            """)
            )
            connection.execute(
                text("""
                INSERT INTO product_project_bindings (tenant_id, id, product_id, project_id, provenance)
                VALUES ('scope-guard-org', 'scope-guard-binding-2', 'scope-guard-product', 'scope-guard-second', '{}')
            """)
            )
            connection.execute(
                text("""
                UPDATE product_project_bindings SET valid_to = valid_from + INTERVAL '1 second'
                WHERE tenant_id = 'scope-guard-org' AND id = 'scope-guard-binding'
            """)
            )
        with pytest.raises(IntegrityError, match="nonarchived Product must retain a current Project") as last_error:
            with engine.begin() as connection:
                connection.execute(
                    text("""
                    UPDATE product_project_bindings SET valid_to = valid_from + INTERVAL '1 second'
                    WHERE tenant_id = 'scope-guard-org' AND id = 'scope-guard-binding-2'
                """)
                )
        assert last_error.value.orig.pgcode == "23514"
        assert last_error.value.orig.diag.constraint_name == "ck_product_current_project"
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("""
                SELECT count(*) FROM product_project_bindings
                WHERE tenant_id = 'scope-guard-org' AND valid_to IS NULL
            """)
                )
                == 1
            )
        downgrade(pg_url, "112_product_profile_foundation")
    finally:
        engine.dispose()
