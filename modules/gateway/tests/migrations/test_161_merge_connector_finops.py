"""Either deployed branch reaches one head without losing retry history."""

import pytest
from l5_services.models import PublicationRetryReceipt, WorkerAttempt
from sqlalchemy import create_engine, inspect, select, text

from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_connector_and_finops_histories_converge_without_reparenting():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("161_merge_connector_finops").down_revision == (
        "160_connector_cell_replay",
        "156_l5_finops_inquiry",
    )
    assert scripts.get_revision("156_connector_cell_authority").down_revision == "155_l5_publication_retry"
    assert scripts.get_revision("156_l5_finops_inquiry").down_revision == "155_l5_publication_retry"
    connector_revisions = {
        "156_connector_cell_authority",
        "157_connector_operation_contexts",
        "158_connector_policy_deps",
        "159_connector_module_admission",
        "160_connector_cell_replay",
    }
    assert {
        revision.revision for revision in scripts.iterate_revisions("161_merge_connector_finops", "156_l5_finops_inquiry", implicit_base=True)
    } == {
        "161_merge_connector_finops",
        *connector_revisions,
    }
    assert {
        revision.revision for revision in scripts.iterate_revisions("161_merge_connector_finops", "160_connector_cell_replay", implicit_base=True)
    } == {
        "161_merge_connector_finops",
        "156_l5_finops_inquiry",
    }


@pytest.mark.parametrize(
    "previous_heads",
    [
        ("156_l5_finops_inquiry",),
        ("160_connector_cell_replay",),
        ("156_l5_finops_inquiry", "160_connector_cell_replay"),
        ("157_merge_transform_inquiry",),
        ("161_merge_connector_finops",),
        ("157_merge_transform_inquiry", "161_merge_connector_finops"),
        ("162_merge_connector_transform",),
        ("158_l5_cost_collections",),
        ("162_merge_connector_transform", "158_l5_cost_collections"),
    ],
)
def test_upgrade_joins_both_schemas_and_preserves_receipts(pg_url, previous_heads):
    for revision in previous_heads:
        upgrade(pg_url, revision)
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                PublicationRetryReceipt.__table__.insert().values(
                    tenant_id="tenant",
                    product_id="product",
                    project_id="project",
                    service_id="devops",
                    actor_id="actor",
                    idempotency_key="retained-retry-key",
                    publication_id="publication",
                    request_hash="a" * 64,
                    response={"publication_id": "publication", "state": "pending"},
                )
            )
            receipt_before = dict(connection.execute(select(PublicationRetryReceipt)).mappings().one())

        upgrade(pg_url)
        upgrade(pg_url)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [GATEWAY_HEAD]
            inspector = inspect(connection)
            assert {
                "connector_policy_realms",
                "connector_cells",
                "connector_collection_grants",
                "connector_operation_contexts",
                "connector_evidence_dependencies",
                "connector_module_admissions",
                "l5_service_cost_collections",
            } <= set(inspector.get_table_names())
            assert "question_hash" in {column["name"] for column in inspector.get_columns(WorkerAttempt.__tablename__)}
            assert "cleanup_receipt" in {column["name"] for column in inspector.get_columns("l5_service_binding_operations")}
            assert any(
                constraint["column_names"] == ["tenant_id", "consumer", "cell_id", "operation_id", "attempt_id"]
                for constraint in inspector.get_unique_constraints("connector_operation_contexts")
            )
            assert dict(connection.execute(select(PublicationRetryReceipt)).mappings().one()) == receipt_before
    finally:
        engine.dispose()
