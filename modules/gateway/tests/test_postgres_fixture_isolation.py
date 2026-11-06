"""Re-exported PostgreSQL fixtures share servers, never test databases."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize("migrated", [False, True])
def test_reexported_fixtures_share_one_server_per_worker_and_clean_up(tmp_path, workers, migrated):
    pytest.importorskip("pgserver")
    psycopg2 = pytest.importorskip("psycopg2")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    if migrated:
        (tmp_path / "conftest.py").write_text(
            "import pathlib, os, psycopg2\n"
            "from tests.migrations import conftest_postgres as fixtures\n"
            "def upgrade(url):\n"
            "    with psycopg2.connect(url) as connection, connection.cursor() as cursor:\n"
            "        cursor.execute('CREATE TABLE migrated_seed (value integer)')\n"
            "    with pathlib.Path(f'migrations-{os.getpid()}.txt').open('a') as log:\n"
            "        log.write('migrated\\n')\n"
            "fixtures.upgrade = upgrade\n"
        )
    for index in range(4):
        (tmp_path / f"test_alias_{index}.py").write_text(
            "import json, os, pathlib, psycopg2\n"
            f"from tests.migrations.conftest_postgres import pg_server, {'pg_head_url as pg_url' if migrated else 'pg_url'}\n"
            "def test_isolated_database(pg_server, pg_url):\n"
            "    with psycopg2.connect(pg_url) as connection:\n"
            "        with connection.cursor() as cursor:\n"
            "            cursor.execute(\"SELECT to_regclass('leaked_data')\")\n"
            "            assert cursor.fetchone()[0] is None\n"
            "            cursor.execute('CREATE TABLE leaked_data (value integer)')\n"
            "            cursor.execute('INSERT INTO leaked_data VALUES (42)')\n"
            f"    pathlib.Path('result-{index}.json').write_text(json.dumps(\n"
            "        dict(worker=os.getpid(), server=pg_server.get_uri(), database=pg_url)))\n"
        )
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    env.pop("BG_TEST_POSTGRES_URI", None)
    args = [sys.executable, "-m", "pytest", "-q"]
    if workers:
        args += ["-p", "xdist.plugin", "-n", str(workers), "--dist", "loadfile"]
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads(path.read_text()) for path in sorted(tmp_path.glob("result-*.json"))]
    assert len(records) == 4
    assert len({record["database"] for record in records}) == 4
    assert len({record["worker"] for record in records}) == (workers or 1)
    if migrated:
        migrations = list(tmp_path.glob("migrations-*.txt"))
        assert len(migrations) == (workers or 1)
        assert all(path.read_text() == "migrated\n" for path in migrations)
    for worker in {record["worker"] for record in records}:
        servers = {record["server"] for record in records if record["worker"] == worker}
        assert len(servers) == 1
        # The owning pytest session has ended, so its embedded server must stop.
        with pytest.raises(psycopg2.OperationalError):
            psycopg2.connect(servers.pop(), connect_timeout=1)
