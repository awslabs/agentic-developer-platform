"""Migration preserves old accounting without inventing reservation authority."""

from tests.migrations.conftest_postgres import downgrade, upgrade


def test_legacy_receipts_remain_untrusted(pg_url, connect):
    upgrade(pg_url, "083_probe_contract_revision")
    connection = connect()
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO budget_settlement_receipts (org_id, request_id, user_id, cost_usd, total_tokens, allocation_key) "
            "VALUES ('org', 'old', 'owner', 1.23, 10, 'allocation')"
        )
    connection.commit()
    upgrade(pg_url, "084_reservation_receipt_scopes")
    with connection.cursor() as cursor:
        cursor.execute("SELECT cost_usd, reservation_scope_keys IS NULL FROM budget_settlement_receipts WHERE request_id = 'old'")
        cost, untrusted = cursor.fetchone()
        assert str(cost) == "1.230000" and untrusted
        cursor.execute("UPDATE budget_settlement_receipts SET reservation_scope_keys = '[\"exact-key\"]'::json WHERE request_id = 'old'")
    connection.commit()
    downgrade(pg_url, "083_probe_contract_revision")
    with connection.cursor() as cursor:
        cursor.execute("SELECT reservation_scope_keys FROM budget_settlement_receipts WHERE request_id = 'old'")
        assert cursor.fetchone() == (["exact-key"],)
    upgrade(pg_url, "084_reservation_receipt_scopes")
    with connection.cursor() as cursor:
        cursor.execute("SELECT reservation_scope_keys FROM budget_settlement_receipts WHERE request_id = 'old'")
        assert cursor.fetchone() == (["exact-key"],)
