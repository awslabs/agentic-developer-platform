"""Protected owner enrollment/revocation, after reviewed Terraform composition.

No human bearer token, UserCredential insert, implicit restoration or runtime
readiness claim. Run with the selected installation operator and kube context.
Input contains references/selectors, never SecretString or session credentials.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess

import boto3


def contract():
    path = (
        Path(__file__).resolve().parents[4]
        / "modules/gateway/src/shared/domain_provider_contract.py"
    )
    spec = importlib.util.spec_from_file_location("provider_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition):
    if not condition:
        raise ValueError("protected provider enrollment refused")


def installed_config(namespace, cluster):
    require(
        isinstance(namespace, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", namespace)
    )
    context = subprocess.run(
        ["kubectl", "config", "view", "--minify", "--raw", "-o", "jsonpath={.clusters}"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    current = json.loads(context.stdout)
    require(
        len(current) == 1
        and current[0]["cluster"]["server"] == cluster["endpoint"]
        and current[0]["cluster"].get("certificate-authority-data")
        == cluster["certificateAuthority"]["data"]
        and not current[0]["cluster"].get("insecure-skip-tls-verify")
    )
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "configmap",
            "adp-worker-authority-config",
            "-n",
            namespace,
            "-o",
            "json",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    value = json.loads(result.stdout)
    require(value["metadata"]["namespace"] == namespace)
    return value["data"]


def enroll(
    document, session, *, config_reader=installed_config, check_only=True, revoke=False
):
    require(
        isinstance(document, dict)
        and set(document)
        == {
            "version",
            "environment",
            "operator_role_arn",
            "operator_role_id",
            "gateway_namespace",
            "registry_table",
            "authority",
            "management_cluster",
        }
    )
    require(type(document["version"]) is int and document["version"] == 1)
    environment = document["environment"]
    require(re.fullmatch(r"[a-z][a-z0-9-]{0,19}", environment))
    schema = contract()
    value = schema.validate(document["authority"])
    require(value["status"] == "active" and value["generation"] == 1)
    account, region = value["account_id"], value["region"]
    caller = session.client("sts", region_name=region).get_caller_identity()
    operator = document["operator_role_arn"]
    require(
        caller["Account"] == account
        and re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_-]+", operator)
    )
    require(
        caller["Arn"].startswith(
            f"arn:aws:sts::{account}:assumed-role/{operator.rsplit('/', 1)[1]}/"
        )
    )
    require(caller["UserId"].split(":", 1)[0] == document["operator_role_id"])
    iam = session.client("iam", region_name=region)
    owner = iam.get_role(RoleName=operator.rsplit("/", 1)[1])["Role"]
    require(
        owner["Arn"] == operator and owner["RoleId"] == document["operator_role_id"]
    )
    ddb = session.client("dynamodb", region_name=region)
    table = f"adp-{environment}-superplane-provider-authorities"
    key = {"record_id": {"S": value["credential_id"]}}
    expected = {
        **key,
        "document": {"S": schema.canonical(value)},
        "revision": {"S": schema.digest(value)},
    }
    old = ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
    revoked = {**value, "status": "revoked", "generation": 2}
    revoked_item = {
        **key,
        "document": {"S": schema.canonical(revoked)},
        "revision": {"S": schema.digest(revoked)},
    }
    if revoke:
        # Revocation still works if the provider/installation is broken or removed.
        # It can only withdraw the exact original owner-enrolled generation.
        require(old in (expected, revoked_item))
        if old == expected and not check_only:
            ddb.put_item(
                TableName=table,
                Item=revoked_item,
                ConditionExpression="revision = :expected",
                ExpressionAttributeValues={":expected": expected["revision"]},
            )
            require(
                ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
                == revoked_item
            )
        return {
            "state": "revoke-reviewed" if check_only else "revoked",
            "credential_id": value["credential_id"],
        }
    require(old is None or old == expected)
    require(
        schema.policy_identity(iam, value["role_arn"], value["managed_policy_arns"])
        == (value["role_id"], value["policy_sha256"])
    )
    require(
        schema.boundary_identity(iam, value["child_boundary_arn"])
        == value["child_boundary_sha256"]
    )
    versions = session.client("secretsmanager", region_name=region).describe_secret(
        SecretId=value["secret_arn"]
    )["VersionIdsToStages"]
    require(
        [version for version, stages in versions.items() if "AWSCURRENT" in stages]
        == [value["secret_version"]]
    )
    response = session.client("s3", region_name=region).get_object(
        Bucket=f"adp-terraform-state-{account}",
        Key=f"domain-routes/{environment}/superplane/public-route.json",
    )
    body = response["Body"]
    try:
        raw = body.read(4097)
    finally:
        body.close()
    require(len(raw) <= 4096)
    route = json.loads(raw)
    require(
        route.get("version") == 2
        and route.get("enabled") is True
        and route.get("installation_id") == value["installation_id"]
    )
    cluster = session.client("eks", region_name=region).describe_cluster(
        name=document["management_cluster"]
    )["cluster"]
    require(
        cluster["arn"]
        == f"arn:aws:eks:{region}:{account}:cluster/{document['management_cluster']}"
        and cluster["status"] == "ACTIVE"
    )
    configured = config_reader(document["gateway_namespace"], cluster)
    require(configured.get("ADP_DOMAIN_PROVIDER_ACCOUNT_ID") == account)
    require(configured.get("ADP_DOMAIN_PROVIDER_AUTHORITY_TABLE") == table)
    require(
        configured.get("ADP_DOMAIN_PROVIDER_EVIDENCE_TABLE")
        == f"adp-{environment}-superplane-provider-evidence"
    )
    bindings = json.loads(configured["ADP_DOMAIN_OPERATION_BINDINGS"])
    matches = [
        item
        for item in bindings
        if item.get("domain") == "superplane" and item.get("org_id") == value["org_id"]
    ]
    require(len(matches) == 1)
    binding = {
        "worker_scaled_job": "superplane-paid-worker",
        "current_identity_enforced": False,
        "domain_database_secret_id": "",
        "domain_database_schema": "",
        **matches[0],
    }
    require(
        schema.digest(binding) == value["binding_sha256"]
        and binding["adp_org_id"] == value["adp_org_id"]
        and binding["worker_namespace"] == route["namespace"]
    )
    require(re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", document["registry_table"]))
    registry = session.client("ssm", region_name=region).get_parameter(
        Name=f"/adp/{environment}/gateway/agent-registry-table"
    )["Parameter"]["Value"]
    require(registry == document["registry_table"])
    for kind, suffix, scopes in (
        ("producer", "api-producer", ["domain:operation-producer"]),
        (
            "worker",
            "domain-worker",
            ["domain:operation-executor", "domain:operation-recovery"],
        ),
    ):
        entry = ddb.get_item(
            TableName=document["registry_table"],
            Key={"agent_id": {"S": binding[f"{kind}_registry_id"]}},
            ConsistentRead=True,
        ).get("Item", {})
        role_arn = f"arn:aws:iam::{account}:role/adp-{environment}-superplane-{suffix}"
        require(
            entry.get("owner") == {"S": "webhook-terraform-domain-operations-v1"}
            and entry.get("scope") == {"S": "internal"}
            and entry.get("status") == {"S": "active"}
            and entry.get("org_id") == {"S": value["adp_org_id"]}
            and entry.get("domain_org_id") == {"S": value["org_id"]}
            and entry.get("role_arn") == {"S": role_arn}
            and sorted(entry.get("credential_scopes", {}).get("SS", []))
            == sorted(scopes)
        )
        live = iam.get_role(RoleName=role_arn.rsplit("/", 1)[1])["Role"]
        require(
            live["Arn"] == role_arn
            and entry.get("iam_role_id") == {"S": live["RoleId"]}
        )
    if old is None and not check_only:
        ddb.put_item(
            TableName=table,
            Item=expected,
            ConditionExpression="attribute_not_exists(record_id)",
        )
        require(
            ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
            == expected
        )
    return {
        "state": "configured" if old is not None or not check_only else "absent",
        "credential_id": value["credential_id"],
        "revision": schema.digest(value),
        "human_admitted": False,
        "worker_ready": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", required=True)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply the already-reviewed exact conditional enrollment/revocation",
    )
    parser.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    path = Path(args.document)
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and not path.is_symlink()
        and not info.st_mode & 0o077
        and info.st_uid == os.getuid()
    )
    require(info.st_size <= 65536)
    try:
        result = enroll(
            json.loads(path.read_text()),
            boto3.Session(),
            check_only=not args.write,
            revoke=args.revoke,
        )
    except Exception:
        # No remote text, path, manifest or secret reference is printed on failure.
        raise SystemExit(
            "Provider authority operation refused; preserve the exact document and reconcile with its owner."
        ) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
