#!/usr/bin/env python3
"""Correlate code repos with AWS infrastructure and CI/CD pipelines.

Reads resource inventory, IaC maps, and deploy maps from S3,
then produces a relationship-graph.json linking repos to resources to pipelines.

Usage:
  python correlate.py
  python correlate.py --accounts-file /config/accounts.txt
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("correlate")

from config import settings
from s3_store import S3ContentStore

REQUEST_TIMEOUT = settings.request_timeout


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
            # Check if workflow mentions any known resource names
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
    parser.parse_args()  # validates args; no action-specific flags currently

    if not settings.s3_bucket_name:
        log.error("No S3 bucket name configured.")
        sys.exit(1)

    store = S3ContentStore(
        bucket_name=settings.s3_bucket_name,
        prefix=settings.s3_content_prefix,
        region_name=settings.aws_region,
    )

    # Read all infrastructure inventories from S3
    all_resources: list[dict] = []
    infra_entries = store.list_prefix("infra/")
    for entry in infra_entries:
        name = entry.get("name", "")
        if entry.get("is_dir", False):
            # Read resources.json for this account
            resources_data = store.get_json(f"infra/{name}/resources.json")
            if resources_data and "resources" in resources_data:
                all_resources.extend(resources_data["resources"])
                log.info("Loaded %d resources from account %s", len(resources_data["resources"]), name)

    # Read IaC maps and deploy maps from repos
    iac_maps: dict[str, Any] = {}
    deploy_maps: dict[str, Any] = {}

    # List all repos in S3
    repo_orgs = store.list_prefix("repos/")
    for org_entry in repo_orgs:
        org_name = org_entry.get("name", "")
        if not org_entry.get("is_dir", False) or org_name in ("web", "infra", "deepwiki"):
            continue
        repos = store.list_prefix(f"repos/{org_name}/")
        for repo_entry in repos:
            repo_name = repo_entry.get("name", "")
            repo_path = f"{org_name}/{repo_name}"

            # Try reading infra-map and deploy-map
            infra_map = store.get_json(f"repos/{repo_path}/.infra-map.json")
            if infra_map:
                iac_maps[repo_path] = infra_map

            deploy_map = store.get_json(f"repos/{repo_path}/.deploy-map.json")
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
            uploaded = store.put_content(
                f"infra/{name}/relationships.json",
                json.dumps(graph, indent=2),
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
