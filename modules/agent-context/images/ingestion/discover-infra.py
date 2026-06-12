#!/usr/bin/env python3
"""AWS Infrastructure discovery — Resource Explorer + IaC parsing.

Discovers resources via AWS Resource Explorer, parses IaC (Terraform, CloudFormation, CDK)
from indexed repos, and uploads resource inventory to S3.

Usage:
  python discover-infra.py --accounts-file /config/accounts.txt
  python discover-infra.py --account <target-account-id> --role AgentContextReadOnly --regions us-east-1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("discover-infra")

from config import settings
from s3_store import S3ContentStore

REQUEST_TIMEOUT = settings.request_timeout

# Try importing boto3
try:
    import boto3
    from botocore.exceptions import ClientError

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False
    log.warning("boto3 not available — AWS discovery will be limited")


# ---------------------------------------------------------------------------
# Account parsing
# ---------------------------------------------------------------------------


def parse_accounts_file(path: str) -> list[dict[str, Any]]:
    """Parse accounts.txt: format account_id:role_name:regions."""
    accounts = []
    if not os.path.exists(path):
        log.warning("Accounts file not found: %s", path)
        return accounts
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) >= 3:
                accounts.append({
                    "account_id": parts[0],
                    "role_name": parts[1],
                    "regions": parts[2].split(","),
                })
            elif len(parts) == 2:
                accounts.append({
                    "account_id": parts[0],
                    "role_name": parts[1],
                    "regions": ["us-east-1"],
                })
            else:
                log.warning("Invalid accounts.txt line: %s", line)
    return accounts


# ---------------------------------------------------------------------------
# AWS Resource Explorer
# ---------------------------------------------------------------------------


def assume_role(account_id: str, role_name: str) -> dict[str, str] | None:
    """Assume a cross-account IAM role, returning temporary credentials."""
    if not BOTO3_AVAILABLE:
        return None

    try:
        sts = boto3.client("sts")
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        response = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="agent-context-discovery",
            DurationSeconds=3600,
        )
        creds = response["Credentials"]
        return {
            "aws_access_key_id": creds["AccessKeyId"],
            "aws_secret_access_key": creds["SecretAccessKey"],
            "aws_session_token": creds["SessionToken"],
        }
    except Exception as e:
        log.warning("Could not assume role for account %s: %s", account_id, e)
        return None


def discover_resources(account_id: str, role_name: str, regions: list[str]) -> dict[str, Any]:
    """Discover AWS resources via Resource Explorer."""
    if not BOTO3_AVAILABLE:
        return {"account_id": account_id, "error": "boto3 not available", "resources": []}

    creds = assume_role(account_id, role_name)
    if not creds:
        # Try without assuming role (same account)
        creds = {}

    all_resources: list[dict[str, Any]] = []
    resource_by_type: dict[str, list[dict]] = {}

    for region in regions:
        try:
            client = boto3.client(
                "resource-explorer-2",
                region_name=region,
                **creds,
            )

            # Search for all resources
            paginator = client.get_paginator("search")
            page_iterator = paginator.paginate(QueryString="*")

            for page in page_iterator:
                for resource in page.get("Resources", []):
                    resource_info = {
                        "arn": resource.get("Arn", ""),
                        "region": resource.get("Region", region),
                        "resource_type": resource.get("ResourceType", ""),
                        "service": resource.get("Service", ""),
                        "owning_account_id": resource.get("OwningAccountId", account_id),
                    }
                    all_resources.append(resource_info)

                    rtype = resource_info["resource_type"]
                    if rtype not in resource_by_type:
                        resource_by_type[rtype] = []
                    resource_by_type[rtype].append(resource_info)

            log.info(
                "Discovered %d resources in %s/%s",
                len(all_resources),
                account_id,
                region,
            )

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "AccessDeniedException":
                log.warning(
                    "Access denied to Resource Explorer in %s/%s — "
                    "ensure the role has resource-explorer-2:Search permission",
                    account_id,
                    region,
                )
            else:
                log.warning("Resource Explorer error in %s/%s: %s", account_id, region, e)
        except Exception as e:
            log.warning("Resource Explorer failed in %s/%s: %s", account_id, region, e)

    return {
        "account_id": account_id,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "total_resources": len(all_resources),
        "resource_types": {k: len(v) for k, v in resource_by_type.items()},
        "resources": all_resources,
    }


# ---------------------------------------------------------------------------
# IaC parsing (from S3 content store)
# ---------------------------------------------------------------------------


def parse_iac_from_repos(store: S3ContentStore) -> dict[str, Any]:
    """Parse IaC declarations from repos already indexed in the S3 content store.

    Lists the repos/ prefix and looks for .infra-map.json files.
    Returns a map of repo -> IaC resources.
    """
    iac_map: dict[str, Any] = {}

    try:
        # List org-level directories under repos/
        orgs = store.list_prefix("repos/")
        for org_entry in orgs:
            org_name = org_entry.get("name", "")
            if not org_entry.get("is_dir", False):
                continue
            # List repos within this org
            repos = store.list_prefix(f"repos/{org_name}/")
            for repo_entry in repos:
                repo_name = repo_entry.get("name", "")
                if not repo_entry.get("is_dir", False):
                    continue
                repo_path = f"{org_name}/{repo_name}"
                infra_data = store.get_json(f"repos/{repo_path}/.infra-map.json")
                if infra_data:
                    iac_map[repo_path] = infra_data
    except Exception as e:
        log.warning("Failed to read IaC maps from S3: %s", e)

    return iac_map


# ---------------------------------------------------------------------------
# CI/CD workflow parsing
# ---------------------------------------------------------------------------


def parse_workflows_from_repos(store: S3ContentStore) -> dict[str, Any]:
    """Search for CI/CD workflow/deploy maps in the S3 content store."""
    deploy_map: dict[str, Any] = {}

    try:
        # List org-level directories under repos/
        orgs = store.list_prefix("repos/")
        for org_entry in orgs:
            org_name = org_entry.get("name", "")
            if not org_entry.get("is_dir", False):
                continue
            # List repos within this org
            repos = store.list_prefix(f"repos/{org_name}/")
            for repo_entry in repos:
                repo_name = repo_entry.get("name", "")
                if not repo_entry.get("is_dir", False):
                    continue
                repo_path = f"{org_name}/{repo_name}"
                deploy_data = store.get_json(f"repos/{repo_path}/.deploy-map.json")
                if deploy_data:
                    deploy_map[repo_path] = deploy_data
    except Exception as e:
        log.warning("Failed to read deploy maps from S3: %s", e)

    return deploy_map


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="AWS infrastructure discovery")
    parser.add_argument("--accounts-file", default=settings.accounts_file)
    parser.add_argument("--account", help="Single account to discover (overrides file)")
    parser.add_argument("--role", default="AgentContextReadOnly")
    parser.add_argument("--regions", default="us-east-1", help="Comma-separated regions")
    args = parser.parse_args()

    if not settings.s3_bucket_name:
        log.error("No S3 bucket name configured.")
        sys.exit(1)

    store = S3ContentStore(
        bucket_name=settings.s3_bucket_name,
        prefix=settings.s3_content_prefix,
        region_name=settings.aws_region,
    )

    # Determine accounts to discover
    if args.account:
        accounts = [{"account_id": args.account, "role_name": args.role, "regions": args.regions.split(",")}]
    else:
        accounts = parse_accounts_file(args.accounts_file)

    if not accounts:
        log.info("No accounts to discover")
        return

    # Step 1: Discover AWS resources for each account
    for acct in accounts:
        account_id = acct["account_id"]
        log.info("Discovering resources for account %s...", account_id)

        resources = discover_resources(account_id, acct["role_name"], acct["regions"])

        # Upload resource inventory to S3
        content = json.dumps(resources, indent=2)
        uploaded = store.put_content(f"infra/{account_id}/resources.json", content)
        if uploaded:
            log.info("Uploaded %d resources for account %s", resources["total_resources"], account_id)
        else:
            log.warning("Failed to upload resources for account %s", account_id)

    # Step 2: Parse IaC from indexed repos
    log.info("Parsing IaC from indexed repos...")
    iac_map = parse_iac_from_repos(store)
    for repo, iac_data in iac_map.items():
        store.put_content(
            f"repos/{repo}/.infra-map.json",
            json.dumps(iac_data, indent=2),
        )

    # Step 3: Parse CI/CD workflows from indexed repos
    log.info("Parsing CI/CD workflows from indexed repos...")
    deploy_map = parse_workflows_from_repos(store)
    for repo, deploy_data in deploy_map.items():
        store.put_content(
            f"repos/{repo}/.deploy-map.json",
            json.dumps(deploy_data, indent=2),
        )

    log.info(
        "Infrastructure discovery complete: %d accounts, %d repos with IaC, %d repos with workflows",
        len(accounts),
        len(iac_map),
        len(deploy_map),
    )


if __name__ == "__main__":
    main()
