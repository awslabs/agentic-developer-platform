"""Code rollback and re-upgrade never remove or duplicate incident credits."""

from tests.migrations.conftest_postgres import downgrade, upgrade


def test_audit_survives_code_rollback_and_reupgrade(pg_url, connect):
    upgrade(pg_url, "head")
    connection = connect()
    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO budget_pricing_corrections
                (org_id, request_id, correction_id, credit_usd, original_decision,
                 corrected_decision, allocation_key, source_key, actor, created_at)
            VALUES ('tenant','request','test-credit',0.5,'{}','{}','hash','receipt','operator',NOW())
        """)
    downgrade(pg_url, "078_gpt6_sol_luna_pricing")
    upgrade(pg_url, "079_budget_pricing_corrections")
    with connection.cursor() as cursor:
        cursor.execute("SELECT org_id,request_id,credit_usd,actor FROM budget_pricing_corrections")
        from decimal import Decimal

        assert cursor.fetchall() == [("tenant", "request", Decimal("0.500000"), "operator")]
