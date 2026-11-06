"""Installed source-only Well-Architected execution with broker-owned authority."""

import json
import os

import httpx
from fastapi import HTTPException

from l5_services import runtime_support as resources
from l5_services.adapters.well_architected.binding import (
    ProfileBindingProvider,
    ProfileCleanupProvider,
    ProfileIdentitySelection,
    ProfileSelection,
)
from l5_services.adapters.well_architected.execution import build_execution
from l5_services.adapters.well_architected.runtime_callbacks import (
    PublicationClient,
    compare_source_statement,
)
from l5_services.models.configuration import ConfigurationVersion
from l5_services.publication.installed import installed_publication_client
from l5_services.workers.execution import AuthorityRevoked


def factory():
    if resources.required("L5_SERVICE_ID") != "well_architected":
        raise ValueError("installed Well-Architected service identity does not match")
    contracts = json.loads(os.environ.get("L5_WA_PEER_CONTRACTS") or "{}")
    PublicationClient.validate_contracts(contracts)
    transport = resources.gateway_transport()
    sessions = resources.sessions()
    sources = resources.ScopedSources(transport)
    archive = resources.archive_store("well_architected")
    publish = resources.queue_publisher("well_architected")

    async def admit_event(receipt):
        if receipt.service_id != "well_architected" or receipt.event_kind != "schedule":
            raise ValueError("Well-Architected scheduler received unrelated work")
        try:
            response = await transport.post("/internal/v1/l5/well-architected/schedule-admissions", json={
                field: getattr(receipt, field) for field in ("tenant_id", "product_id", "project_id", "trigger_id", "event_id")
            })
        except httpx.HTTPError as error:
            raise HTTPException(503, detail={"code": "schedule_gateway_unavailable"}) from error
        if response.status_code != 200:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise HTTPException(response.status_code, detail=detail if isinstance(detail, dict) else {"code": "schedule_admission_unavailable"})
        result = response.json()
        if result.get("event_id") != receipt.event_id or result.get("state") != "admitted" or not result.get("run_id"):
            raise HTTPException(503, detail={"code": "schedule_admission_unverified"})
        return result

    async def authorize(run, claim, operation):
        return await resources.run_provider_session(
            run,
            claim,
            transport,
            sources,
            resources.provider_session,
            service_id="well_architected",
            allowed_clients={"wellarchitected"},
        )

    async def authorize_setup(intent, claim):
        verified, identity, client = await resources.setup_provider_session(
            intent,
            claim,
            transport,
            sessions,
            resources.provider_session,
            service_id="well_architected",
            client_name="wellarchitected",
        )
        if intent.request.get("action") == "cleanup" and verified.get("authorization", {}).get("control_action") == "cleanup":
            try:
                owned = verified["owned_cleanup"]
                original = owned["profile_selection"]
                selection = ProfileIdentitySelection(
                    intent.tenant_id, intent.product_id, intent.project_id, identity["account_id"], identity["region"],
                    identity["role_arn"], original["execution_role_arn"], tuple(original["pillars"]),
                )
                provider = ProfileCleanupProvider(client, selection, verified["connection_ref"], owned)
            except (KeyError, TypeError, ValueError) as error:
                raise AuthorityRevoked("owned cleanup provenance unavailable") from error
            return verified, provider
        async with sessions() as session:
            version = await session.get(
                ConfigurationVersion,
                (
                    intent.tenant_id,
                    intent.configuration_id,
                    intent.request["configuration_revision"],
                ),
            )
            if version is None:
                raise AuthorityRevoked(
                    "Well-Architected setup configuration unavailable"
                )
            settings = version.settings.get("well_architected") or {}
        try:
            selection = ProfileSelection(
                intent.tenant_id,
                intent.product_id,
                intent.project_id,
                identity["account_id"],
                identity["region"],
                identity["role_arn"],
                settings["execution_role_ref"]["record_id"],
                tuple(settings["pillars"]),
                verified["source_role_qualification"],
            )
            selection.fingerprint()
        except (KeyError, TypeError, ValueError) as error:
            raise AuthorityRevoked(
                "source-only role qualification unavailable"
            ) from error
        return verified, ProfileBindingProvider(
            client, selection, verified["connection_ref"]
        )

    return build_execution(
        sessions,
        sources,
        archive,
        publish,
        authorize,
        authorize_setup,
        compare_source_statement,
        admit_event=admit_event,
        context_client=PublicationClient(
            sessions, transport, "product-context", contracts
        )
        if "product-context" in contracts
        else installed_publication_client(
            service_kind="well_architected",
            destination="product-context",
            transport=transport,
            sessions=sessions,
        ),
        insights_client=PublicationClient(
            sessions, transport, "engineering-insights", contracts
        )
        if "engineering-insights" in contracts
        else installed_publication_client(
            service_kind="well_architected",
            destination="engineering-insights",
            transport=transport,
            sessions=sessions,
        ),
    )
