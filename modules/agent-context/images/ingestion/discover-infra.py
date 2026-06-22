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
from iac_neptune_csv import IaCCSVOutput, generate_csv, get_infra_delete_queries
from iac_terraform_parser import parse_terraform
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
                accounts.append(
                    {
                        "account_id": parts[0],
                        "role_name": parts[1],
                        "regions": parts[2].split(","),
                    }
                )
            elif len(parts) == 2:
                accounts.append(
                    {
                        "account_id": parts[0],
                        "role_name": parts[1],
                        "regions": ["us-east-1"],
                    }
                )
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
# IaC → Neptune graph ingestion
# ---------------------------------------------------------------------------


def _load_infra_csv(
    neptune_url: str,
    region: str,
    csv_output: IaCCSVOutput,
    batch_size: int = 50,
) -> dict[str, Any]:
    """Load IaC CSV files into Neptune via openCypher UNWIND batch.

    This is the IaC-specific loader. It cannot reuse scip_neptune_loader's
    load_to_neptune() because that function hardcodes :Symbol label and
    code-specific columns (symbol_id, kind, module). IaC nodes use distinct
    labels (:InfraResource, :InfraModule, :InfraProvider) and different
    property columns (address, resource_type, provider, etc.).

    Reuses: _neptune_query() from scip_neptune_loader (SigV4 auth + HTTP).
    """
    import csv as csv_mod

    from scip_neptune_loader import _neptune_query

    # Test connectivity
    result = _neptune_query(neptune_url, region, "RETURN 1 AS alive")
    if "error" in result:
        log.error("Cannot connect to Neptune at %s: %s", neptune_url, result)
        return {"error": "connection_failed", "detail": str(result)}

    # --- Load vertices ---
    vertices: list[dict[str, str]] = []
    with open(csv_output.vertices_path) as f:
        reader = csv_mod.DictReader(f)
        for row in reader:
            vertices.append(row)

    vertices_loaded = 0
    v_errors = 0

    # Group vertices by label for type-specific MERGE
    # (Neptune openCypher requires label in MERGE to be a literal, not a parameter)
    label_groups: dict[str, list[dict[str, str]]] = {}
    for v in vertices:
        label = v["~label"]
        if label not in label_groups:
            label_groups[label] = []
        label_groups[label].append(v)

    for label, verts in label_groups.items():
        for i in range(0, len(verts), batch_size):
            batch = verts[i : i + batch_size]
            params = [
                {
                    "id": v["~id"],
                    "address": v["address:String"],
                    "resource_type": v["resource_type:String"],
                    "name": v["name:String"],
                    "provider": v["provider:String"],
                    "file": v["file:String"],
                    "line": int(v["line:Int"]),
                    "repo": v["repo:String"],
                    "module_path": v["module_path:String"],
                    "source": v["source:String"],
                    "version_constraint": v["version_constraint:String"],
                }
                for v in batch
            ]

            cypher = f"""
            UNWIND $nodes AS node
            MERGE (n:{label} {{`~id`: node.id}})
            SET n.address = node.address, n.resource_type = node.resource_type,
                n.name = node.name, n.provider = node.provider,
                n.file = node.file, n.line = node.line, n.repo = node.repo,
                n.module_path = node.module_path, n.source = node.source,
                n.version_constraint = node.version_constraint
            RETURN count(n) AS cnt
            """
            result = _neptune_query(neptune_url, region, cypher, {"nodes": params})
            if "error" in result:
                v_errors += len(batch)
                log.warning("Infra vertex batch error (%s): %s", label, str(result["error"])[:150])
            else:
                cnt = result.get("results", [{}])[0].get("cnt", 0)
                vertices_loaded += cnt

    # --- Load edges ---
    edges: list[dict[str, str]] = []
    with open(csv_output.edges_path) as f:
        reader = csv_mod.DictReader(f)
        for row in reader:
            edges.append(row)

    edges_loaded = 0
    e_errors = 0

    # Group edges by label for type-specific MERGE
    edge_label_groups: dict[str, list[dict[str, str]]] = {}
    for e in edges:
        elabel = e["~label"]
        if elabel not in edge_label_groups:
            edge_label_groups[elabel] = []
        edge_label_groups[elabel].append(e)

    for elabel, edge_batch_list in edge_label_groups.items():
        for i in range(0, len(edge_batch_list), batch_size):
            batch = edge_batch_list[i : i + batch_size]
            params = [
                {
                    "id": e["~id"],
                    "from_id": e["~from"],
                    "to_id": e["~to"],
                    "file": e["file:String"],
                    "line": int(e["line:Int"]),
                    "repo": e["repo:String"],
                }
                for e in batch
            ]

            cypher = f"""
            UNWIND $edges AS edge
            MATCH (a {{`~id`: edge.from_id}})
            MATCH (b {{`~id`: edge.to_id}})
            MERGE (a)-[r:{elabel} {{`~id`: edge.id}}]->(b)
            SET r.file = edge.file, r.line = edge.line, r.repo = edge.repo
            RETURN count(r) AS cnt
            """
            result = _neptune_query(neptune_url, region, cypher, {"edges": params})
            if "error" in result:
                e_errors += len(batch)
                log.warning("Infra edge batch error (%s): %s", elabel, str(result["error"])[:150])
            else:
                cnt = result.get("results", [{}])[0].get("cnt", 0)
                edges_loaded += cnt

    total_errors = v_errors + e_errors
    if total_errors == 0:
        log.info(
            "Infra Neptune load complete: %d vertices + %d edges (0 errors)",
            vertices_loaded,
            edges_loaded,
        )
    else:
        log.warning(
            "Infra Neptune load completed with errors: %d vertices + %d edges, %d errors",
            vertices_loaded,
            edges_loaded,
            total_errors,
        )

    return {
        "vertices_loaded": vertices_loaded,
        "edges_loaded": edges_loaded,
        "total_errors": total_errors,
        "success": total_errors == 0,
    }


