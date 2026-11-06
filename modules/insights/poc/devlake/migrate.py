import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import admission
import cell_grants
import deploy
import migration_receipt
import publish


SCHEMA_SQL = Path(__file__).with_name("migrations") / "schema-v2.sql"


def apply_migration(config, sql_path=SCHEMA_SQL):
    manifest = migration_receipt.checked_manifest()
    if manifest["migration_sql_sha256"] != hashlib.sha256(sql_path.read_bytes()).hexdigest():
        raise ValueError("migration_required: checked schema and migration SQL are unavailable")

    target = dict(host=config["existing_db_endpoint"], database=config["database_name"],
                  cell=config["cell_id"], resource_id=config["existing_db_resource_id"],
                  username=config["migration_username"], ca_path=config["ca_path"],
                  region=config["aws_region"], account=config["aws_account_id"],
                  role=config["migration_role"], label=config["migration_label"])
    if (not migration_receipt.HOST.fullmatch(target["host"]) or
            not migration_receipt.RESOURCE_ID.fullmatch(target["resource_id"]) or
            not all(migration_receipt.IDENTIFIER.fullmatch(target[key]) for key in ("database", "cell")) or
            not migration_receipt.ROLE.fullmatch(target["username"]) or
            not migration_receipt.ROLE.fullmatch(target["role"]) or
            not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", target["region"]) or
            not re.fullmatch(r"[0-9]{12}", target["account"]) or
            not target["label"] or not Path(target["ca_path"]).is_file()):
        raise ValueError("registered migration target and verified CA are required")

    digest = hashlib.sha256(migration_receipt.SCHEMA.read_bytes()).hexdigest()
    grants = cell_grants.checked_roles(config, manifest, digest)

    admission.image_preflight(admission.load_receipt(config["api_receipt"]),
                              admission.load_receipt(config["worker_receipt"]))
    account = target["account"]
    role = target["role"]
    label = target["label"]
    identity = json.loads(publish.broker(label, "aws", "sts", "get-caller-identity", "--output", "json"))
    if (identity.get("Account") != account or
            not identity.get("Arn", "").startswith(f"arn:aws:sts::{account}:assumed-role/{role}/")):
        raise ValueError("selected migration role differs from approved account or role")
    token = publish.broker(label, "aws", "rds", "generate-db-auth-token", "--hostname", target["host"],
                           "--port", "5432", "--username", target["username"], "--region", target["region"])
    if not token:
        raise ValueError("fresh IAM migration token is unavailable")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    environment.update(PGPASSWORD=token, PGSSLMODE="verify-full", PGSSLROOTCERT=str(target["ca_path"]))
    command = ["psql", "-X", "-w", "-q", "--set=ON_ERROR_STOP=1", "--single-transaction",
               "--host", target["host"], "--port", "5432", "--username", target["username"],
               "--dbname", target["database"], "--set=cell_id=" + target["cell"],
               "--set=db_resource_id=" + target["resource_id"], "--set=manifest_sha256=" + digest,
               "--file", str(sql_path), "--file", "-"]
    try:
        subprocess.run(command, input=grants, env=environment, capture_output=True, text=True, check=True, timeout=120)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError("IAM/TLS migration transaction failed") from None
    if migration_receipt.readback(**target) != digest:
        raise ValueError("migration_required: database receipt differs after migration")
    return digest


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Apply pinned collection schema under migration-only IAM")
    parser.add_argument("--config", required=True, help="private selected operator configuration")
    args = parser.parse_args()
    try:
        config = deploy.selected_config(args.config)
        if config is None:
            raise ValueError("migration requires explicit collection selection")
        apply_migration(config)
    except (OSError, KeyError, TypeError, ValueError) as error:
        parser.exit(2, str(error) + "\n")


if __name__ == "__main__":
    main()
