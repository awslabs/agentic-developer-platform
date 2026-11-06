"""Gateway upgrades retain caller-scoped publication retry receipts."""

import pytest
from l5_services.models import PublicationRetryReceipt
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_publication_retry_bridge_extends_finops_history():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("155_l5_publication_retry").down_revision == "154_l5_finops_ownership"


def test_gateway_upgrade_preserves_scoped_retry_receipts(pg_url):
    upgrade(pg_url, "154_l5_finops_ownership")
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            assert not inspect(connection).has_table(PublicationRetryReceipt.__tablename__)
        upgrade(pg_url, "155_l5_publication_retry")
        with engine.begin() as connection:
            inspector = inspect(connection)
            columns = {column["name"]: column for column in inspector.get_columns(PublicationRetryReceipt.__tablename__)}
            assert set(columns) == set(PublicationRetryReceipt.__table__.columns.keys())
            assert isinstance(columns["response"]["type"], JSONB)
            assert set(inspector.get_pk_constraint(PublicationRetryReceipt.__tablename__)["constrained_columns"]) == {
                "tenant_id",
                "product_id",
                "project_id",
                "service_id",
                "actor_id",
                "idempotency_key",
            }
            receipt = {
                "tenant_id": "tenant-one",
                "product_id": "product-one",
                "project_id": "project-one",
                "service_id": "devops",
                "actor_id": "actor-one",
                "idempotency_key": "retry-receipt-key-0001",
                "publication_id": "publication-one",
                "request_hash": "a" * 64,
                "response": {"publication_id": "publication-one", "state": "pending"},
            }
            connection.execute(PublicationRetryReceipt.__table__.insert().values(**receipt))
            connection.execute(PublicationRetryReceipt.__table__.insert().values({**receipt, "actor_id": "actor-two"}))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(PublicationRetryReceipt.__table__.insert().values(**receipt))
            before = connection.execute(select(PublicationRetryReceipt).order_by(PublicationRetryReceipt.actor_id)).mappings().all()
        upgrade(pg_url, "155_l5_publication_retry")
        upgrade(pg_url)
        refused = run_alembic(pg_url, "downgrade", "154_l5_finops_ownership")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert connection.execute(select(PublicationRetryReceipt).order_by(PublicationRetryReceipt.actor_id)).mappings().all() == before
    finally:
        engine.dispose()
