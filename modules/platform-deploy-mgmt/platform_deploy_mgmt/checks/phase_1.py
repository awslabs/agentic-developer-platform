"""Phase 1 verification checks: Bootstrap state backend.

Checks 1.1–1.8 verify that the Terraform state backend (S3 bucket + DynamoDB
lock table) is correctly configured in the customer's AWS account.

Spec source: docs/adp-platform-deployment/phase-verification.md
"""

from __future__ import annotations

import time

from botocore.exceptions import ClientError

from .boto_helpers import Context
from .shape import CheckResult, CostClass, Result, Severity


def check_1_1_state_bucket_exists(ctx: Context) -> CheckResult:
    """Verify the Terraform state S3 bucket exists in the customer account."""
    start = time.perf_counter_ns()
    bucket_name = ctx.state_bucket_name
    s3 = ctx.customer_session.client("s3")
    try:
        s3.head_bucket(Bucket=bucket_name)
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        return CheckResult(
            id="1.1",
            name="State bucket exists in target account",
            result=Result.PASS,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail=f"Bucket {bucket_name} exists and is accessible.",
            evidence={"bucket": bucket_name},
        )
    except ClientError as e:
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        code = e.response.get("Error", {}).get("Code", "Unknown")
        return CheckResult(
            id="1.1",
            name="State bucket exists in target account",
            result=Result.FAIL,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail=f"Bucket {bucket_name} not found or not accessible: {code}",
            evidence={"bucket": bucket_name, "error_code": code},
        )


def check_1_2_versioning_enabled(ctx: Context) -> CheckResult:
    """Verify S3 bucket versioning is enabled on the state bucket."""
    start = time.perf_counter_ns()
    bucket_name = ctx.state_bucket_name
    s3 = ctx.customer_session.client("s3")
    try:
        resp = s3.get_bucket_versioning(Bucket=bucket_name)
        status = resp.get("Status", "Disabled")
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        if status == "Enabled":
            return CheckResult(
                id="1.2",
                name="State bucket versioning enabled",
                result=Result.PASS,
                severity=Severity.HARD,
                duration_ms=duration_ms,
                detail="Versioning is enabled.",
                evidence={"bucket": bucket_name, "versioning_status": status},
            )
        return CheckResult(
            id="1.2",
            name="State bucket versioning enabled",
            result=Result.FAIL,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail=f"Versioning is {status}, expected Enabled.",
            evidence={"bucket": bucket_name, "versioning_status": status},
        )
    except ClientError as e:
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        return CheckResult(
            id="1.2",
            name="State bucket versioning enabled",
            result=Result.FAIL,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail=f"Could not check versioning: {e}",
            evidence={"bucket": bucket_name, "error": str(e)},
        )


def check_1_3_encryption_enabled(ctx: Context) -> CheckResult:
    """Verify server-side encryption is configured on the state bucket."""
    start = time.perf_counter_ns()
    bucket_name = ctx.state_bucket_name
    s3 = ctx.customer_session.client("s3")
    try:
        resp = s3.get_bucket_encryption(Bucket=bucket_name)
        rules = resp.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        if rules:
            algo = rules[0].get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm", "none")
            return CheckResult(
                id="1.3",
                name="State bucket encryption enabled",
                result=Result.PASS,
                severity=Severity.HARD,
                duration_ms=duration_ms,
                detail=f"Encryption configured with {algo}.",
                evidence={"bucket": bucket_name, "algorithm": algo},
            )
        return CheckResult(
            id="1.3",
            name="State bucket encryption enabled",
            result=Result.FAIL,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail="No encryption rules found.",
            evidence={"bucket": bucket_name, "rules": []},
        )
    except ClientError as e:
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        code = e.response.get("Error", {}).get("Code", "Unknown")
        # ServerSideEncryptionConfigurationNotFoundError means no encryption
        if "NotFound" in code or "NoSuchEncryption" in code:
            return CheckResult(
                id="1.3",
                name="State bucket encryption enabled",
                result=Result.FAIL,
                severity=Severity.HARD,
                duration_ms=duration_ms,
                detail="No encryption configuration found on bucket.",
                evidence={"bucket": bucket_name, "error_code": code},
            )
        return CheckResult(
            id="1.3",
            name="State bucket encryption enabled",
            result=Result.FAIL,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail=f"Error checking encryption: {e}",
            evidence={"bucket": bucket_name, "error": str(e)},
        )


