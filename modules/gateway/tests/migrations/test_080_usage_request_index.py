"""The online index supports scoped lookups and survives re-upgrade."""

from tests.migrations.conftest_postgres import downgrade, upgrade


def test_request_index_valid_and_reversible(pg_url, connect):
    upgrade(pg_url, "080_usage_request_index")
    connection = connect()
    with connection.cursor() as cursor:
        cursor.execute("SELECT indisvalid, pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid=to_regclass('ix_usage_org_request')")
        valid, definition = cursor.fetchone()
        assert valid
        assert "(org_id, request_id)" in definition
        assert "request_id IS NOT NULL" in definition
    downgrade(pg_url, "079_budget_pricing_corrections")
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('ix_usage_org_request')")
        assert cursor.fetchone() == (None,)
    upgrade(pg_url, "080_usage_request_index")
    with connection.cursor() as cursor:
        cursor.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass('ix_usage_org_request')")
        assert cursor.fetchone() == (True,)
