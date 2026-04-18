#!/usr/bin/env python3
"""AWS Infrastructure discovery — Resource Explorer + IaC parsing.

Discovers resources via AWS Resource Explorer, parses IaC (Terraform, CloudFormation, CDK)
from indexed repos, and uploads resource inventory to OpenViking.

Usage:
  python discover-infra.py --accounts-file /config/accounts.txt
  python discover-infra.py --account 605440105851 --role AgentContextReadOnly --regions us-east-1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("discover-infra")

OV_URL = os.getenv("OV_URL", "http://openviking.agent-context.svc.cluster.local:1933")
OV_KEY = os.getenv("OPENVIKING_ROOT_KEY", os.getenv("ROOT_KEY", ""))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))

# Try importing boto3
try:
    import boto3
    from botocore.exceptions import ClientError

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False
    log.warning("boto3 not available — AWS discovery will be limited")


# ---------------------------------------------------------------------------
# OpenViking helpers
# ---------------------------------------------------------------------------


def ov_headers(api_key: str) -> dict[str, str]:
    return {
        "X-API-Key": api_key,
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": "default",
    }


def upload_to_openviking(ov_url: str, headers: dict, content: str, filename: str, target_uri: str) -> bool:
    """Upload content to OpenViking via temp_upload."""
    try:
        files = {"file": (filename, content.encode("utf-8"), "application/octet-stream")}
        resp = requests.post(
            f"{ov_url}/api/v1/resources/temp_upload",
            headers={k: v for k, v in headers.items() if k != "Content-Type"},
            files=files,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 300:
            return False

        temp_id = resp.json().get("result", {}).get("temp_file_id")
        if not temp_id:
            return False

        resp = requests.post(
            f"{ov_url}/api/v1/resources",
            headers={**headers, "Content-Type": "application/json"},
            json={"temp_file_id": temp_id, "to": target_uri, "wait": True, "timeout": REQUEST_TIMEOUT},
            timeout=REQUEST_TIMEOUT + 10,
        )
        return resp.status_code < 300
    except Exception as e:
        log.error("Upload failed: %s", e)
        return False


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
# IaC parsing (from OpenViking indexed repos)
# ---------------------------------------------------------------------------


def parse_iac_from_repos(ov_url: str, headers: dict) -> dict[str, Any]:
    """Parse IaC declarations from repos already indexed in OpenViking.

    Searches for Terraform (.tf), CloudFormation (.yaml/.json), and CDK patterns.
    Returns a map of repo -> IaC resources.
    """
    iac_map: dict[str, Any] = {}

    # Search OpenViking for Terraform files
    try:
        resp = requests.post(
            f"{ov_url}/api/v1/search/search",
            headers={**headers, "Content-Type": "application/json"},
            json={"query": "resource aws terraform provider", "target_uri": "viking://resources/"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code < 300:
            data = resp.json()
            results = data.get("result", {})
            if isinstance(results, dict):
                results = results.get("resources", [])
            for r in results:
                uri = r.get("uri", "")
                if ".tf" in uri or "terraform" in uri.lower():
                    # Extract repo from URI
                    parts = uri.replace("viking://resources/", "").split("/", 2)
                    if len(parts) >= 2:
                        repo = f"{parts[0]}/{parts[1]}"
                        if repo not in iac_map:
                            iac_map[repo] = {"terraform_files": [], "resources": []}
                        iac_map[repo]["terraform_files"].append(uri)
    except Exception as e:
        log.warning("Failed to search for IaC files: %s", e)

    return iac_map


# ---------------------------------------------------------------------------
# CI/CD workflow parsing
# ---------------------------------------------------------------------------


def parse_workflows_from_repos(ov_url: str, headers: dict) -> dict[str, Any]:
    """Search for CI/CD workflow files in indexed repos."""
    deploy_map: dict[str, Any] = {}

    try:
        resp = requests.post(
            f"{ov_url}/api/v1/search/search",
            headers={**headers, "Content-Type": "application/json"},
            json={"query": "github actions workflow deploy build", "target_uri": "viking://resources/"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code < 300:
            data = resp.json()
            results = data.get("result", {})
            if isinstance(results, dict):
                results = results.get("resources", [])
            for r in results:
                uri = r.get("uri", "")
                if ".github/workflows" in uri or "buildspec" in uri:
                    parts = uri.replace("viking://resources/", "").split("/", 2)
                    if len(parts) >= 2:
                        repo = f"{parts[0]}/{parts[1]}"
                        if repo not in deploy_map:
                            deploy_map[repo] = {"workflows": []}
                        deploy_map[repo]["workflows"].append(uri)
    except Exception as e:
        log.warning("Failed to search for workflow files: %s", e)

    return deploy_map


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="AWS infrastructure discovery")
    parser.add_argument("--accounts-file", default=os.getenv("ACCOUNTS_FILE", "/config/accounts.txt"))
    parser.add_argument("--account", help="Single account to discover (overrides file)")
    parser.add_argument("--role", default="AgentContextReadOnly")
    parser.add_argument("--regions", default="us-east-1", help="Comma-separated regions")
    args = parser.parse_args()

    if not OV_KEY:
        log.error("No OpenViking API key.")
        sys.exit(1)

    headers = ov_headers(OV_KEY)

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

        # Upload resource inventory
        target_uri = f"viking://resources/infra/{account_id}/resources.json"
        content = json.dumps(resources, indent=2)
        uploaded = upload_to_openviking(OV_URL, headers, content, f"resources-{account_id}.json", target_uri)
        if uploaded:
            log.info("Uploaded %d resources for account %s", resources["total_resources"], account_id)
        else:
            log.warning("Failed to upload resources for account %s", account_id)

    # Step 2: Parse IaC from indexed repos
    log.info("Parsing IaC from indexed repos...")
    iac_map = parse_iac_from_repos(OV_URL, headers)
    for repo, iac_data in iac_map.items():
        safe_repo = repo.replace("/", "-")
        target_uri = f"viking://resources/{repo}/.infra-map.json"
        upload_to_openviking(
            OV_URL,
            headers,
            json.dumps(iac_data, indent=2),
            f"{safe_repo}-infra-map.json",
            target_uri,
        )

    # Step 3: Parse CI/CD workflows from indexed repos
    log.info("Parsing CI/CD workflows from indexed repos...")
    deploy_map = parse_workflows_from_repos(OV_URL, headers)
    for repo, deploy_data in deploy_map.items():
        safe_repo = repo.replace("/", "-")
        target_uri = f"viking://resources/{repo}/.deploy-map.json"
        upload_to_openviking(
            OV_URL,
            headers,
            json.dumps(deploy_data, indent=2),
            f"{safe_repo}-deploy-map.json",
            target_uri,
        )

    log.info(
        "Infrastructure discovery complete: %d accounts, %d repos with IaC, %d repos with workflows",
        len(accounts),
        len(iac_map),
        len(deploy_map),
    )


if __name__ == "__main__":
    main()