def check_1_4_public_access_blocked(ctx: Context) -> CheckResult:
    """Verify public access is fully blocked on the state bucket."""
    start = time.perf_counter_ns()
    bucket_name = ctx.state_bucket_name
    s3 = ctx.customer_session.client("s3")
    try:
        resp = s3.get_public_access_block(Bucket=bucket_name)
        config = resp.get("PublicAccessBlockConfiguration", {})
        all_blocked = all(
            [
                config.get("BlockPublicAcls", False),
                config.get("IgnorePublicAcls", False),
                config.get("BlockPublicPolicy", False),
                config.get("RestrictPublicBuckets", False),
            ]
        )
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        if all_blocked:
            return CheckResult(
                id="1.4",
                name="State bucket public access blocked",
                result=Result.PASS,
                severity=Severity.HARD,
                duration_ms=duration_ms,
                detail="All public access blocks enabled.",
                evidence={"bucket": bucket_name, "config": config},
            )
        return CheckResult(
            id="1.4",
            name="State bucket public access blocked",
            result=Result.FAIL,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail=f"Not all public access blocks enabled: {config}",
            evidence={"bucket": bucket_name, "config": config},
        )
    except ClientError as e:
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        return CheckResult(
            id="1.4",
            name="State bucket public access blocked",
            result=Result.FAIL,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail=f"Could not check public access block: {e}",
            evidence={"bucket": bucket_name, "error": str(e)},
        )


def check_1_5_lock_table_exists(ctx: Context) -> CheckResult:
    """Verify the DynamoDB lock table exists and is ACTIVE."""
    start = time.perf_counter_ns()
    table_name = "adp-terraform-locks"
    dynamodb = ctx.customer_session.client("dynamodb")
    try:
        resp = dynamodb.describe_table(TableName=table_name)
        status = resp["Table"]["TableStatus"]
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        if status == "ACTIVE":
            return CheckResult(
                id="1.5",
                name="DynamoDB lock table exists and active",
                result=Result.PASS,
                severity=Severity.HARD,
                duration_ms=duration_ms,
                detail=f"Table {table_name} is ACTIVE.",
                evidence={"table": table_name, "status": status},
            )
        return CheckResult(
            id="1.5",
            name="DynamoDB lock table exists and active",
            result=Result.FAIL,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail=f"Table {table_name} status is {status}, expected ACTIVE.",
            evidence={"table": table_name, "status": status},
        )
    except ClientError as e:
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        code = e.response.get("Error", {}).get("Code", "Unknown")
        return CheckResult(
            id="1.5",
            name="DynamoDB lock table exists and active",
            result=Result.FAIL,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail=f"Table {table_name} not found or error: {code}",
            evidence={"table": table_name, "error_code": code},
        )


def check_1_6_lock_table_has_lock_id_key(ctx: Context) -> CheckResult:
    """Verify DynamoDB lock table has LockID as the partition key."""
    start = time.perf_counter_ns()
    table_name = "adp-terraform-locks"
    dynamodb = ctx.customer_session.client("dynamodb")
    try:
        resp = dynamodb.describe_table(TableName=table_name)
        key_schema = resp["Table"]["KeySchema"]
        hash_key = next((k["AttributeName"] for k in key_schema if k["KeyType"] == "HASH"), None)
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        if hash_key == "LockID":
            return CheckResult(
                id="1.6",
                name="Lock table has LockID partition key",
                result=Result.PASS,
                severity=Severity.HARD,
                duration_ms=duration_ms,
                detail="Partition key is LockID (as required by Terraform).",
                evidence={"table": table_name, "hash_key": hash_key},
            )
        return CheckResult(
            id="1.6",
            name="Lock table has LockID partition key",
            result=Result.FAIL,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail=f"Partition key is '{hash_key}', expected 'LockID'.",
            evidence={"table": table_name, "hash_key": hash_key},
        )
    except ClientError as e:
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        return CheckResult(
            id="1.6",
            name="Lock table has LockID partition key",
            result=Result.FAIL,
            severity=Severity.HARD,
            duration_ms=duration_ms,
            detail=f"Error checking lock table schema: {e}",
            evidence={"table": table_name, "error": str(e)},
        )


