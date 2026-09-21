"""Start the pinned SkyPilot server with PostgreSQL-backed configuration and state.

Mounted from the installation-owned ConfigMap, alongside desired-config.yaml.
The initial global config is empty: SkyPilot 0.12 loads server config from its
database when a PostgreSQL URI is configured, and rejects a mixed file config.
"""

import json
import os
from pathlib import Path
import sys


def main():
    if (
        os.environ.get("IS_SKYPILOT_SERVER") != "true"
        or not os.environ.get("SKYPILOT_DB_CONNECTION_URI")
        or os.environ.get("PGSSLMODE") != "verify-full"
    ):
        raise RuntimeError("Verified PostgreSQL server configuration is required")

    from sky import skypilot_config
    from sky.utils.db import db_utils
    import sqlalchemy

    desired = skypilot_config.parse_and_validate_config_file(
        str(Path(__file__).with_name("desired-config.yaml"))
    )
    engine = db_utils.get_engine(None)
    if engine.dialect.name != "postgresql":
        raise RuntimeError("SkyPilot must not fall back to local SQLite")

    if sys.argv[1:] == ["--check-database"]:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text("SET TRANSACTION READ ONLY"))
            database, schema = connection.execute(
                sqlalchemy.text("SELECT current_database(), current_schema()")
            ).one()
            tls = connection.execute(
                sqlalchemy.text(
                    "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
                )
            ).scalar_one()
            tables = sqlalchemy.inspect(connection).get_table_names(schema=schema)
        print(
            json.dumps(
                {
                    "dialect": engine.dialect.name,
                    "database": database,
                    "schema": schema,
                    "tls": tls,
                    "tables": tables,
                    "config_matches": dict(skypilot_config.to_dict()) == dict(desired),
                }
            )
        )
        return

    # One Recreate replica starts this before the API process, so no concurrent
    # server config writer is running. The pinned API persists this in PostgreSQL.
    skypilot_config.update_api_server_config_no_lock(desired)
    engine.dispose()
    os.execv(sys.executable, [sys.executable, "-m", "sky.server.server", *sys.argv[1:]])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # SQLAlchemy exceptions can contain connection details; never emit them.
        print(
            json.dumps(
                {
                    "error": "SkyPilot database startup/check failed",
                    "type": type(exc).__name__,
                }
            )
        )
        raise SystemExit(2) from None
