"""Administrator replay snapshots are additive and survive protected downgrade refusal."""

import psycopg2

from tests.migrations.conftest_postgres import upgrade
from tests.migrations.head_contract import GATEWAY_HEAD

pytest_plugins = ("tests.migrations.conftest_postgres",)


def test_current_head_retains_authoritative_reconciliation_response(pg_url):
    upgrade(pg_url, "115_legacy_metering_cohort")
    upgrade(pg_url)
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(GATEWAY_HEAD,)]
        cursor.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name='metering_reconciliation_events' AND column_name='result_snapshot'"
        )
        assert cursor.fetchall() == [("json", "YES")]
