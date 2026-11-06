"""Initiative migration is additive and cannot erase catalog history."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.catalog.models import CatalogReceipt, ContributionHead, ContributionVersion, InitiativeHead, InitiativeVersion, PortfolioVersion
from tests.migrations.conftest_postgres import run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD

MIGRATION = Path(__file__).resolve().parents[2] / "alembic/versions/116_catalog_initiatives.py"


def test_116_is_additive_single_head_and_refuses_downgrade(monkeypatch):
    root = MIGRATION.parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("116_catalog_initiatives").down_revision == "115_product_onboarding_sessions"
    spec = importlib.util.spec_from_file_location("catalog_initiative_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dropped = []
    monkeypatch.setattr(module.op, "drop_table", lambda name: dropped.append(name))
    with pytest.raises(RuntimeError, match="history and receipts must be retained"):
        module.downgrade()
    assert dropped == []


def test_116_postgres_upgrade_preserves_sessions_and_catalog_history(pg_url):
    upgrade(pg_url, "115_product_onboarding_sessions")
    engine = create_engine(pg_url)
    at = datetime(2026, 10, 20, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            connection.execute(
                PortfolioVersion.__table__.insert(),
                {
                    "tenant_id": "example",
                    "source_connection_id": "adp.catalog",
                    "id": "portfolio",
                    "revision": 1,
                    "data": {"name": "Example"},
                    "recorded_at": at,
                },
            )
            connection.execute(
                CatalogReceipt.__table__.insert(),
                {
                    "tenant_id": "example",
                    "actor_id": "editor",
                    "operation": "create_portfolio",
                    "idempotency_key": "retained",
                    "payload_digest": "a" * 64,
                    "outcome": {"id": "portfolio", "revision": "1"},
                    "recorded_at": at,
                },
            )
        upgrade(pg_url, "116_catalog_initiatives")
        models = (InitiativeVersion, InitiativeHead, ContributionVersion, ContributionHead)
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "116_catalog_initiatives"
            tables = set(inspect(connection).get_table_names())
            assert {model.__tablename__ for model in models} <= tables
            assert "product_onboarding_sessions" in tables
            assert connection.scalar(select(PortfolioVersion.id)) == "portfolio"
            assert connection.scalar(select(CatalogReceipt.idempotency_key)) == "retained"
        refused = run_alembic(pg_url, "downgrade", "115_product_onboarding_sessions")
        assert refused.returncode != 0
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "116_catalog_initiatives"
            assert inspect(connection).has_table("product_onboarding_sessions")
            assert inspect(connection).has_table("catalog_contribution_versions")
            assert connection.scalar(select(PortfolioVersion.id)) == "portfolio"
            assert connection.scalar(select(CatalogReceipt.idempotency_key)) == "retained"
    finally:
        engine.dispose()
