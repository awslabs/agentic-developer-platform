"""A serving-image rollback must not destroy catalog histories or receipts."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.catalog.models import CatalogChange, CatalogReceipt, MembershipHead, MembershipVersion, PortfolioHead, PortfolioVersion
from tests.migrations.conftest_postgres import run_alembic, upgrade
from tests.migrations.head_contract import HEAD_DOWNGRADE_REFUSAL


def test_catalog_migration_extends_single_head():
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    assert "114_catalog_portfolios" in {revision.revision for revision in scripts.walk_revisions()}
    assert scripts.get_revision("114_catalog_portfolios").down_revision == "113_product_scope_guard"


@pytest.mark.parametrize(
    "target_revision,barrier",
    [
        ("114_catalog_portfolios", "Catalog history and receipts must be retained on rollback"),
        ("119_metering_binding_fence", "Metering identity and membership fences must survive rollback"),
        ("head", HEAD_DOWNGRADE_REFUSAL),
    ],
)
def test_catalog_postgres_upgrade_and_rollback_retention(pg_url, target_revision, barrier):
    upgrade(pg_url, "113_product_scope_guard")
    upgrade(pg_url, "114_catalog_portfolios")
    engine = create_engine(pg_url)
    recorded_at = datetime(2026, 10, 20, 12, tzinfo=UTC)
    portfolio = {
        "tenant_id": "catalog-fixture",
        "source_connection_id": "adp.catalog",
        "id": "commerce",
        "revision": 1,
        "data": {"name": "Commerce"},
        "recorded_at": recorded_at,
    }
    membership = {
        "tenant_id": "catalog-fixture",
        "relationship_id": "commerce-checkout",
        "relationship_revision": 1,
        "portfolio_source": "adp.catalog",
        "portfolio_id": "commerce",
        "portfolio_revision": 1,
        "product_source": "adp.profile",
        "product_id": "checkout",
        "product_revision": 1,
        "project_id": "checkout-project",
        "source_connection_id": "adp.catalog",
        "mapping_version": "catalog-1",
        "source_refs": [
            {
                "tenant_id": "catalog-fixture",
                "project_id": "checkout-project",
                "source_connection_id": "adp.profile",
                "record_type": "product",
                "record_id": "checkout",
                "record_revision": "1",
            }
        ],
        "valid_from": recorded_at,
        "recorded_at": recorded_at,
    }
    models = (PortfolioVersion, PortfolioHead, MembershipVersion, MembershipHead, CatalogChange, CatalogReceipt)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "114_catalog_portfolios"
            assert {model.__tablename__ for model in models} <= set(inspect(connection).get_table_names())
            connection.execute(PortfolioVersion.__table__.insert(), portfolio)
            connection.execute(
                PortfolioHead.__table__.insert(), {key: portfolio[key] for key in ("tenant_id", "source_connection_id", "id", "revision")}
            )
            connection.execute(MembershipVersion.__table__.insert(), membership)
            connection.execute(
                MembershipHead.__table__.insert(),
                {
                    key: membership[key]
                    for key in (
                        "tenant_id",
                        "relationship_id",
                        "relationship_revision",
                        "portfolio_source",
                        "portfolio_id",
                        "product_source",
                        "product_id",
                        "project_id",
                    )
                },
            )
            connection.execute(
                CatalogChange.__table__.insert(),
                {
                    "tenant_id": "catalog-fixture",
                    "id": "catalog-change",
                    "actor_id": "editor",
                    "source_connection_id": "adp.catalog",
                    "record_type": "portfolio_product",
                    "record_id": "commerce-checkout",
                    "native_record_revision": "1",
                    "relationship_revision": 1,
                    "mapping_version": "catalog-1",
                    "source_refs": membership["source_refs"],
                    "valid_from": recorded_at,
                    "recorded_at": recorded_at,
                },
            )
            connection.execute(
                CatalogReceipt.__table__.insert(),
                {
                    "tenant_id": "catalog-fixture",
                    "actor_id": "editor",
                    "operation": "add_membership:commerce",
                    "idempotency_key": "catalog-fixture-membership",
                    "payload_digest": "a" * 64,
                    "outcome": {"id": "commerce-checkout", "revision": "1", "change_id": "catalog-change"},
                    "recorded_at": recorded_at,
                },
            )
        with engine.connect() as connection:
            before = {model.__tablename__: connection.execute(select(model.__table__)).mappings().all() for model in models}
        upgrade(pg_url, target_revision)
        with engine.connect() as connection:
            upgraded_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert {model.__tablename__: connection.execute(select(model.__table__)).mappings().all() for model in models} == before
            assert inspect(connection).has_table("product_onboarding_sessions") == (target_revision != "114_catalog_portfolios")
        refused = run_alembic(pg_url, "downgrade", "113_product_scope_guard")
        assert refused.returncode != 0
        assert barrier in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == upgraded_revision
            assert inspect(connection).has_table("product_onboarding_sessions") == (target_revision != "114_catalog_portfolios")
            after = {model.__tablename__: connection.execute(select(model.__table__)).mappings().all() for model in models}
            assert after == before
        upgrade(pg_url, "head")
        root = Path(__file__).resolve().parents[2]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "alembic"))
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(config).get_current_head()
            assert inspect(connection).has_table("product_onboarding_sessions")
            assert {model.__tablename__: connection.execute(select(model.__table__)).mappings().all() for model in models} == before
    finally:
        engine.dispose()


def test_catalog_downgrade_refuses_to_drop_history(monkeypatch):
    migration = Path(__file__).resolve().parents[2] / "alembic/versions/114_catalog_portfolios.py"
    spec = importlib.util.spec_from_file_location("catalog_portfolio_migration", migration)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dropped = []
    monkeypatch.setattr(module.op, "drop_table", lambda name: dropped.append(name))
    with pytest.raises(RuntimeError, match="history and receipts must be retained"):
        module.downgrade()
    assert dropped == []
