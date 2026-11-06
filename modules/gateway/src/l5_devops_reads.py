"""Read selected DevOps sources and native integrations without creating anything."""

import asyncio
import re
from contextlib import asynccontextmanager

from fastapi import HTTPException
from l5_services.adapters.devops.integrations import NativeIntegrationAssociation, github_repository_configuration
from sqlalchemy import select

from src.admin.connections.github_client import GitHubAppClient
from src.admin.connections.service import _get_github_app_credentials
from src.products.models import ProductSourceBinding
from src.products.source_routes import connection_access


def unavailable(code, message, status=409):
    return HTTPException(status, detail={"code": code, "message": message})


@asynccontextmanager
async def github_source(backend, project_id, reference):
    if (
        not isinstance(reference, dict)
        or reference.get("tenant_id") != backend.actor.org_id
        or reference.get("project_id") != project_id
        or reference.get("record_type") not in {"repository", "repository_commit"}
        or not re.fullmatch(r"[a-f0-9]{40}", str(reference.get("record_revision", "")))
        or not re.fullmatch(r"[1-9][0-9]{0,19}", str(reference.get("record_id", "")))
    ):
        raise unavailable("devops_source_selection_invalid", "Select a verified GitHub repository at an immutable commit.")
    rows = (
        await backend.db.scalars(
            select(ProductSourceBinding)
            .where(
                ProductSourceBinding.tenant_id == backend.actor.org_id,
                ProductSourceBinding.project_id == project_id,
                ProductSourceBinding.kind == "repository",
                ProductSourceBinding.provider == "github",
                ProductSourceBinding.native_id == reference["record_id"],
                ProductSourceBinding.installation_id == reference["source_connection_id"],
                ProductSourceBinding.unlinked_at.is_(None),
                ProductSourceBinding.status != "revoked",
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(rows) != 1:
        raise unavailable("devops_source_access_denied", "Reconnect the selected Product repository and verify its current revision.")
    binding = rows[0]
    if ((binding.observed or {}).get("source_revision") or (binding.desired or {}).get("source_revision")) != reference["record_revision"]:
        raise unavailable("devops_source_changed", "Refresh the Product repository selection and review the new commit before retrying.")
    mapping = await connection_access(backend.db, backend.actor, binding)
    if mapping is None or not re.fullmatch(r"[1-9][0-9]*", str(mapping.installation_id)):
        raise unavailable("devops_source_access_denied", "Restore the owner's access to the selected GitHub installation.")
    app_id, key = _get_github_app_credentials()
    if not app_id or not key:
        raise unavailable("github_app_unavailable", "Configure the Product GitHub App before checking this source.", 503)
    client = GitHubAppClient(app_id, key)
    selected = (binding.revision, binding.generation, binding.status, binding.desired, binding.observed)
    try:
        async with asyncio.timeout(30):
            repository = await client.get_installation_repository(int(mapping.installation_id), int(binding.native_id))
            owner = repository.get("owner") or {}
            if (
                str(repository.get("id")) != binding.native_id
                or str(owner.get("id")) != binding.namespace
                or mapping.provider_scope_id not in (str(owner.get("id")), owner.get("login"))
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(repository.get("full_name", "")))
            ):
                raise unavailable("devops_source_access_denied", "The selected repository no longer belongs to the authorized installation.")

            async def read(path):
                # All callers supply fixed REST suffixes, never a returned URL.
                result = await client.installation_request(int(mapping.installation_id), "GET", f"/repos/{repository['full_name']}/{path}")
                return result

            commit = await read("commits/" + reference["record_revision"])
            if commit.get("sha") != reference["record_revision"]:
                raise unavailable("devops_source_changed", "GitHub did not return the selected commit.")
            yield repository, read, binding
            await backend.db.refresh(binding)
            if (
                selected != (binding.revision, binding.generation, binding.status, binding.desired, binding.observed)
                or await connection_access(backend.db, backend.actor, binding) is None
            ):
                raise unavailable("devops_source_changed", "Source access changed during validation. Review the current selection.")
    finally:
        await client.aclose()


def pages(method, field, **kwargs):
    rows, token, seen = [], None, set()
    for _ in range(10):
        response = method(**kwargs, **({"nextToken": token} if token else {}))
        values = response.get(field)
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(v, dict) for v in values):
            raise unavailable("devops_native_readback_incomplete", "Native discovery returned an incomplete page.")
        rows.extend(values)
        token = response.get("nextToken")
        if not token:
            return rows
        if not isinstance(token, str) or token in seen:
            break
        seen.add(token)
    raise unavailable("devops_native_readback_incomplete", "Native discovery exceeded its bounded page limit. Narrow the selection.")


def association_readback(native, space_id, reference, repository):
    expected = github_repository_configuration(reference, repository)
    space = native.get_agent_space(agentSpaceId=space_id).get("agentSpace") or {}
    if space.get("agentSpaceId") != space_id:
        raise unavailable("devops_space_changed", "Verify the selected DevOps space again.")
    services = pages(native.list_services, "services", filterServiceType="github", maxResults=100)
    matches = [service for service in services if NativeIntegrationAssociation._service_matches(service, expected)]
    if len(matches) != 1 or not matches[0].get("serviceId"):
        raise unavailable("devops_registration_unverified", "Register the selected GitHub installation in the DevOps UI, then check again.")
    service_id = matches[0]["serviceId"]
    service = native.get_service(serviceId=service_id).get("service") or {}
    if service.get("serviceId") != service_id or not NativeIntegrationAssociation._service_matches(service, expected):
        raise unavailable("devops_registration_unverified", "The native GitHub registration does not match the selected repository owner.")
    associations = pages(native.list_associations, "associations", agentSpaceId=space_id, maxResults=100)
    matches = [item for item in associations if item.get("serviceId") == service_id and item.get("configuration") == {"github": expected}]
    if len(matches) != 1 or not matches[0].get("associationId"):
        raise unavailable("devops_association_unverified", "Associate the selected repository with this DevOps space, then check again.")
    association = native.get_association(agentSpaceId=space_id, associationId=matches[0]["associationId"]).get("association") or {}
    if (
        association.get("agentSpaceId") != space_id
        or association.get("associationId") != matches[0]["associationId"]
        or association.get("serviceId") != service_id
        or association.get("configuration") != {"github": expected}
        or association.get("status") != "valid"
    ):
        raise unavailable("devops_association_unverified", "The repository association is not valid in the selected DevOps space.")
    return service, association