def parse_and_load_iac(
    repo_path: str,
    repo: str,
    neptune_endpoint: str = "",
    region: str = "",
    output_dir: str = "/tmp/iac-csv",
) -> dict[str, Any]:
    """Parse Terraform from a repo and load the dependency graph into Neptune.

    This is the IaC counterpart to the SCIP pipeline:
      iac_terraform_parser → iac_neptune_csv → scip_neptune_loader → Neptune

    Args:
        repo_path: Absolute path to the cloned repository
        repo: Repository identifier (e.g., "aws-e/adp")
        neptune_endpoint: Neptune cluster endpoint (host:port). If empty, skips load.
        region: AWS region for Neptune SigV4 auth
        output_dir: Directory to write intermediate CSV files

    Returns:
        Dict with parse/load results

    Raises:
        ValueError: If .tf files exist but parsing produces 0 nodes (fail-loud)
    """
    # Step 1: Parse Terraform HCL into IaCGraph
    log.info("Parsing Terraform for %s at %s", repo, repo_path)
    graph = parse_terraform(repo_path, repo)

    if graph.node_count == 0:
        log.info("No Terraform resources found in %s — nothing to load", repo)
        return {"repo": repo, "status": "skipped", "reason": "no_tf_resources"}

    # Step 2: Generate Neptune CSV
    repo_output_dir = os.path.join(output_dir, repo.replace("/", "-"))
    csv_output = generate_csv(graph, repo_output_dir)

    log.info(
        "IaC graph for %s: %d nodes, %d edges → CSV at %s",
        repo,
        csv_output.vertex_count,
        csv_output.edge_count,
        repo_output_dir,
    )

    # Step 3: Load into Neptune (if endpoint configured)
    if not neptune_endpoint:
        neptune_endpoint = settings.neptune_endpoint
    if not region:
        region = settings.aws_region

    if not neptune_endpoint:
        log.warning("Neptune endpoint not configured — skipping graph load for %s", repo)
        return {
            "repo": repo,
            "status": "csv_only",
            "node_count": csv_output.vertex_count,
            "edge_count": csv_output.edge_count,
            "csv_dir": repo_output_dir,
        }

    # Import _neptune_query helper from SCIP loader (reuse SigV4 auth logic).
    # NOTE: load_to_neptune() is NOT reusable for IaC — it hardcodes :Symbol
    # label and code-specific columns (symbol_id, kind, module). IaC loading
    # uses its own UNWIND batch logic below with infra-specific columns/labels.
    from scip_neptune_loader import _neptune_query

    neptune_url = f"https://{neptune_endpoint}/opencypher"

    # Step 3a: Scoped delete — remove existing infra nodes for this repo
    # (per design doc §2.4 — idempotent re-ingestion)
    log.info("Clearing existing infra graph for %s", repo)
    delete_queries = get_infra_delete_queries(repo)
    for query, params in delete_queries:
        result = _neptune_query(neptune_url, region, query, params)
        if "error" in result:
            log.warning("Infra delete query failed: %s", str(result["error"])[:200])

    # Step 3b: Load IaC vertices into Neptune using batched UNWIND MERGE.
    # Each vertex gets its label from the ~label CSV column (InfraResource,
    # InfraModule, InfraProvider) — NOT hardcoded to :Symbol.
    load_result = _load_infra_csv(neptune_url, region, csv_output, batch_size=50)

    log.info(
        "Neptune load for %s: %d vertices, %d edges loaded (%d errors)",
        repo,
        load_result.get("vertices_loaded", 0),
        load_result.get("edges_loaded", 0),
        load_result.get("total_errors", 0),
    )

    # Step 3c: Post-load verification — assert data made it to Neptune
    verify_query = (
        "MATCH (n {repo: $repo}) "
        "WHERE n:InfraResource OR n:InfraModule OR n:InfraProvider "
        "RETURN count(n) AS cnt"
    )
    verify_result = _neptune_query(neptune_url, region, verify_query, {"repo": repo})
    if "error" not in verify_result:
        neptune_count = verify_result.get("results", [{}])[0].get("cnt", 0)
        if neptune_count == 0 and csv_output.vertex_count > 0:
            log.warning(
                "Post-load verification FAILED for %s: 0 nodes in Neptune "
                "despite %d in CSV — possible silent load failure",
                repo,
                csv_output.vertex_count,
            )

    return {
        "repo": repo,
        "status": "loaded",
        "node_count": csv_output.vertex_count,
        "edge_count": csv_output.edge_count,
        "vertices_loaded": load_result.get("vertices_loaded", 0),
        "edges_loaded": load_result.get("edges_loaded", 0),
        "errors": load_result.get("total_errors", 0),
    }


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
        accounts = [
            {"account_id": args.account, "role_name": args.role, "regions": args.regions.split(",")}
        ]
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
            log.info(
                "Uploaded %d resources for account %s", resources["total_resources"], account_id
            )
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

    # Step 4: Parse Terraform from cloned repos into Neptune graph
    # (IAC-1: #1647 — IaC dependency graph)
    iac_results: list[dict[str, Any]] = []
    clone_base = settings.clone_base
    if os.path.isdir(clone_base):
        for org_name in os.listdir(clone_base):
            org_path = os.path.join(clone_base, org_name)
            if not os.path.isdir(org_path):
                continue
            for repo_name in os.listdir(org_path):
                repo_dir = os.path.join(org_path, repo_name)
                if not os.path.isdir(repo_dir):
                    continue
                # Check if repo has .tf files before parsing
                has_tf = any(
                    f.endswith(".tf")
                    for root, _dirs, files in os.walk(repo_dir)
                    for f in files
                    if ".terraform" not in root
                )
                if not has_tf:
                    continue

                repo_id = f"{org_name}/{repo_name}"
                try:
                    result = parse_and_load_iac(repo_dir, repo_id)
                    iac_results.append(result)
                except Exception as e:
                    log.error("IaC parse failed for %s: %s", repo_id, str(e)[:300])
                    iac_results.append({"repo": repo_id, "status": "error", "error": str(e)[:300]})

    loaded_count = sum(1 for r in iac_results if r.get("status") == "loaded")
    log.info(
        "Infrastructure discovery complete: %d accounts, %d repos with IaC, "
        "%d repos with workflows, %d repos with Neptune graph loaded",
        len(accounts),
        len(iac_map),
        len(deploy_map),
        loaded_count,
    )


if __name__ == "__main__":
    main()
