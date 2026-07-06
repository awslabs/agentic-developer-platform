"""DB verification for the agent-context verify workflows.

Runs inside an ingestion-image pod (piped via stdin by
.github/workflows/agent-context-verify-dispatch.yml); asserts the core
Knowledge Layer tables exist and prints run/stage summaries.
"""

import os
import sys

sys.path.insert(0, "/app")
os.environ.setdefault("DB_HOST", "bedrockgw-dev-postgres.civhekhiupfe.us-east-1.rds.amazonaws.com")
os.environ.setdefault("DB_NAME", "agent_context")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_USER", "agent_context_svc")
os.environ.setdefault("DB_USE_IAM_AUTH", "true")

try:
    import db

    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('index_run_stages')")
    print(f"index_run_stages exists: {cur.fetchone()[0]}")
    cur.execute("SELECT to_regclass('index_runs')")
    print(f"index_runs exists: {cur.fetchone()[0]}")
    cur.execute("SELECT stage, status, count(*) FROM index_run_stages GROUP BY 1, 2 ORDER BY 1, 2")
    rows = cur.fetchall()
    print(f"\n=== Stage status summary ({len(rows)} rows) ===")
    for row in rows:
        print(f"  {row[0]:20s} | {row[1]:12s} | {row[2]}")
    cur.execute("SELECT count(*) FROM dependencies")
    dep_count = cur.fetchone()[0]
    print(f"\nDependency rows: {dep_count}")
    cur.execute("SELECT count(*) FROM index_runs")
    run_count = cur.fetchone()[0]
    print(f"Index runs: {run_count}")
    cur.execute("SELECT id, repo, status FROM index_runs ORDER BY started_at DESC LIMIT 5")
    print("\n=== Recent index runs ===")
    for row in cur.fetchall():
        print(f"  {row[0][:8]}... | {row[1]:40s} | {row[2]}")
    conn.close()
except Exception as e:
    print(f"DB verification failed: {e}")
    sys.exit(1)
