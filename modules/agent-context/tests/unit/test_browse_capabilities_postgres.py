"""Reusing a verified index must preserve its advertised capabilities."""

from datetime import datetime, timedelta, timezone

from psycopg2.extras import Json
from psycopg2.pool import SimpleConnectionPool
import pytest

from door.browse_backend import browse


@pytest.fixture
def catalog(tmp_path):
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(str(tmp_path / "postgres"))
    pool = SimpleConnectionPool(1, 2, server.get_uri())
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE repositories (repo_name text, git_url text, indexed_at timestamptz)"
            )
            cur.execute("""CREATE TABLE index_run_stages (
                repo text, stage text, status text, metrics jsonb, error text,
                completed_at timestamptz
            )""")
            cur.execute("INSERT INTO repositories VALUES ('example/service', '', NULL)")
            before = datetime.now(timezone.utc) - timedelta(minutes=1)
            for stage in ["zoekt_index", "deepwiki"]:
                cur.execute(
                    "INSERT INTO index_run_stages VALUES (%s, %s, %s, %s, %s, %s)",
                    ("example/service", stage, "verified", Json({"files": 23}), None, before),
                )
        conn.commit()
        pool.putconn(conn)
        yield pool
    finally:
        pool.closeall()
        server.cleanup()


@pytest.mark.parametrize("action,uri", [("ls", "/"), ("info", "/example/service")])
@pytest.mark.parametrize(
    "reason,ready",
    [
        ("already verified at current SHA", True),
        ("disabled", False),
        (None, False),
    ],
)
async def test_reindex_reuse_preserves_evidence_but_disabled_stages_do_not(
    catalog, action, uri, reason, ready
):
    conn = catalog.getconn()
    with conn.cursor() as cur:
        for stage in ["zoekt_index", "deepwiki", "scip_structural"]:
            cur.execute(
                "INSERT INTO index_run_stages VALUES (%s, %s, %s, NULL, %s, NOW())",
                ("example/service", stage, "skipped", reason),
            )
    conn.commit()
    catalog.putconn(conn)

    results = await browse(action, uri, db_pool=catalog)
    assert len(results) == 1
    caps = results[0].data["capabilities"]
    for name in ["code_search", "wiki"]:
        assert caps[name]["ready"] is ready
        if ready:
            assert caps[name]["files"] == 23
    # A skip alone can never manufacture evidence that a capability exists.
    assert caps.get("call_graph", {}).get("ready", False) is False
