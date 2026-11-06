"""Exercise Product provenance against the real registry SQL, including removal."""

import tempfile

import pytest

from door.acl import CallerPrincipal, PostgresACLStore, SearchHit, filter_results

PRODUCT = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
ATTEMPT = "00000000-0000-0000-0000-000000000003"


def test_product_acl_rechecks_registry_and_does_not_use_repo_name(monkeypatch):
    pgserver = pytest.importorskip("pgserver")
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.pool import SimpleConnectionPool

    server = pgserver.get_server(
        tempfile.mkdtemp(prefix="product-knowledge-acl-"), cleanup_mode="delete"
    )
    pool = SimpleConnectionPool(1, 2, server.get_uri())
    try:
        with psycopg2.connect(server.get_uri()) as conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE knowledge_assets(id uuid PRIMARY KEY, tenant_id text, product_id text, status text, owner_sub text, ingestion_attempt_id uuid)"
            )
            cur.executemany(
                "INSERT INTO knowledge_assets(id,tenant_id,product_id,status,ingestion_attempt_id) VALUES (%s,%s,%s,%s,%s)",
                [
                    (PRODUCT, "tenant", PRODUCT, "complete", ATTEMPT),
                    (OTHER, "tenant", OTHER, "complete", ATTEMPT),
                ],
            )
        caller = CallerPrincipal(
            owner_sub="member-without-github",
            tenant_id="tenant",
            product_id=PRODUCT,
            run_bound=True,
        )
        store = PostgresACLStore(pool)

        def hit(asset_id):
            return SearchHit(
                "org/same-source",
                {
                    "asset_id": asset_id,
                    "tenant_id": "tenant",
                    "product_id": PRODUCT,
                    "visibility": "product",
                    "ingestion_attempt_id": ATTEMPT,
                },
            )

        own, other = hit(PRODUCT), hit(OTHER)
        assert store.get_active_ingestion_attempts(caller) == {ATTEMPT}
        with monkeypatch.context() as bounded:
            bounded.setattr("door.acl.MAX_ACTIVE_INGESTION_ATTEMPTS", 0)
            with pytest.raises(ValueError, match="active ingestion attempts"):
                store.get_active_ingestion_attempts(caller)
        assert filter_results([own, other], caller, store) == [own]
        with psycopg2.connect(server.get_uri()) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE knowledge_assets SET ingestion_attempt_id=%s WHERE id=%s", (OTHER, PRODUCT)
            )
        assert filter_results([own], caller, store) == []  # Old cache / late worker.
        assert store.get_active_ingestion_attempts(caller) == {OTHER}
        own.data["ingestion_attempt_id"] = OTHER
        assert filter_results([own], caller, store) == [own]
        for status in ("removed", "failed", "indexing", "registered"):
            with psycopg2.connect(server.get_uri()) as conn, conn.cursor() as cur:
                cur.execute("UPDATE knowledge_assets SET status=%s WHERE id=%s", (status, PRODUCT))
            assert filter_results([own, other], caller, store) == []
        with psycopg2.connect(server.get_uri()) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE knowledge_assets SET status='complete', tenant_id='other' WHERE id=%s",
                (PRODUCT,),
            )
        assert filter_results([own], caller, store) == []
        assert store.get_allowed_repos(caller) == set()  # There is no repo catalog in this fixture.

        personal_id = "00000000-0000-0000-0000-000000000004"
        foreign_id = "00000000-0000-0000-0000-000000000005"
        with psycopg2.connect(server.get_uri()) as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO knowledge_assets VALUES (%s,%s,NULL,'complete',%s,%s)",
                [
                    (personal_id, "tenant", "member-without-github", ATTEMPT),
                    (foreign_id, "tenant", "other-owner", ATTEMPT),
                ],
            )
        personal_caller = CallerPrincipal(
            owner_sub="member-without-github", tenant_id="tenant", run_bound=True
        )
        assert store.get_active_ingestion_attempts(personal_caller) == {ATTEMPT}
        personal = SearchHit(
            "org/same-source",
            {
                "visibility": "personal",
                "asset_id": personal_id,
                "owner_sub": "member-without-github",
                "tenant_id": "tenant",
                "ingestion_attempt_id": ATTEMPT,
            },
        )
        foreign = SearchHit("org/same-source", {**personal.data, "asset_id": foreign_id})
        disguised_product = SearchHit("org/same-source", {**personal.data, "asset_id": PRODUCT})
        assert filter_results([personal, foreign, disguised_product], personal_caller, store) == [
            personal
        ]
        personal.data["tenant_id"] = ""
        assert filter_results([personal], personal_caller, store) == []
        personal.data["tenant_id"] = "tenant"
        with psycopg2.connect(server.get_uri()) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE knowledge_assets SET ingestion_attempt_id=%s WHERE id=%s",
                (OTHER, personal_id),
            )
        assert filter_results([personal], personal_caller, store) == []
        personal.data["ingestion_attempt_id"] = OTHER
        assert filter_results([personal], personal_caller, store) == [personal]
        with psycopg2.connect(server.get_uri()) as conn, conn.cursor() as cur:
            cur.execute("UPDATE knowledge_assets SET status='removed' WHERE id=%s", (personal_id,))
        assert filter_results([personal], personal_caller, store) == []
        assert store.get_active_ingestion_attempts(personal_caller) == set()
        # Readers can roll out before the additive Product registry migration.
        with psycopg2.connect(server.get_uri()) as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE knowledge_assets DROP COLUMN product_id")
        assert store.get_allowed_product_assets(caller, {PRODUCT}) == {}
    finally:
        pool.closeall()
        server.cleanup()
