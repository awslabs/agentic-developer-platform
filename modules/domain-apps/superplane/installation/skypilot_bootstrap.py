"""Start the pinned SkyPilot server with PostgreSQL-backed configuration and state.

Mounted from the installation-owned ConfigMap, alongside desired-config.yaml.
The initial global config is empty: SkyPilot 0.12 loads server config from its
database when a PostgreSQL URI is configured, and rejects a mixed file config.
"""

import json
import os
from pathlib import Path
import sys


def provider_identity():
    """Attest the backend's own constrained credential source, never the proxy's."""
    path = Path("/provider-identity/identity.json")
    path.unlink(missing_ok=True)
    forbidden = {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    }
    if any(os.environ.get(key) for key in forbidden):
        raise RuntimeError("alternate provider credential sources refused")
    if any(
        os.environ.get(key) != "/dev/null"
        for key in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE")
    ):
        raise RuntimeError("provider credential files must be disabled")
    from botocore.client import BaseClient

    if not getattr(BaseClient._make_api_call, "_superplane_guard", False):
        raise RuntimeError("provider allocation guard unavailable")
    role = os.environ.get("AWS_ROLE_ARN")
    if not role:
        # A zero-workspace installation can serve management without cloud auth.
        value = {"version": 1, "configured": False}
    else:
        import boto3

        identity = boto3.client("sts").get_caller_identity()
        expected = (
            f"arn:aws:sts::{role.split(':')[4]}:assumed-role/{role.rsplit('/', 1)[-1]}/"
        )
        if identity["Account"] != role.split(":")[4] or not identity["Arn"].startswith(
            expected
        ):
            raise RuntimeError("backend provider role mismatch")
        value = {
            "version": 1,
            "configured": True,
            "provider": "aws",
            "account_id": identity["Account"],
            "principal_arn": identity["Arn"],
            "role_arn": role,
            "credential_source": "web_identity",
            "allocation_tags": ["instance", "volume", "network-interface"],
        }
        if getattr(BaseClient._make_api_call, "_superplane_gpu_limit", False):
            value["capacity_constraints"] = ["physical_gpu_limit"]
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o644)
    temporary.replace(path)


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
    provider_identity()
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
