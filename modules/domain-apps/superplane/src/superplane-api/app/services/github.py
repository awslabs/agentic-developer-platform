"""GitHub Actions workflow_dispatch trigger service."""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


async def trigger_workflow(
    workflow_file: str, ref: str = "main", inputs: dict | None = None
) -> bool:
    """Trigger a GitHub Actions workflow via workflow_dispatch.

    Args:
        workflow_file: Name of the workflow file (e.g. "bootstrap-workspace.yml").
        ref: Git ref to run the workflow on.
        inputs: Key-value inputs to pass to the workflow.

    Returns:
        True if the dispatch was accepted (HTTP 204), False otherwise.
    """
    if not settings.github_token:
        logger.warning(
            "GITHUB_TOKEN not configured — skipping workflow trigger for %s",
            workflow_file,
        )
        return False

    url = f"{GITHUB_API_BASE}/repos/{settings.github_repo}/actions/workflows/{workflow_file}/dispatches"
    headers = {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = {"ref": ref, "inputs": inputs or {}}

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=body, headers=headers, timeout=30)

    if response.status_code == 204:
        logger.info("Triggered workflow %s with inputs %s", workflow_file, inputs)
        return True

    logger.error(
        "Failed to trigger workflow %s: %s %s",
        workflow_file,
        response.status_code,
        response.text,
    )
    return False


async def trigger_bootstrap(
    workspace_id: str,
    workspace_name: str,
    org_id: str,
    isolation_mode: str,
    account: str = "",
) -> bool:
    """Trigger the bootstrap-workspace.yml workflow.

    For research workspaces, the account parameter is passed to provision
    the workspace in the specified AWS account with permissive agent IAM policies.
    """
    inputs = {
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "org_id": org_id,
        "isolation_mode": isolation_mode,
    }
    if account:
        inputs["aws_account_id"] = account
    return await trigger_workflow("bootstrap-workspace.yml", inputs=inputs)


async def trigger_teardown(workspace_id: str, workspace_name: str, org_id: str) -> bool:
    """Trigger the teardown-workspace.yml workflow."""
    return await trigger_workflow(
        "teardown-workspace.yml",
        inputs={
            "workspace_id": workspace_id,
            "workspace_name": workspace_name,
            "org_id": org_id,
        },
    )
