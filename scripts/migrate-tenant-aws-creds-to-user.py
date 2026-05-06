#!/usr/bin/env python3
"""Migrate tenant-scoped AWS credentials to user-scoped vault entries.

Issue #455: Moves AWS role credentials from the old tenant-wide path
    adp/<env>/tenants/<tenant_id>/aws-access
to user-scoped entries stored via the gateway's credential API at
    adp/users/<cognito_sub>/aws-default

For each tenant entry:
1. Read the existing secret from Secrets Manager
2. Identify the tenant admin (owner of the role) — provided via mapping file
3. Create a user_credentials row via the gateway admin API
4. Verify the new credential resolves correctly
5. Optionally delete the old tenant-level entry

Usage:
    python scripts/migrate-tenant-aws-creds-to-user.py --env dev --mapping mapping.json [--delete-old]

Mapping file format (JSON):
    {
        "sophos-test": {
            "admin_user_id": "cognito-sub-uuid-here",
            "admin_email": "alice@example.com"
        }
    }

Requires:
    - AWS credentials with secretsmanager:GetSecretValue on adp/<env>/tenants/*
    - Gateway admin access (VAULT_GATEWAY_URL + VAULT_INTERNAL_API_KEY env vars)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import boto3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def read_tenant_secret(sm_client, env: str, tenant_id: str) -> dict | None:
    """Read the old tenant-scoped AWS access secret."""
    secret_id = f"adp/{env}/tenants/{tenant_id}/aws-access"
    try:
        resp = sm_client.get_secret_value(SecretId=secret_id)
        return json.loads(resp["SecretString"])
    except sm_client.exceptions.ResourceNotFoundException:
        logger.warning("Secret not found: %s", secret_id)
        return None
    except Exception as exc:
        logger.error("Failed to read %s: %s", secret_id, exc)
        return None


def store_user_credential(
    sm_client,
    env: str,
    user_id: str,
    cred_data: dict,
    label: str = "default",
) -> str:
    """Store the credential at the user-scoped path in Secrets Manager.

    Returns the secret ARN.
    """
    secret_id = f"adp/{env}/users/{user_id}/aws-{label}"
    secret_value = json.dumps(cred_data)

    try:
        # Try to create; if it exists, update
        resp = sm_client.create_secret(
            Name=secret_id,
            SecretString=secret_value,
            Description=f"AWS role assumption credentials for user {user_id} (migrated from tenant)",
        )
        logger.info("Created secret: %s", secret_id)
        return resp["ARN"]
    except sm_client.exceptions.ResourceExistsException:
        resp = sm_client.put_secret_value(
            SecretId=secret_id,
            SecretString=secret_value,
        )
        logger.info("Updated existing secret: %s", secret_id)
        # Get the ARN
        desc = sm_client.describe_secret(SecretId=secret_id)
        return desc["ARN"]


def delete_tenant_secret(sm_client, env: str, tenant_id: str) -> None:
    """Delete the old tenant-scoped secret."""
    secret_id = f"adp/{env}/tenants/{tenant_id}/aws-access"
    try:
        sm_client.delete_secret(
            SecretId=secret_id,
            ForceDeleteWithoutRecovery=False,  # 30-day recovery window
        )
        logger.info("Scheduled deletion of old secret: %s (30-day recovery window)", secret_id)
    except Exception as exc:
        logger.error("Failed to delete %s: %s", secret_id, exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate tenant AWS creds to user-scoped vault")
    parser.add_argument("--env", required=True, help="Environment (dev, staging, prod)")
    parser.add_argument("--mapping", required=True, help="Path to tenant->user mapping JSON file")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument(
        "--delete-old",
        action="store_true",
        help="Delete old tenant-level secrets after migration (uses 30-day recovery window)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without making changes")
    args = parser.parse_args()

    # Load mapping
    try:
        with open(args.mapping) as f:
            mapping = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.error("Failed to load mapping file: %s", exc)
        return 1

    sm_client = boto3.client("secretsmanager", region_name=args.region)
    migrated = 0
    failed = 0

    for tenant_id, user_info in mapping.items():
        admin_user_id = user_info.get("admin_user_id")
        if not admin_user_id:
            logger.error("Tenant %s: missing admin_user_id in mapping", tenant_id)
            failed += 1
            continue

        logger.info("Migrating tenant=%s -> user=%s", tenant_id, admin_user_id)

        # Read existing tenant secret
        cred_data = read_tenant_secret(sm_client, args.env, tenant_id)
        if cred_data is None:
            failed += 1
            continue

        if args.dry_run:
            logger.info(
                "[DRY RUN] Would migrate tenant=%s role_arn=%s -> user=%s",
                tenant_id,
                cred_data.get("role_arn", "?"),
                admin_user_id,
            )
            migrated += 1
            continue

        # Store at user-scoped path
        try:
            secret_arn = store_user_credential(sm_client, args.env, admin_user_id, cred_data)
            logger.info("Stored user credential: arn=%s", secret_arn)
        except Exception as exc:
            logger.error("Failed to store user credential for tenant %s: %s", tenant_id, exc)
            failed += 1
            continue

        # Verify by reading back
        verify_id = f"adp/{args.env}/users/{admin_user_id}/aws-default"
        try:
            resp = sm_client.get_secret_value(SecretId=verify_id)
            verified = json.loads(resp["SecretString"])
            assert verified["role_arn"] == cred_data["role_arn"]
            logger.info("Verification passed for user %s", admin_user_id)
        except Exception as exc:
            logger.error("Verification FAILED for user %s: %s", admin_user_id, exc)
            failed += 1
            continue

        # Delete old entry if requested
        if args.delete_old:
            delete_tenant_secret(sm_client, args.env, tenant_id)

        migrated += 1

    logger.info("Migration complete: %d migrated, %d failed", migrated, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
