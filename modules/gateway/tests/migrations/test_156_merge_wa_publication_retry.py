"""Both retained Profile claims and publication retries survive the merged head."""

import pytest
from l5_services.models import ProfileClaim, PublicationRetryReceipt, WorkerAttempt
from sqlalchemy import create_engine, inspect, select, text

from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_profile_claims_and_retry_receipts_join_without_rewriting_history():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("157_merge_wa_finops_inquiry").down_revision == (
        "156_merge_wa_publication_retry",
        "156_l5_finops_inquiry",
    )
    assert scripts.get_revision("156_merge_wa_publication_retry").down_revision == (
        "155_l5_wa_profile_claims",
        "155_l5_publication_retry",
    )
    for parent in ("155_l5_wa_profile_claims", "155_l5_publication_retry"):
        assert scripts.get_revision(parent).down_revision == "154_l5_finops_ownership"


@pytest.mark.parametrize("previous_head", ["155_l5_wa_profile_claims", "155_l5_publication_retry", "156_merge_wa_publication_retry"])
def test_upgrade_from_either_parent_preserves_claims_and_retry_history(pg_url, previous_head):
    upgrade(pg_url, previous_head)
    engine = create_engine(pg_url)
    scope = {"tenant_id": "tenant", "product_id": "product", "project_id": "project"}
    if previous_head in {"155_l5_wa_profile_claims", "156_merge_wa_publication_retry"}:
        model = ProfileClaim
        values = {**scope, "service_id": "well_architected", "native_identity_key": "a" * 64}
        missing_model = PublicationRetryReceipt
    else:
        model = PublicationRetryReceipt
        values = {
            **scope,
            "service_id": "devops",
            "actor_id": "actor",
            "idempotency_key": "retry-key-0000001",
            "publication_id": "publication",
            "request_hash": "b" * 64,
            "response": {"publication_id": "publication", "state": "pending"},
        }
        missing_model = ProfileClaim
    try:
        with engine.begin() as connection:
            assert inspect(connection).has_table(missing_model.__tablename__) is (previous_head == "156_merge_wa_publication_retry")
            connection.execute(model.__table__.insert().values(**values))
            before = connection.execute(select(model)).mappings().one()

        upgrade(pg_url)
        upgrade(pg_url)
        with engine.connect() as connection:
            inspector = inspect(connection)
            for expected_model in (ProfileClaim, PublicationRetryReceipt, WorkerAttempt):
                assert {column["name"] for column in inspector.get_columns(expected_model.__tablename__)} == set(
                    expected_model.__table__.columns.keys()
                )
            assert connection.execute(select(model)).mappings().one() == before
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD

        refused = run_alembic(pg_url, "downgrade", "154_l5_finops_ownership")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert connection.execute(select(model)).mappings().one() == before
    finally:
        engine.dispose()
