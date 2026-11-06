"""Request-bound FinOps authority exported through the shared Gateway registry."""

from datetime import UTC, datetime
from functools import partial

from fastapi import HTTPException
from l5_services.adapters.finops.binding import binding_readback
from l5_services.api.bindings import view_binding
from l5_services.api.configuration import view
from l5_services.configuration_readiness import assess_readiness, effective_monitoring
from sqlalchemy import select

from src.auth.l5_delegation import issue_l5_work_delegation
from src.l5_service_identity import worker_id
from src.products.l5_admission_capture import GatewayAdmissionSourceClient
from src.shared.models.vault import CredentialServiceDelegation, UserCredential


class FinOpsAuthority:
    def bind_backend(self, backend):
        return partial(self, backend)

    async def __call__(self, backend, configuration, version, run_input, actor):
        if (
            actor is not backend.actor
            or actor.account_type != "human"
            or configuration.owner_id != actor.user_id
            or configuration.tenant_id != actor.org_id
            or configuration.handoff_state != "linked"
            or configuration.service_id != "finops"
            or run_input.get("service_id") != "finops"
            or run_input.get("capability") != "cost_inquiry"
            or run_input.get("configuration_revision") != version.revision
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        store, setup = await backend.binding_authority(configuration.product_id, configuration.project_id, "finops")
        binding = await store.binding(run_input.get("binding_id"))
        record = binding_readback(binding)
        settings = version.settings
        identity = {field: setup[field] for field in ("account_id", "role_arn", "region")}
        if (
            record is None
            or record.get("configuration_revision") != version.revision
            or binding.configuration_id != configuration.configuration_id
            or binding.scope != view(configuration, version)["scope"]
            or binding.connection_ref != settings.get("connection_ref")
            or binding.provider_identity != identity
        ):
            raise HTTPException(409, detail={"code": "native_unqualified"})
        readiness, gaps, _ = assess_readiness(
            settings, configuration.configuration_id, [view_binding(binding)], delegation_active=True, service_id="finops"
        )
        if readiness != "ready" or gaps:
            raise HTTPException(409, detail={"code": "configuration_disabled"})
        sources = run_input.get("sources") or []
        allowed = await backend.source_rights(configuration.project_id) if sources else set()
        if any(
            source not in settings.get("source_refs", [])
            or source.get("tenant_id") != actor.org_id
            or source.get("project_id") != configuration.project_id
            or (source.get("source_connection_id"), source.get("record_id")) not in allowed
            for source in sources
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        triggers = [
            trigger
            for trigger in settings.get("triggers", [])
            if trigger.get("kind") == "manual"
            and trigger.get("enabled") is True
            and trigger.get("capability") == "cost_inquiry"
            and trigger.get("binding_id") == binding.binding_id
            and trigger.get("source_refs", []) == sources
        ]
        if len(triggers) != 1:
            raise HTTPException(409, detail={"code": "trigger_disabled"})
        grants = (
            await backend.db.scalars(
                select(CredentialServiceDelegation)
                .where(
                    CredentialServiceDelegation.tenant_id == actor.org_id,
                    CredentialServiceDelegation.product_id == configuration.product_id,
                    CredentialServiceDelegation.project_id == configuration.project_id,
                    CredentialServiceDelegation.created_by == actor.user_id,
                    CredentialServiceDelegation.credential_id == binding.connection_ref["record_id"],
                    CredentialServiceDelegation.service_id == worker_id("finops"),
                    CredentialServiceDelegation.parent_delegation_id.is_(None),
                    CredentialServiceDelegation.revoked_at.is_(None),
                    CredentialServiceDelegation.expires_at > datetime.now(UTC),
                )
                .limit(2)
            )
        ).all()
        required = {"product:read", "credential:session"} | ({"source:pin", "source:read"} if sources else set())
        credential_ref = {**binding.connection_ref, "record_type": "credential"}
        if (
            len(grants) != 1
            or grants[0].binding_generation != binding.generation
            or "cost_inquiry" not in grants[0].capabilities
            or not required.issubset(grants[0].operations)
            or any(source not in grants[0].sources for source in [credential_ref, *sources])
            or not backend.admission_key
            or backend.admission_operation not in {"submit", "retry"}
        ):
            raise HTTPException(403, detail={"code": "delegation_denied"})
        credential = await backend.db.get(UserCredential, binding.connection_ref["record_id"])
        grant = await issue_l5_work_delegation(
            backend.db,
            actor,
            grants[0],
            credential,
            binding,
            binding.connection_ref,
            sources,
            "cost_inquiry",
            settings["limits"]["deadline_seconds"],
            backend.admission_operation,
            backend.admission_key,
            previous_id=backend.previous_delegation_id,
        )
        verified = {
            "scope": binding.scope,
            "service_id": "finops",
            "actor_id": actor.user_id,
            "actor_type": "human",
            "configuration_id": configuration.configuration_id,
            "configuration_revision": version.revision,
            "configuration_hash": version.hash,
            "configuration_enabled": True,
            "profile_ref": settings["profile_ref"],
            "connection_ref": binding.connection_ref,
            "binding_connection_ref": binding.connection_ref,
            "binding_id": binding.binding_id,
            "binding_generation": binding.generation,
            "binding_state": binding.state,
            "delegation_id": grant.id,
            "delegation_version": grant.revision,
            "delegation_active": True,
            "native_qualified": True,
            "native_agent_ref": binding.native_resource_ref,
            "native_agent_display_name": record["native_agent_display_name"],
            "provider_identity": identity,
            "billing_scope": record["billing_scope"],
            "trigger": triggers[0],
            "adapter_revision": "finops-native-v1",
            "limits": settings["limits"],
            "visibility": settings["visibility"],
            "classification": settings["classification"],
            "causal_origin": "manual",
            "exclusions": settings.get("exclusions", []),
            "monitoring_policy": effective_monitoring(settings),
            "max_bytes": 6 * 1024 * 1024,
            "max_files": 1000,
        }
        source_client = (
            GatewayAdmissionSourceClient(backend.db, actor, grant.id, verified, backend.admission_operation, backend.admission_key)
            if sources
            else None
        )
        return verified, source_client


def gateway_backends():
    return {"l5_finops_authority": FinOpsAuthority()}
