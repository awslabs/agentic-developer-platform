"""Prepare owned database credentials and authenticate each role before success."""

import json
import secrets
import sys
from urllib.parse import quote, unquote, urlsplit

import httpx

from .cluster_probe import ClusterProbe
from .config import prepare_database_sql, require


def owned_secret(installer, kind, make_value):
    name = installer.env["secrets"][kind]
    result = installer.aws(
        "secretsmanager", "describe-secret", "--secret-id", name, allow_failure=True
    )
    if result.returncode:
        require(
            "ResourceNotFoundException" in result.stderr,
            "Secret ownership lookup failed",
        )
        value = make_value()
        payload = {
            "Name": name,
            "SecretString": json.dumps(value),
            "Tags": [{"Key": "SuperplaneInstallation", "Value": installer.owner}],
        }
        created = installer.json(
            installer.commands.call(
                [sys.executable, "-m", "installation.database_preparation"],
                data=json.dumps(
                    {"region": installer.env["region"], "payload": payload}
                ),
            )
        )
        version = created["VersionId"]
    else:
        described = installer.json(result)
        require(
            any(
                tag == {"Key": "SuperplaneInstallation", "Value": installer.owner}
                for tag in described.get("Tags", [])
            ),
            "Existing credential secret is not owned by this installation",
        )
        stored = installer.json(
            installer.aws("secretsmanager", "get-secret-value", "--secret-id", name)
        )
        value, version = json.loads(stored["SecretString"]), stored["VersionId"]
    return value, version


def prepare(installer, admin_url):
    require(
        installer.env.get("image_execution") == "cluster",
        "Credential preparation requires image_execution: cluster for the selected private database",
    )
    require(
        bool(admin_url),
        "Set SUPERPLANE_DATABASE_ADMIN_URL for this one-shot preparation process",
    )
    installer.target()
    installer.images()
    with installer.exclusive():
        return _prepare_owned(installer, admin_url)


def observation_secret_value(installer):
    env = installer.env
    result = {
        key: secrets.token_urlsafe(40)
        for key in (
            "monitor-signing-key",
            "controller-signing-key",
            "skypilot-token",
            "jwt-signing-key",
        )
    }
    result.update(
        {
            key: "Bearer " + secrets.token_urlsafe(40)
            for key in ("monitor-credential", "controller-credential")
        }
    )
    grants = []
    for component in ("monitor", "controller"):
        scopes = (
            ["budget_monitor/global"]
            if component == "monitor"
            else (
                [f"controller_management/{env['org_id']}"]
                if installer.control_plane_only
                else []
            )
        )
        grants.append(
            {
                "submitter_id": installer.owner + "-" + component,
                "credential": result[component + "-credential"],
                "signing_key": result[component + "-signing-key"],
                "workspaces": []
                if installer.control_plane_only
                else [env["workspace_id"]],
                "lease_scopes": scopes,
            }
        )
    result["submitters"] = json.dumps(grants)
    return result


