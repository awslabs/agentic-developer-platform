#!/usr/bin/env python3
"""Read-only preparation of additive Task API IAM/Cognito operator artifacts.

Never applies changes. In particular, UpdateUserPool resets omitted mutable
settings; the output preserves every live setting accepted by that API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from string import Template

import boto3

SCOPES = {
    "submit": "Submit bounded tasks",
    "read": "Read owned task state and events",
    "input": "Send follow-up input to owned tasks",
    "cancel": "Cancel owned tasks",
    "artifacts": "Upload and read owned task artifacts",
}


def pool_update(pool: dict, accepted_members: set[str], *, enable_m2m: bool) -> dict:
    payload = {key: value for key, value in pool.items() if key in accepted_members and key != "UserPoolId"}
    payload["UserPoolId"] = pool["Id"]
    payload = json.loads(json.dumps(payload))
    if enable_m2m:
        config = payload.get("LambdaConfig", {}).get("PreTokenGenerationConfig")
        if not config or not config.get("LambdaArn"):
            raise ValueError("Pool has no existing pre-token generation Lambda; do not invent its target")
        config["LambdaVersion"] = "V3_0"
    return payload


def render_policy(**bindings: str) -> dict:
    template = Path(__file__).resolve().parents[1] / "infra/policies/task-api.json.tftpl"
    return json.loads(Template(template.read_text()).substitute(bindings))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--user-pool-id", required=True)
    parser.add_argument("--artifact-bucket", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    session = boto3.Session(region_name=args.region)
    identity = session.client("sts").get_caller_identity()
    if identity["Account"] != args.account:
        raise SystemExit("Active AWS account differs from the explicit target")
    dynamodb = session.client("dynamodb")
    tables = [dynamodb.describe_table(TableName=f"adp-{args.environment}-{suffix}")["Table"] for suffix in ("webhook-events", "agent-authority")]
    key_arns = {table.get("SSEDescription", {}).get("KMSMasterKeyArn") for table in tables}
    if len(key_arns) != 1 or None in key_arns:
        raise SystemExit("Expected both task tables to use the same real customer-managed DynamoDB key")
    s3 = session.client("s3")
    s3.head_bucket(Bucket=args.artifact_bucket, ExpectedBucketOwner=args.account)
    encryption = s3.get_bucket_encryption(Bucket=args.artifact_bucket, ExpectedBucketOwner=args.account)
    algorithms = {rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] for rule in encryption["ServerSideEncryptionConfiguration"]["Rules"]}
    if algorithms != {"AES256"}:
        raise SystemExit("Reviewed policy assumes existing AES256 artifact encryption; KMS requires an explicit key binding")
    cognito = session.client("cognito-idp")
    pool = cognito.describe_user_pool(UserPoolId=args.user_pool_id)["UserPool"]
    if f":{args.account}:" not in pool["Arn"]:
        raise SystemExit("Cognito pool does not belong to target account")
    if pool.get("UserPoolTier") not in {"ESSENTIALS", "PLUS"}:
        raise SystemExit("V3 M2M trigger requires a reviewed supported Cognito tier")
    accepted = set(cognito.meta.service_model.operation_model("UpdateUserPool").input_shape.members)
    role = session.client("iam").get_role(RoleName=f"adp-{args.environment}-role-gateway-service")["Role"]
    policy = render_policy(
        request_table_arn=tables[0]["TableArn"],
        authority_table_arn=tables[1]["TableArn"],
        artifact_bucket_arn=f"arn:aws:s3:::{args.artifact_bucket}",
        dynamodb_key_arn=next(iter(key_arns)),
        region=args.region,
        account_id=args.account,
    )
    objects = {
        "gateway-task-api-policy.json": policy,
        "cognito-pool-before.json": pool_update(pool, accepted, enable_m2m=False),
        "cognito-pool-v3.json": pool_update(pool, accepted, enable_m2m=True),
        "cognito-task-resource-server.json": {
            "UserPoolId": pool["Id"],
            "Identifier": "adp-tasks",
            "Name": "ADP Task API",
            "Scopes": [{"ScopeName": name, "ScopeDescription": text} for name, text in SCOPES.items()],
        },
        "bindings.json": {
            "account": args.account,
            "region": args.region,
            "role_arn": role["Arn"],
            "request_table": tables[0]["TableName"],
            "authority_table": tables[1]["TableName"],
            "dynamodb_key": next(iter(key_arns)),
            "artifact_bucket": args.artifact_bucket,
            "task_index_status": next(
                (index["IndexStatus"] for index in tables[0].get("GlobalSecondaryIndexes", []) if index["IndexName"] == "task-work-index"), "ABSENT"
            ),
            "user_pool_id": pool["Id"],
            "user_pool_tier": pool["UserPoolTier"],
            "pretoken_lambda": pool["LambdaConfig"]["PreTokenGenerationConfig"]["LambdaArn"],
        },
    }
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    hashes = {}
    for name, value in objects.items():
        raw = (json.dumps(value, indent=2) + "\n").encode()
        target = args.output_dir / name
        target.write_bytes(raw)
        os.chmod(target, 0o600)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    (args.output_dir / "hashes.json").write_text(json.dumps(hashes, indent=2) + "\n")
    print(json.dumps({"output_dir": str(args.output_dir), "bindings": objects["bindings.json"], "mode": "prepared_only"}))


if __name__ == "__main__":
    main()
