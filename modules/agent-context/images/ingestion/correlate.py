#!/usr/bin/env python3
"""Correlate code repos with AWS infrastructure and CI/CD pipelines.

Reads resource inventory, IaC maps, and deploy maps from OpenViking,
then produces a relationship-graph.json linking repos to resources to pipelines.

Usage:
  python correlate.py
  python correlate.py --accounts-file /config/accounts.txt
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("correlate")

from config import settings

OV_URL = settings.ov_url
OV_KEY = settings.ov_key
REQUEST_TIMEOUT = settings.request_timeout


def ov_headers(api_key: str) -> dict[str, str]:
    return {
        "X-API-Key": api_key,
        "X-OpenViking-Account": "default",
        "X-OpenViking-User": "default",
    }


def upload_to_openviking(ov_url: str, headers: dict, content: str, filename: str, target_uri: str) -> bool:
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


def read_from_openviking(ov_url: str, headers: dict, uri: str) -> dict | None:
    """Read a JSON resource from OpenViking."""
    try:
        resp = requests.get(
            f"{ov_url}/api/v1/content/read",
            headers=headers,
            params={"uri": uri},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code < 300:
            data = resp.json()
            content = data.get("result", "") if isinstance(data, dict) else str(data)
            if content:
                return json.loads(content)
        return None
    except Exception:
        return None


def list_from_openviking(ov_url: str, headers: dict, uri: str) -> list[dict]:
    """List directory contents from OpenViking."""
    try:
        resp = requests.get(
            f"{ov_url}/api/v1/fs/ls",
            headers=headers,
            params={"uri": uri},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code < 300:
            data = resp.json()
            if isinstance(data, dict) and "result" in data:
                return data["result"] or []
            return data if isinstance(data, list) else []
        return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Correlation logic
# ---------------------------------------------------------------------------


def correlate_resources(
    resources: list[dict],
    iac_maps: dict[str, Any],
    deploy_maps: dict[str, Any],
) -> dict[str, Any]:
    """Correlate AWS resources with code repos and CI/CD pipelines.

    Matching strategies:
    - Resource names/tags matching repo names
    - ARN patterns matching IaC resource declarations
    - Cluster/service names matching workflow deploy targets
    """
    relationships: list[dict[str, Any]] = []
    matched_resources = 0

    # Build lookup tables
    resource_by_name: dict[str, dict] = {}
    for r in resources:
        arn = r.get("arn", "")
        # Extract resource name from ARN (last segment)
        name = arn.split("/")[-1] if "/" in arn else arn.split(":")[-1]
        if name:
            resource_by_name[name.lower()] = r

    # Match IaC declarations to actual resources
    for repo, iac_data in iac_maps.items():
        for tf_resource in iac_data.get("resources", []):
            resource_name = tf_resource.get("name", "").lower()
            if resource_name in resource_by_name:
                relationships.append({
                    "type": "iac-creates",
                    "source": {"type": "repo", "id": repo},
                    "target": {"type": "resource", "id": resource_by_name[resource_name].get("arn", "")},
                    "via": "terraform",
                })
                matched_resources += 1

    # Match workflow deploy targets to resources
    for repo, deploy_data in deploy_maps.items():
        for workflow in deploy_data.get("workflows", []):
            # Check if workflow URI mentions any known resource names
            workflow_lower = workflow.lower()
            for name, resource in resource_by_name.items():
                if name in workflow_lower and len(name) > 5:  # Avoid short name matches
                    relationships.append({
                        "type": "deploys-to",
                        "source": {"type": "repo", "id": repo},
                        "target": {"type": "resource", "id": resource.get("arn", "")},
                        "via": workflow,
                    })
                    matched_resources += 1

    return {
        "correlated_at": datetime.now(timezone.utc).isoformat(),
        "total_resources": len(resources),
        "matched_resources": matched_resources,
        "relationships": relationships,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Correlate code with infrastructure")
    parser.add_argument("--accounts-file", default=settings.accounts_file)
    args = parser.parse_args()

    if not OV_KEY:
        log.error("No OpenViking API key.")
        sys.exit(1)

    headers = ov_headers(OV_KEY)

    # Read all infrastructure inventories from OpenViking
    all_resources: list[dict] = []
    infra_entries = list_from_openviking(OV_URL, headers, "viking://resources/infra/")
    for entry in infra_entries:
        name = entry.get("name", "")
        if entry.get("is_dir", False):
            # Read resources.json for this account
            resources_data = read_from_openviking(
                OV_URL, headers, f"viking://resources/infra/{name}/resources.json"
            )
            if resources_data and "resources" in resources_data:
                all_resources.extend(resources_data["resources"])
                log.info("Loaded %d resources from account %s", len(resources_data["resources"]), name)

    # Read IaC maps and deploy maps from repos
    iac_maps: dict[str, Any] = {}
    deploy_maps: dict[str, Any] = {}

    # List all repos in OpenViking
    repo_orgs = list_from_openviking(OV_URL, headers, "viking://resources/")
    for org_entry in repo_orgs:
        org_name = org_entry.get("name", "")
        if not org_entry.get("is_dir", False) or org_name in ("web", "infra", "deepwiki"):
            continue
        repos = list_from_openviking(OV_URL, headers, f"viking://resources/{org_name}/")
        for repo_entry in repos:
            repo_name = repo_entry.get("name", "")
            repo_path = f"{org_name}/{repo_name}"

            # Try reading infra-map and deploy-map
            infra_map = read_from_openviking(OV_URL, headers, f"viking://resources/{repo_path}/.infra-map.json")
            if infra_map:
                iac_maps[repo_path] = infra_map

            deploy_map = read_from_openviking(OV_URL, headers, f"viking://resources/{repo_path}/.deploy-map.json")
            if deploy_map:
                deploy_maps[repo_path] = deploy_map

    log.info(
        "Loaded: %d resources, %d repos with IaC, %d repos with workflows",
        len(all_resources),
        len(iac_maps),
        len(deploy_maps),
    )

    # Correlate
    graph = correlate_resources(all_resources, iac_maps, deploy_maps)

    # Upload relationship graph for each account
    for entry in infra_entries:
        name = entry.get("name", "")
        if entry.get("is_dir", False):
            target_uri = f"viking://resources/infra/{name}/relationships.json"
            uploaded = upload_to_openviking(
                OV_URL,
                headers,
                json.dumps(graph, indent=2),
                f"relationships-{name}.json",
                target_uri,
            )
            if uploaded:
                log.info("Uploaded relationship graph for account %s: %d relationships", name, len(graph["relationships"]))

    log.info(
        "Correlation complete: %d relationships found across %d resources",
        len(graph["relationships"]),
        graph["total_resources"],
    )


if __name__ == "__main__":
    main()
