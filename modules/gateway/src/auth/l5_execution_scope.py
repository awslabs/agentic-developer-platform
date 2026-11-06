"""Check retained owner consent against current configuration at every boundary.

NULL distinguishes pre-existing delegation mechanisms. Owner API grants always
retain scope, and their children must retain that same scope. Audit retention
cannot downgrade a scoped grant to legacy authority.
"""

from fastapi import HTTPException
from l5_services.models.configuration import Configuration, ConfigurationVersion

from src.auth.l5_transform_branch_scope import BRANCHES, OCCURRENCE, branch_selections, current_occurrence, occurrence_source
from src.l5_service_identity import service_for_worker
from src.shared.models.vault import UserCredential


async def current_execution_scope(db, grant, *, check_binding=False, binding_id=None, binding_generation=None):
    from src.auth.l5_devops_report_scope import current_report_recovery, is_report_recovery

    if is_report_recovery(grant):
        return await current_report_recovery(db, grant)
    scope = grant.execution_scope
    if scope is None:
        return True
    if not isinstance(scope, dict) or scope.get("purpose") is not None or not isinstance(scope.get("configuration_id"), str):
        return False
    service = service_for_worker(grant.service_id)
    occurrence = occurrence_source(grant)
    if OCCURRENCE in scope and (occurrence is None or not await current_occurrence(db, grant)):
        return False
    config = await db.get(Configuration, (grant.tenant_id, scope["configuration_id"]), populate_existing=True)
    if (
        config is None
        or service is None
        or any(
            scope.get(field) != getattr(grant, field)
            for field in ("tenant_id", "product_id", "project_id", "service_id", "credential_id", "created_by", "binding_generation", "visibility")
        )
        or (config.product_id, config.project_id, config.service_id, config.owner_id, config.current_revision, config.handoff_state)
        != (grant.product_id, grant.project_id, service, grant.created_by, scope.get("configuration_revision"), "linked")
        or not all(isinstance(scope.get(field), list) for field in ("operations", "capabilities", "sources"))
        or not set(grant.operations or []).issubset(scope["operations"])
        or not set(grant.capabilities or []).issubset(scope["capabilities"])
        or any(source not in scope["sources"] and source != occurrence for source in (grant.sources or []))
    ):
        return False
    version = await db.get(ConfigurationVersion, (grant.tenant_id, config.configuration_id, config.current_revision), populate_existing=True)
    credential = await db.get(UserCredential, grant.credential_id, populate_existing=True)
    if version is None or credential is None or version.hash != scope.get("configuration_hash"):
        return False
    settings = version.settings or {}
    if BRANCHES in scope:
        try:
            if service != "transform" or scope[BRANCHES] != branch_selections(settings, grant.tenant_id, grant.project_id):
                return False
        except HTTPException:
            return False
    selected = settings.get(service, {}) if service in {"devops", "transform"} else settings
    connection = selected.get("hosting_connection_ref" if service == "devops" else "connection_ref")
    if (
        not isinstance(connection, dict)
        or settings.get("enabled") is not True
        or selected.get("region") != scope.get("region")
        or (credential.scopes or {}).get("account_id") != scope.get("provider_account_id")
        or (credential.scopes or {}).get("role_arn") != scope.get("provider_role_arn")
        or credential.org_id != grant.tenant_id
        or credential.user_id != grant.created_by
        or connection.get("record_id") != grant.credential_id
        or connection.get("record_revision") != credential.aws_verified_version_id
        or scope["sources"] != [{**connection, "record_type": "credential"}, *(settings.get("source_refs") or [])]
        or sorted(set(settings.get("capabilities") or [])) != scope["capabilities"]
        or settings.get("visibility", "only_me") != scope.get("visibility")
    ):
        return False
    if service == "devops":
        from src.l5_devops_resources import require_workload_refs, retained_investigation_admission

        try:
            await require_workload_refs(
                db,
                tenant_id=grant.tenant_id,
                project_id=grant.project_id,
                owner_id=grant.created_by,
                settings=settings,
                binding_id=binding_id if check_binding else scope.get("binding_id"),
                admitted_at=await retained_investigation_admission(db, grant),
            )
        except HTTPException:
            return False
    if check_binding and (
        scope.get("binding_generation") != binding_generation or scope.get("binding_id") is not None and scope["binding_id"] != binding_id
    ):
        return False
    return True


async def require_execution_scope(db, grant, **target):
    if not await current_execution_scope(db, grant, **target):
        raise HTTPException(
            403,
            detail={
                "code": "delegation_scope_changed",
                "message": (
                    "The selected configuration or binding differs from the owner's consent. "
                    "Revoke the old grant, review delegation-scope, and authorize the current selection."
                ),
            },
        )
