"""Resolve fixed Identity references for legacy connectors, never from arguments."""

from fastapi import HTTPException

PROVIDERS = {"github": "github", "jira": "atlassian", "slack": "slack"}


def legacy_identity(connector, settings):
    if connector not in PROVIDERS or settings is None or settings.legacy_identity_workload is None:
        raise HTTPException(403, detail={"error": "adapter_authority_required"})
    workload = settings.legacy_identity_workload
    return workload, workload.removesuffix("-oauth-user") + "-" + PROVIDERS[connector] + "-user"
