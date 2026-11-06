"""Amendment publication upgrades preserve existing data and enforce retry identity."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.migrations.conftest_postgres import downgrade, upgrade


def test_publication_binding_upgrade_identity_and_downgrade(pg_url):
    upgrade(pg_url, "084_reservation_receipt_scopes")
    engine = create_engine(pg_url)
    table = "orchestration_amendment_issue_bindings"
    insert = text(
        "INSERT INTO orchestration_amendment_issue_bindings "
        "(id, org_id, repository, proposal_identity, state, claim_token, claimed_at, content_hash, created_at) "
        "VALUES (:id, :org, 'example-org/example-repo', 'cleanup-checker', 'pending', 'claim-1', NOW(), :hash, NOW())"
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO budget_settlement_receipts (org_id, request_id, user_id, cost_usd, total_tokens, allocation_key) "
                    "VALUES ('org', 'preserved', 'owner', 1.23, 10, 'allocation')"
                )
            )
        upgrade(pg_url, "085_amendment_issue_bindings")
        with engine.begin() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns(table)}
            assert columns == {
                "id",
                "org_id",
                "repository",
                "proposal_identity",
                "provider_repository_id",
                "state",
                "claim_token",
                "claimed_at",
                "content_hash",
                "issue_number",
                "issue_node_id",
                "issue_url",
                "created_at",
                "completed_at",
            }
            connection.execute(insert, {"id": "binding-1", "org": "org-alpha", "hash": "a" * 64})
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(insert, {"id": "binding-2", "org": "org-alpha", "hash": "b" * 64})
        with engine.begin() as connection:
            connection.execute(insert, {"id": "binding-3", "org": "org-beta", "hash": "a" * 64})
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 2
        downgrade(pg_url, "084_reservation_receipt_scopes")
        with engine.connect() as connection:
            assert table not in inspect(connection).get_table_names()
            assert str(connection.scalar(text("SELECT cost_usd FROM budget_settlement_receipts WHERE request_id = 'preserved'"))) == "1.230000"
        upgrade(pg_url, "085_amendment_issue_bindings")
        with engine.connect() as connection:
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    finally:
        engine.dispose()