def check_1_7_backend_tfvars_substituted(ctx: Context) -> CheckResult:
    """Verify backend.tfvars contains the customer's account ID (not a placeholder)."""
    start = time.perf_counter_ns()
    import os

    # Look for the backend.tfvars relative to the repo root
    repo_root = os.environ.get("GITHUB_WORKSPACE", os.getcwd())
    tfvars_path = os.path.join(repo_root, "environments", "dev", "backend.tfvars")
    try:
        with open(tfvars_path) as f:
            content = f.read()
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        account_id = ctx.customer_account_id
        if account_id in content:
            return CheckResult(
                id="1.7",
                name="backend.tfvars contains account ID",
                result=Result.PASS,
                severity=Severity.SOFT,
                duration_ms=duration_ms,
                detail=f"backend.tfvars references account {account_id}.",
                evidence={"file": tfvars_path, "account_id_found": True},
            )
        # Check if it still has the placeholder
        if "ACCOUNT_ID" in content or "000000000000" in content:
            return CheckResult(
                id="1.7",
                name="backend.tfvars contains account ID",
                result=Result.FAIL,
                severity=Severity.SOFT,
                duration_ms=duration_ms,
                detail="backend.tfvars still contains placeholder. Run bootstrap.sh.",
                evidence={"file": tfvars_path, "placeholder_found": True},
            )
        # The file exists but references a different account
        return CheckResult(
            id="1.7",
            name="backend.tfvars contains account ID",
            result=Result.PASS,
            severity=Severity.SOFT,
            duration_ms=duration_ms,
            detail="backend.tfvars is populated (may reference platform account).",
            evidence={"file": tfvars_path, "account_id_found": False},
        )
    except FileNotFoundError:
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        return CheckResult(
            id="1.7",
            name="backend.tfvars contains account ID",
            result=Result.FAIL,
            severity=Severity.SOFT,
            duration_ms=duration_ms,
            detail=f"File not found: {tfvars_path}",
            evidence={"file": tfvars_path, "exists": False},
        )


def check_1_8_state_bucket_not_empty(ctx: Context) -> CheckResult:
    """Verify the state bucket has at least one object (state file written)."""
    start = time.perf_counter_ns()
    bucket_name = ctx.state_bucket_name
    s3 = ctx.customer_session.client("s3")
    try:
        resp = s3.list_objects_v2(Bucket=bucket_name, MaxKeys=1)
        key_count = resp.get("KeyCount", 0)
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        if key_count > 0:
            return CheckResult(
                id="1.8",
                name="State bucket contains state files",
                result=Result.PASS,
                severity=Severity.SOFT,
                duration_ms=duration_ms,
                detail="Bucket contains at least one object.",
                evidence={"bucket": bucket_name, "has_objects": True},
            )
        return CheckResult(
            id="1.8",
            name="State bucket contains state files",
            result=Result.FAIL,
            severity=Severity.SOFT,
            duration_ms=duration_ms,
            detail="Bucket is empty — no state files written yet.",
            evidence={"bucket": bucket_name, "has_objects": False},
        )
    except ClientError as e:
        duration_ms = (time.perf_counter_ns() - start) // 1_000_000
        return CheckResult(
            id="1.8",
            name="State bucket contains state files",
            result=Result.FAIL,
            severity=Severity.SOFT,
            duration_ms=duration_ms,
            detail=f"Error listing bucket objects: {e}",
            evidence={"bucket": bucket_name, "error": str(e)},
        )


# Registry of all Phase 1 checks.
# Format: (id, name, fn, severity, cost_class)
CHECKS = [
    ("1.1", "State bucket exists in target account", check_1_1_state_bucket_exists, Severity.HARD, CostClass.CHEAP),
    ("1.2", "State bucket versioning enabled", check_1_2_versioning_enabled, Severity.HARD, CostClass.CHEAP),
    ("1.3", "State bucket encryption enabled", check_1_3_encryption_enabled, Severity.HARD, CostClass.CHEAP),
    ("1.4", "State bucket public access blocked", check_1_4_public_access_blocked, Severity.HARD, CostClass.CHEAP),
    ("1.5", "DynamoDB lock table exists and active", check_1_5_lock_table_exists, Severity.HARD, CostClass.CHEAP),
    (
        "1.6",
        "Lock table has LockID partition key",
        check_1_6_lock_table_has_lock_id_key,
        Severity.HARD,
        CostClass.CHEAP,
    ),
    ("1.7", "backend.tfvars contains account ID", check_1_7_backend_tfvars_substituted, Severity.SOFT, CostClass.CHEAP),
    ("1.8", "State bucket contains state files", check_1_8_state_bucket_not_empty, Severity.SOFT, CostClass.CHEAP),
]
