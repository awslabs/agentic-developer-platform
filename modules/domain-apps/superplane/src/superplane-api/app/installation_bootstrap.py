"""Fresh-install tenancy initialization from a verified ADP administrator.

No legacy org, grant or identity is migrated or overwritten. The token is a
short-lived secret reference in a one-shot Job, never a user-supplied identity.
"""

import asyncio
import json
import os
import uuid

import httpx
from sqlalchemy import select, text
from superplane_auth.policy import TRUSTED_VALIDATION_PATH, Permission

from app.auth import build_domain_policy, verify_access_token
from app.database import async_session_factory
from app.models.cluster import Cluster
from app.models.organization import Organization
from app.models.organization_grant import (
    ORGANIZATION_ADMINISTER,
    OrganizationGrantRecord,
)
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord


async def bootstrap(config, token, *, membership_reader=None):
    policy = build_domain_policy()
    if policy is None:
        raise ValueError("strict ADP policy required")
    principal = policy.admit(
        verify_access_token(token), validation_path=TRUSTED_VALIDATION_PATH
    )
    if principal.org_id != config["adp_org_id"] or principal.account_type != "human":
        raise ValueError(
            "bootstrap caller does not match the selected ADP organization"
        )

    async def current_admin():
        # Revalidate expiry and current membership after any database lock wait.
        current = policy.admit(
            verify_access_token(token), validation_path=TRUSTED_VALIDATION_PATH
        )
        if current != principal:
            raise ValueError("bootstrap principal changed")
        if membership_reader is None:
            async with httpx.AsyncClient(
                timeout=20, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.get(
                    config["origin"] + "/api/auth/workspaces",
                    headers={"Authorization": "Bearer " + token},
                )
                response.raise_for_status()
                membership = response.json()
        else:
            membership = await membership_reader()
        selected = [
            item
            for item in membership.get("items", [])
            if item.get("org_id") == principal.org_id
            and item.get("is_current") is True
            and item.get("role") == "org_admin"
        ]
        if len(selected) != 1:
            raise ValueError(
                "current ADP organization-administrator membership required"
            )
        return selected[0]

    selected = await current_admin()
    control_plane_only = config.get("control_plane_only", False)
    if not isinstance(control_plane_only, bool):
        raise ValueError("control_plane_only must be a boolean")
    org_id = uuid.UUID(config["org_id"])
    workspace_keys = (
        "workspace_id", "cluster_id", "workspace_cluster",
        "workspace_namespace", "workspace_cluster_arn",
    )
    if control_plane_only and any(key in config for key in workspace_keys):
        raise ValueError("control-plane bootstrap cannot include workspace bindings")
    if not control_plane_only and not all(config.get(key) for key in workspace_keys):
        raise ValueError("workspace bootstrap requires a complete workspace binding")
    workspace_id = None if control_plane_only else uuid.UUID(config["workspace_id"])
    cluster_id = None if control_plane_only else uuid.UUID(config["cluster_id"])
    async with async_session_factory() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:binding, 0))"),
            {"binding": "superplane-bootstrap:" + principal.org_id},
        )
        existing = await session.scalar(
            select(Organization)
            .where(Organization.adp_org_id == principal.org_id)
            .with_for_update()
        )
        by_id = await session.get(Organization, org_id)
        if (existing is not None and existing.id != org_id) or (
            by_id is not None and by_id.adp_org_id != principal.org_id
        ):
            raise ValueError("refusing legacy adoption or organization rebinding")
        if existing is None:
            session.add(
                Organization(
                    id=org_id, adp_org_id=principal.org_id, name=selected["name"]
                )
            )
            await session.flush()
        organization_grant = await session.scalar(
            select(OrganizationGrantRecord).where(
                OrganizationGrantRecord.org_id == org_id,
                OrganizationGrantRecord.principal == principal.subject,
            ).with_for_update()
        )
        if organization_grant is not None:
            if (
                organization_grant.revoked_at is not None
                or organization_grant.principal_type != "human"
                or ORGANIZATION_ADMINISTER not in organization_grant.permission_values()
            ):
                raise ValueError("existing organization grant is revoked or restricted")
        else:
            session.add(OrganizationGrantRecord(
                org_id=org_id,
                principal=principal.subject,
                principal_type="human",
                permissions=ORGANIZATION_ADMINISTER,
                granted_by=principal.subject,
            ))
        if control_plane_only:
            await session.flush()
            await current_admin()
            return {
                "adp_org_id": principal.org_id,
                "org_id": str(org_id),
                "workspace_id": None,
                "actor": principal.subject,
                "organization_grant": ORGANIZATION_ADMINISTER,
                "legacy_migration": False,
            }
        workspace = await session.get(Workspace, workspace_id, with_for_update=True)
        cluster = await session.get(Cluster, cluster_id, with_for_update=True)
        if workspace is not None and (
            workspace.org_id != org_id
            or workspace.cluster_id != cluster_id
            or workspace.namespace_name != config["workspace_namespace"]
            or workspace.status in {"Teardown", "Deleted"}
        ):
            raise ValueError("workspace binding already differs")
        if cluster is not None and (
            cluster.org_id != org_id
            or cluster.eks_cluster_arn != config["workspace_cluster_arn"]
        ):
            raise ValueError("cluster binding already differs")
        if cluster is None:
            session.add(
                Cluster(
                    id=cluster_id,
                    org_id=org_id,
                    name=config["workspace_cluster"],
                    cloud_provider="aws",
                    cluster_type="eks",
                    eks_cluster_arn=config["workspace_cluster_arn"],
                    status="Pending",
                )
            )
            await session.flush()
        if workspace is None:
            session.add(
                Workspace(
                    id=workspace_id,
                    org_id=org_id,
                    name=config["workspace_cluster"],
                    isolation_mode="dedicated",
                    cluster_id=cluster_id,
                    namespace_name=config["workspace_namespace"],
                    status="Ready",
                )
            )
            await session.flush()
        grant = await session.scalar(
            select(WorkspaceGrantRecord)
            .where(
                WorkspaceGrantRecord.workspace_id == workspace_id,
                WorkspaceGrantRecord.principal == principal.subject,
            )
            .with_for_update()
        )
        if grant is not None:
            if (
                grant.revoked_at is not None
                or grant.org_id != org_id
                or grant.principal_type != "human"
            ):
                raise ValueError("existing grant is revoked or bound differently")
        else:
            session.add(
                WorkspaceGrantRecord(
                    workspace_id=workspace_id,
                    org_id=org_id,
                    principal=principal.subject,
                    principal_type="human",
                    permissions=Permission.ADMINISTER.value,
                )
            )
        await session.flush()
        await current_admin()
    return {
        "adp_org_id": principal.org_id,
        "org_id": str(org_id),
        "workspace_id": str(workspace_id),
        "actor": principal.subject,
        "legacy_migration": False,
    }


def main():
    try:
        config = json.loads(os.environ["SUPERPLANE_BOOTSTRAP_CONFIG"])
        result = asyncio.run(
            bootstrap(config, os.environ["SUPERPLANE_BOOTSTRAP_TOKEN"])
        )
        print(json.dumps(result))
        return 0
    except Exception:
        print(
            json.dumps(
                {
                    "error": "Fresh-install bootstrap refused; verify ADP administrator, token policy and explicit organization/workspace binding. Existing identities are not migrated."
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