def _prepare_owned(installer, admin_url):
    env = installer.env
    db = env["database"]
    instance = installer.json(
        installer.aws(
            "rds", "describe-db-instances", "--db-instance-identifier", db["identifier"]
        )
    )["DBInstances"][0]
    backup = installer.json(
        installer.aws(
            "rds", "describe-db-snapshots", "--db-snapshot-identifier", db["backup_id"]
        )
    )["DBSnapshots"][0]
    require(
        backup["Status"] == "available"
        and backup["DBInstanceIdentifier"] == db["identifier"]
        and instance.get("DbiResourceId")
        and backup.get("DbiResourceId") == instance.get("DbiResourceId"),
        "Available backup does not belong to the selected database resource",
    )
    endpoint = instance["Endpoint"]
    parsed_admin = urlsplit(admin_url)
    require(
        parsed_admin.hostname == endpoint["Address"]
        and (parsed_admin.port or 5432) == endpoint["Port"]
        and parsed_admin.path == "/" + db["database"]
        and not parsed_admin.query
        and not parsed_admin.fragment,
        "Preparation administrator targets a different database",
    )
    ca_response = httpx.get(
        f"https://truststore.pki.rds.amazonaws.com/{env['region']}/{env['region']}-bundle.pem",
        timeout=30,
        follow_redirects=False,
    )
    require(
        ca_response.status_code == 200 and "BEGIN CERTIFICATE" in ca_response.text,
        "RDS CA bundle unavailable",
    )

    def database_value():
        result = {"ca-pem": ca_response.text}
        for kind in ("runtime", "migration", "skypilot"):
            role = f"superplane_{env['environment']}_{kind}"
            password = secrets.token_urlsafe(40)
            result[kind + "-url"] = (
                f"postgresql://{quote(role, safe='')}:{quote(password, safe='')}@{endpoint['Address']}:{endpoint['Port']}/{quote(db['database'], safe='')}"
            )
        return result

    database, db_version = owned_secret(installer, "database", database_value)
    installer.receipt["preparation"] = {
        "database_secret_version": db_version,
        "database": db["database"],
        "backup_id": db["backup_id"],
        "roles_authenticated": False,
    }
    installer.save()
    passwords = {}
    for kind in ("runtime", "migration", "skypilot"):
        parsed = urlsplit(database[kind + "-url"])
        role = f"superplane_{env['environment']}_{kind}"
        require(
            parsed.hostname == endpoint["Address"]
            and (parsed.port or 5432) == endpoint["Port"]
            and unquote(parsed.path) == "/" + db["database"]
            and unquote(parsed.username or "") == role
            and bool(parsed.password)
            and not parsed.query
            and not parsed.fragment,
            "Existing domain credential differs from the selected database/role",
        )
        passwords[role] = unquote(parsed.password)
    target = {
        "host": endpoint["Address"],
        "port": endpoint["Port"],
        "database": db["database"],
        "environment": env["environment"],
    }
    values = {
        "SUPERPLANE_PREPARATION_ADMIN_URL": admin_url,
        "SUPERPLANE_PREPARATION_TARGET": json.dumps(target),
        "SUPERPLANE_PREPARATION_PASSWORDS": json.dumps(passwords),
        "SUPERPLANE_PREPARATION_SQL": prepare_database_sql(env),
        "SUPERPLANE_DATABASE_CA": ca_response.text,
    }
    addresses = installer.resolve_database_addresses(endpoint)
    with ClusterProbe(installer) as probe:
        probe.isolate(
            database_cidrs=[
                address + ("/128" if ":" in address else "/32") for address in addresses
            ],
            database_port=endpoint["Port"],
        )
        result = probe.run(
            "superplane-api",
            ["python", "-m", "app.database_preparation"],
            values=values,
        )
        require(
            result.returncode == 0
            and installer.json(result).get("status") == "prepared",
            "Owned database preparation did not complete",
        )

    _, observation_version = owned_secret(
        installer, "observation", lambda: observation_secret_value(installer)
    )
    installer.receipt["secret_versions"] = {
        "database": db_version,
        "observation": observation_version,
    }
    installer.secrets()
    installer.database()  # Real password/TLS connections as all three roles.
    installer.receipt["preparation"]["roles_authenticated"] = True
    installer.receipt["status"] = "database-prepared-and-authenticated"
    installer.save()


def create_secret_from_stdin():
    """SDK transport: JSON stays in a pipe; neither argv nor files hold secrets."""
    import boto3

    request = json.load(sys.stdin)
    response = boto3.client(
        "secretsmanager", region_name=request["region"]
    ).create_secret(**request["payload"])
    print(json.dumps({key: response[key] for key in ("ARN", "Name", "VersionId")}))


if __name__ == "__main__":
    try:
        create_secret_from_stdin()
    except Exception:
        print(
            json.dumps(
                {
                    "status": "refused",
                    "reason": "Secret creation failed; credentials and SDK errors are redacted",
                }
            )
        )
        raise SystemExit(2) from None
