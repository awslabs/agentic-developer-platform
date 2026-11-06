"""Installed Security worker dependencies; queue messages never supply authority."""

import os

import httpx
from fastapi import HTTPException
from l5_services.adapters.security.binding import SecurityBindingAdapter
from l5_services.adapters.security.code_review import CodeReviewAdapter
from l5_services.adapters.security.threat_model import ThreatModelAdapter
from l5_services.publication.installed import installed_publication_client
from l5_services.runtime_support import run_provider_session, setup_provider_session, verify_source_receipts
from l5_services.runtime_support import (
    ArchiveStore as _ArchiveStore,
    ScopedSources as _ScopedSources,
    SignedGateway as _SignedGateway,
    archive_store as _archive_store,
    gateway_transport as _gateway_transport,
    gateway_url as _gateway_url,
    provider_session as _provider_session,
    queue_publisher as _queue_publisher,
    required as _required,
    sessions as _sessions,
)
from l5_services.workers.execution import AuthorityRevoked, ExecutionService

__all__ = ["factory", "_ArchiveStore", "_SignedGateway", "_gateway_url", "_required"]


def factory():
    """Fail closed without the mounted Gateway and explicit worker resources."""
    if os.environ.get("L5_SERVICE_ID") not in (None, "security"):
        raise AuthorityRevoked("installed Security worker does not own this service")
    transport = _gateway_transport()
    sessions = _sessions()
    archive = _archive_store()
    publish = _queue_publisher()
    if (not all(callable(getattr(transport, name, None)) for name in ("post", "request", "stream"))
            or not callable(sessions)
            or not all(callable(getattr(archive, name, None)) for name in ("put_archive", "get_archive"))
            or not callable(publish) or not callable(_provider_session) or not callable(_ScopedSources)):
        raise RuntimeError("installed Security worker dependencies unavailable")

    async def admit_event(receipt):
        """Scheduled work is admitted only through the protected Gateway route.

        The installed worker never verifies its own schedule target: current
        configuration, binding, native registration, selected threat model,
        source rights and active owner consent are resolved by the Gateway.
        """
        if receipt.service_id != "security" or receipt.event_kind != "schedule":
            raise ValueError("Security scheduler received unrelated work")
        try:
            response = await transport.post("/internal/v1/l5/security/schedule-admissions", json={
                field: getattr(receipt, field)
                for field in ("tenant_id", "product_id", "project_id", "trigger_id", "event_id")
            })
        except httpx.HTTPError as error:
            raise HTTPException(503, detail={"code": "schedule_gateway_unavailable"}) from error
        if response.status_code != 200:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise HTTPException(response.status_code,
                                detail=detail if isinstance(detail, dict) else {"code": "schedule_admission_unavailable"})
        result = response.json()
        if (result.get("event_id") != receipt.event_id or result.get("state") != "admitted"
                or not result.get("run_id")):
            raise HTTPException(503, detail={"code": "schedule_admission_unverified"})
        return result

    async def authorize_setup(intent, claim):
        verified, identity, client = await setup_provider_session(
            intent, claim, transport, sessions, _provider_session, service_id="security", client_name="securityagent")

        async def scope_document():
            import base64
            from hashlib import sha256
            from l5_services.models.configuration import ConfigurationVersion

            async with sessions() as session:
                version = await session.get(ConfigurationVersion, (
                    intent.tenant_id, intent.configuration_id, intent.request["configuration_revision"]))
            if version and "threat_model" not in version.settings.get("capabilities", []):
                return None
            documents = [item for item in version.settings.get("source_refs", []) if item.get("record_type") == "document"] if version else []
            if len(documents) != 1:
                raise ValueError("security_architecture_document_required")
            authority = verified["authorization"]
            request = {"delegation_id": authority["delegation_id"],
                       "delegation_revision": authority["delegation_revision"],
                       "operation_id": intent.operation_id, "attempt_id": claim.work_id,
                       "lease_owner": claim.owner, "lease_fence": claim.fence,
                       "connection_ref": {**verified["connection_ref"], "record_type": "credential"}}
            response = await transport.post("/internal/v1/l5/security/setup-document", json=request)
            response.raise_for_status()
            result = response.json()
            snapshot = result["snapshot"]
            content = base64.b64decode(result["content_base64"], validate=True)
            if (snapshot["source_ref"] != documents[0] or snapshot["file_count"] != 1 or snapshot["exclusions"]
                    or len(content) > 6 * 1024 * 1024 or len(content) != snapshot["content"]["bytes"]
                    or sha256(content).hexdigest() != snapshot["content"]["sha256"]):
                raise AuthorityRevoked("setup architecture document changed")
            return {"snapshot": snapshot, "content": content}

        return verified, SecurityBindingAdapter(client, identity, scope_document=scope_document)

    async def authorize(run, claim, operation):
        if run.service_id != "security" or run.input.get("capability") not in {"code_review", "threat_model"}:
            raise AuthorityRevoked("installed Security worker does not own this capability")
        if operation == "materialize":
            await verify_source_receipts(run, claim, _ScopedSources(transport), "security")
            return None
        scoped = await run_provider_session(run, claim, transport, _ScopedSources(transport),
                                            _provider_session, service_id="security",
                                            allowed_clients={"securityagent", "s3"} if run.input["capability"] == "code_review"
                                            else {"securityagent"})
        if run.input["capability"] == "code_review":
            return scoped
        admission = run.admission
        space, model = admission.get("native_space_ref"), admission.get("native_model_ref")
        if (not isinstance(space, dict) or not isinstance(model, dict)
                or space.get("record_type") != "security_space" or model.get("record_type") != "threat_model"
                or not space.get("record_id") or not model.get("record_id")
                or any(model.get(field) != space.get(field) or not model.get(field)
                       for field in ("tenant_id", "project_id", "source_connection_id"))
                or (space.get("tenant_id"), space.get("project_id")) != (run.tenant_id, run.project_id)):
            raise AuthorityRevoked("persistent threat model not bound to Product scope")
        return {"client": scoped.client("securityagent"), "native_space_ref": space,
                "native_model_ref": model, "binding_id": run.binding_id,
                "tenant_id": run.tenant_id, "project_id": run.project_id, "claim": claim}


    sources = _ScopedSources(transport)
    return ExecutionService(sessions, sources, CodeReviewAdapter(archive),
                            publish, authorize, archive_store=archive, binding_authorize=authorize_setup,
                            security_threat_adapter=ThreatModelAdapter(sources), service_id="security",
                            admit_event=admit_event,
                            context_client=installed_publication_client(
                                service_kind="security", destination="product-context", transport=transport, sessions=sessions))
