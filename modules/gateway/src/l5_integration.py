"""Host L5 configuration and signed incident ingress using scoped authority."""

import hashlib
import json
import os
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from inspect import getattr_static
from types import SimpleNamespace
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from l5_services.adapters.devops import federation
from l5_services.adapters.devops.spaces import SpaceAdapter, SpaceSelection
from l5_services.adapters.well_architected.binding import ProfileBindingProvider, ProfileIdentityError, ProfileSelection
from l5_services.admission import reconcile_and_dispatch, retry_and_dispatch, submit_and_dispatch
from l5_services.api.bindings import BindingStore, view_binding
from l5_services.api.configuration import ConfigurationStore, view
from l5_services.api.router import create_router
from l5_services.configuration_population import authorized_connection_refs, configuration_options
from l5_services.configuration_readiness import assess_readiness, effective_monitoring, execution_input_gaps, transform_capability_readiness
from l5_services.devops.access import authorize_browser_launch
from l5_services.devops.incident_events import receive_incident_event
from l5_services.devops.lifecycle import SpaceLifecycle
from l5_services.devops.prevention import PreventionPolicy, require_native_budget
from l5_services.devops.release_prerequisites import release_eligible, selected_repository
from l5_services.devops.trigger_policy import require_new_work
from l5_services.devops_configuration import integration_readiness
from l5_services.dispatcher import dispatch_work
from l5_services.events.schedules import finops_admissions_enabled
from l5_services.events.sources import admit_source_receipt
from l5_services.findings import ResultStore
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.devops_space import DevopsSpaceIntent
from l5_services.models.publication import Publication
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from l5_services.models.setup import SetupIntent, SetupWork
from l5_services.profile_handoff import link_configuration
from l5_services.publication.client import WellArchitectedConsumerClient
from l5_services.run_access import RunStore
from l5_services.security_registration import current_model, current_registration
from l5_services.setup_authority import setup_authorization
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from src.auth.aws_connection_authority import connection_material, require_assumed_identity, verified_connection_evidence
from src.auth.dependencies import get_current_user
from src.auth.l5_cleanup_scope import SUPPORTED_SERVICES as CLEANUP_SERVICES
from src.auth.l5_cleanup_scope import owned_cleanup_proof, require_cleanup_scope
from src.auth.l5_delegation import issue_l5_incident_delegation, issue_l5_work_delegation
from src.auth.l5_devops_setup_scope import is_devops_resource_scope, is_devops_setup_scope, require_devops_resource_scope, require_devops_setup_scope
from src.auth.l5_execution_scope import require_execution_scope
from src.auth.l5_owner_delegation import create_owner_delegation_router
from src.auth.native_launch_revocation import require_native_launch_session
from src.budget.utils import get_period_start_end
from src.internal.sts_assume_service import assume_role
from src.internal.tools_l5 import available_l5_catalogue, dispatch_l5, l5_http_request
from src.l5_queue_registry import publisher as service_publisher
from src.l5_readiness import project_readiness
from src.l5_service_identity import worker_id
from src.l5_setup_continuations import create_router as create_setup_continuation_router
from src.l5_source_preflight import create_router as create_source_preflight_router
from src.l5_wa_provider_selection import create_provider_selection_router
from src.l5_wa_qualification import create_router as create_wa_qualification_router
from src.l5_wa_reuse_authorization import create_router as create_wa_reuse_router
from src.product_onboarding.handoffs import link_l5_configuration
from src.product_onboarding.source_discovery import authorized_connections
from src.products import routes as product_routes
from src.products.l5_admission_capture import GatewayAdmissionSourceClient
from src.products.models import ProductProjectBinding
from src.products.security_evidence_routes import create_router as create_security_evidence_router
from src.products.source_capture_metadata import REPOSITORY_MAX_BYTES, REPOSITORY_MAX_FILES
from src.products.source_routes import permitted_source_refs, service_visible_bindings
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.identity.product_scope import current_l5_parent
from src.shared.models.budget import BudgetConfig, BudgetUsage
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import PeriodType
from src.shared.services.secrets_manager import SecretsManagerHelper


def _record_refs(value):
    if isinstance(value, dict):
        if {"tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from _record_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _record_refs(child)


def _verified_aws_connection_ref(credential, tenant_id, project_id):
    if credential.org_id != tenant_id or credential.service != "aws" or credential.credential_type != "aws_role":
        raise HTTPException(404, detail={"code": "not_found"})
    verified_connection_evidence(credential)
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "source_connection_id": credential.id,
        "record_type": "connection",
        "record_id": credential.id,
        "record_revision": credential.aws_verified_version_id,
    }


def _project_connection(source, credential, allowed, tenant_id, project_id, *, source_free=False):
    connection_id = source.get("connection_ref", {}).get("id")
    if (
        not isinstance(connection_id, str)
        or (source_free and source.get("provider") != "aws")
        or (not source_free and not any(connection_id == selected for selected, _ in allowed))
    ):
        return None
    if isinstance(credential, UserCredential) and credential.service == "aws":
        if credential.id != connection_id or credential.credential_type != "aws_role" or not credential.aws_verified_version_id:
            return None
        try:
            verified_connection_evidence(credential)
        except HTTPException:
            return None
        return {
            **source,
            "record_ref": {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "source_connection_id": connection_id,
                "record_type": "connection",
                "record_id": connection_id,
                "record_revision": credential.aws_verified_version_id,
            },
        }
    reference = source.get("record_ref")
    if reference is not None and (
        reference.get("tenant_id") != tenant_id or reference.get("project_id") != project_id or reference.get("source_connection_id") != connection_id
    ):
        return None
    return source


async def _product_call(operation):
    try:
        return await operation
    except HTTPException as error:
        if error.status_code < 500:
            raise
    except httpx.HTTPStatusError as error:
        if error.response.status_code < 500:
            raise
    except httpx.TransportError:
        pass
    raise HTTPException(503, detail={"code": "dependency_unavailable", "retryable": True}) from None


class GatewayConfigurationBackend:
    def __init__(
        self,
        db: AsyncSession,
        actor: TokenContext,
        *,
        publish=None,
        delegation_id=None,
        devops_integration_observer=None,
        verification_reader=None,
        transform_qualification_reader=None,
        transform_work_authority=None,
        transform_artifact_store=None,
        release_authority=None,
        prevention_provider=None,
        wa_qualify=None,
        wa_profile_client=None,
        wa_authority=None,
        wa_consumer_resolver=None,
        finops_authority=None,
        finops_answer_reader=None,
        security_source_client=None,
    ):
        self.db = db
        self.actor = actor
        self.delegation_id = delegation_id
        self.grant = None
        self.store = ConfigurationStore(db, actor.org_id, actor.user_id, activation_authority=self._activate_devops)
        self.sessions = async_sessionmaker(db.bind, expire_on_commit=False)
        self.publish = publish
        self.devops_integration_observer = devops_integration_observer
        self.verification_reader = verification_reader
        self.transform_qualification_reader = transform_qualification_reader
        self.transform_work_authority = transform_work_authority
        self.transform_artifact_store = transform_artifact_store
        self.release_authority = release_authority
        self.prevention_provider = prevention_provider
        self.wa_qualify = wa_qualify
        self.wa_profile_client = wa_profile_client
        self.wa_authority = wa_authority
        self.wa_consumer_resolver = wa_consumer_resolver
        self.finops_authority = finops_authority
        self.finops_answer_reader = finops_answer_reader
        self.security_source_client = security_source_client
        self.admission_key = None
        self.admission_operation = None
        self.previous_delegation_id = None
        for name in (
            "devops_integration_observer",
            "verification_reader",
            "transform_qualification_reader",
            "transform_work_authority",
            "transform_artifact_store",
            "release_authority",
            "prevention_provider",
            "wa_qualify",
            "wa_profile_client",
            "wa_authority",
            "wa_consumer_resolver",
            "finops_authority",
        ):
            dependency = getattr(self, name)
            if getattr_static(dependency, "bind_backend", None) is not None:
                bind = getattr(dependency, "bind_backend")
                if callable(bind):
                    setattr(self, name, bind(self))

    async def _prevention_target(self, product_id, project_id, binding_id):
        configuration, version = await self.current(product_id, project_id, "devops")
        if (
            self.actor.account_type != "human"
            or self.actor.auth_source != "jwt"
            or self.actor.token_use != "access"
            or configuration.owner_id != self.actor.user_id
            or configuration.handoff_state != "linked"
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        binding = await BindingStore(self.db, self.store.tenant_id, product_id, project_id, "devops").binding(binding_id)
        devops = version.settings.get("devops") or {}
        connection = devops.get("hosting_connection_ref")
        scope = view(configuration, version)["scope"]
        if (
            binding.state != "ready"
            or binding.configuration_id != configuration.configuration_id
            or binding.revision != version.revision
            or binding.scope != scope
            or not isinstance(connection, dict)
            or binding.connection_ref != connection
            or (binding.native_resource_ref or {}).get("record_type") != "agent_space"
            or (binding.native_resource_ref or {}).get("source_connection_id") != connection.get("source_connection_id")
            or not (binding.native_resource_ref or {}).get("record_id")
        ):
            raise HTTPException(409, detail={"code": "prevention_binding_unverified"})
        receipts = (
            await self.db.scalars(
                select(DevopsSpaceIntent)
                .where(
                    DevopsSpaceIntent.tenant_id == self.store.tenant_id,
                    DevopsSpaceIntent.configuration_revision == version.revision,
                    DevopsSpaceIntent.selection["binding_id"].as_string() == binding_id,
                )
                .limit(2)
            )
        ).all()
        if len(receipts) != 1:
            raise HTTPException(409, detail={"code": "prevention_space_unverified"})
        receipt = receipts[0]
        selection = SpaceSelection(**receipt.selection)
        identity = binding.provider_identity or {}
        if (
            receipt.state not in {"succeeded", "ready"}
            or receipt.native_space_id != binding.native_resource_ref["record_id"]
            or (selection.tenant_id, selection.product_id, selection.project_id, selection.binding_id, selection.account_id, selection.region)
            != (self.store.tenant_id, product_id, project_id, binding_id, identity.get("account_id"), identity.get("region"))
            or devops.get("region") != selection.region
        ):
            raise HTTPException(409, detail={"code": "prevention_space_unverified"})
        credential = await self.db.get(UserCredential, connection.get("record_id"))
        if (
            credential is None
            or credential.user_id != configuration.owner_id
            or credential.org_id != self.store.tenant_id
            or credential.id != connection.get("source_connection_id")
            or credential.service != "aws"
            or credential.credential_type != "aws_role"
            or credential.aws_verified_version_id != connection.get("record_revision")
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        try:
            verified_connection_evidence(credential)
        except HTTPException:
            raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from None
        if identity.get("role_arn") != (credential.scopes or {}).get("role_arn") or identity.get("account_id") != (credential.scopes or {}).get(
            "account_id"
        ):
            raise HTTPException(409, detail={"code": "prevention_binding_unverified"})
        grant = await self.db.scalar(
            select(CredentialServiceDelegation).where(
                CredentialServiceDelegation.tenant_id == self.store.tenant_id,
                CredentialServiceDelegation.product_id == product_id,
                CredentialServiceDelegation.project_id == project_id,
                CredentialServiceDelegation.credential_id == credential.id,
                CredentialServiceDelegation.created_by == configuration.owner_id,
                CredentialServiceDelegation.service_id == worker_id("devops"),
                CredentialServiceDelegation.parent_delegation_id.is_(None),
                CredentialServiceDelegation.operation_id.is_(None),
                (
                    CredentialServiceDelegation.execution_scope["purpose"].as_string().is_(None)
                    | (CredentialServiceDelegation.execution_scope["purpose"].as_string() != "devops_native_ui")
                ),
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > datetime.now(UTC),
            )
        )
        if (
            grant is None
            or "credential:session" not in grant.operations
            or "prevention" not in grant.capabilities
            or grant.binding_generation != binding.generation
            or {**connection, "record_type": "credential"} not in grant.sources
        ):
            raise HTTPException(403, detail={"code": "delegation_denied"})
        await require_execution_scope(self.db, grant, check_binding=True, binding_id=binding.binding_id, binding_generation=binding.generation)
        return version, receipt, selection, credential

    async def _devops_connection_session(self, credential, selection, action_id, *, purpose="prevention"):
        try:
            helper = SecretsManagerHelper(region_name=get_settings().aws_region)
            current_version = await run_in_threadpool(helper.current_version_id, credential.secret_arn)
            if current_version != credential.aws_verified_version_id:
                raise ValueError("credential version changed")
            secret, served_version = await run_in_threadpool(helper.get_secret_at_version, credential.secret_arn, current_version)
            if served_version != current_version:
                raise ValueError("credential version changed")
            role, external_id, account = connection_material(json.loads(secret), credential.scopes or {}, credential)
            if account != selection.account_id:
                raise ValueError("selected account changed")
            assumed = await run_in_threadpool(
                assume_role,
                role_arn=role,
                external_id=external_id,
                session_duration_seconds=900,
                default_region=selection.region,
                user_id=self.actor.user_id,
                agent_id=f"l5-{purpose}",
                task_id=action_id,
                label=credential.label,
            )
            require_assumed_identity(assumed, role)
            if await run_in_threadpool(helper.current_version_id, credential.secret_arn) != current_version:
                raise ValueError("credential version changed")
            return boto3.Session(
                aws_access_key_id=assumed.access_key_id,
                aws_secret_access_key=assumed.secret_access_key,
                aws_session_token=assumed.session_token,
                region_name=selection.region,
            )
        except Exception:
            raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from None

    async def _prevention_client(self, credential, selection, action_id, *, update=False):
        if self.prevention_provider is not None:
            return await self.prevention_provider(credential, selection, action_id, update=update)
        raise HTTPException(
            503,
            detail={
                "code": "prevention_provider_unavailable",
                "message": "Install the scoped DevOps prevention provider before reading or changing the native schedule.",
            },
        )

    async def prevention_status(self, product_id, project_id, binding_id):
        version, receipt, selection, credential = await self._prevention_target(product_id, project_id, binding_id)
        client = await self._prevention_client(credential, selection, receipt.operation_id)
        lifecycle = SpaceLifecycle(self.sessions, SpaceAdapter(client, account_id=selection.account_id, region=selection.region))
        status = await lifecycle.prevention_status(selection, receipt.operation_id, version.settings)
        return {**status, "configuration_revision": version.revision, "binding_id": binding_id}

    async def configure_prevention(self, product_id, project_id, binding_id, revision, action, key, if_match):
        version, receipt, selection, credential = await self._prevention_target(product_id, project_id, binding_id)
        if version.revision != revision or if_match != f'"{version.hash}"':
            raise HTTPException(412, detail={"code": "stale_revision"})
        try:
            selected = PreventionPolicy.from_configuration(version.settings)
            policy = replace(selected, enabled=action != "pause")
        except ValueError:
            raise HTTPException(409, detail={"code": "prevention_policy_unavailable"}) from None
        budget = None
        if policy.enabled:
            policy_ref = (version.settings.get("limits") or {}).get("policy_ref") or {}
            selected_budget = await self.db.get(BudgetConfig, policy_ref.get("record_id")) if isinstance(policy_ref, dict) else None
            if (
                selected_budget is not None
                and selected_budget.org_id == self.store.tenant_id
                and (selected_budget.entity_type, selected_budget.entity_id) == ("org", self.store.tenant_id)
            ):
                budget = {
                    "enforcement_mode": selected_budget.enforcement_mode,
                    "tenant_id": self.store.tenant_id,
                    "project_id": project_id,
                    "policy_ref": policy_ref,
                }
            try:
                require_native_budget(version.settings, budget, receipt.selection)
            except ValueError:
                raise HTTPException(409, detail={"code": "native_budget_unsupported"}) from None
        client = await self._prevention_client(credential, selection, key, update=True)
        lifecycle = SpaceLifecycle(self.sessions, SpaceAdapter(client, account_id=selection.account_id, region=selection.region))
        try:
            await lifecycle.configure_prevention(selection, receipt.operation_id, policy, action_id=key, settings=version.settings, budget=budget)
            status = await lifecycle.prevention_status(selection, receipt.operation_id, version.settings)
            return {**status, "configuration_revision": version.revision, "binding_id": binding_id}
        except (ConnectionError, TimeoutError):
            raise HTTPException(503, detail={"code": "prevention_readback_unavailable"}) from None
        except ValueError:
            raise HTTPException(409, detail={"code": "prevention_outcome_unverified"}) from None

    async def binding_store(self, product_id, project_id, service_id):
        configuration, _ = await self.current(product_id, project_id, service_id)
        if service_id == "transform" and self.actor.account_type == "human" and configuration.owner_id != self.actor.user_id:
            raise HTTPException(404, detail={"code": "not_found"})
        # Read saved metadata under current Product/source/connection authority.
        # Setup, reconciliation and admission separately require execution consent.
        return BindingStore(self.db, self.store.tenant_id, product_id, project_id, service_id)

    async def list_bindings(self, product_id, project_id, service_id):
        return await (await self.binding_store(product_id, project_id, service_id)).list_bindings()

    async def get_binding(self, product_id, project_id, service_id, binding_id):
        return await (await self.binding_store(product_id, project_id, service_id)).binding(binding_id)

    async def get_binding_operation(self, product_id, project_id, service_id, operation_id):
        intent = await self.db.get(SetupIntent, (self.store.tenant_id, operation_id), populate_existing=True)
        if (
            service_id in CLEANUP_SERVICES
            and intent is not None
            and intent.request.get("action") == "cleanup"
            and intent.authorization.get("control_action") == "cleanup"
        ):
            from l5_services.api.bindings import _intent_hash

            if (intent.product_id, intent.project_id, intent.service_id, intent.actor_id) != (
                product_id,
                project_id,
                service_id,
                self.actor.user_id,
            ) or intent.intent_hash != _intent_hash(intent):
                raise HTTPException(404, detail={"code": "not_found"})
            store, binding, proof = await self.owned_cleanup_metadata(product_id, project_id, service_id, intent.request.get("binding_id"))
            operation = await store.operation(operation_id)
            if (
                operation.action != "cleanup"
                or operation.binding_id != binding.binding_id
                or intent.configuration_id != binding.configuration_id
                or intent.owned_resource_receipt != proof["creation_receipt"]
            ):
                raise HTTPException(404, detail={"code": "not_found"})
            return operation
        return await (await self.binding_store(product_id, project_id, service_id)).operation(operation_id)

    async def owned_cleanup_metadata(self, product_id, project_id, service_id, binding_id):
        from src.auth.l5_owner_delegation import _owner

        configuration, version = await _owner(self, product_id, project_id, service_id, active=False)
        store = BindingStore(self.db, self.store.tenant_id, product_id, project_id, service_id)
        binding = await store.binding(binding_id)
        proof = await owned_cleanup_proof(self.db, binding, configuration, version, allow_cleaned=True)
        return store, binding, proof

    async def get_cleanup_status(self, product_id, project_id, service_id, binding_id):
        _, binding, proof = await self.owned_cleanup_metadata(product_id, project_id, service_id, binding_id)
        # This returns ownership/cleanup metadata only, never source readiness,
        # native report content or a new authorization to use the connection.
        return {
            "binding_id": binding.binding_id,
            "revision": binding.revision,
            "generation": binding.generation,
            "state": binding.state,
            "ownership": binding.ownership,
            "owned_resource": proof,
        }

    async def cleanup_binding_authority(self, product_id, project_id, service_id, binding_id, *, prior=None):
        from src.auth.l5_owner_delegation import _owner

        configuration, version = await _owner(self, product_id, project_id, service_id)
        grant = await self.db.scalar(
            select(CredentialServiceDelegation)
            .where(
                CredentialServiceDelegation.tenant_id == self.store.tenant_id,
                CredentialServiceDelegation.product_id == product_id,
                CredentialServiceDelegation.project_id == project_id,
                CredentialServiceDelegation.service_id == worker_id(service_id),
                CredentialServiceDelegation.created_by == self.actor.user_id,
                CredentialServiceDelegation.parent_delegation_id.is_(None),
                CredentialServiceDelegation.operation_id.is_(None),
                (
                    CredentialServiceDelegation.execution_scope["purpose"].as_string().is_(None)
                    | (CredentialServiceDelegation.execution_scope["purpose"].as_string() != "devops_native_ui")
                ),
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > datetime.now(UTC),
            )
            .execution_options(populate_existing=True)
        )
        if grant is None:
            raise HTTPException(403, detail={"code": "cleanup_consent_required"})
        if prior is not None and (
            prior.request.get("action") != "cleanup"
            or prior.authorization.get("delegation_id") != grant.id
            or prior.authorization.get("delegation_revision") != grant.revision
        ):
            raise HTTPException(409, detail={"code": "idempotency_conflict"})
        await require_cleanup_scope(
            self.db, grant, binding_id=binding_id, admitted=prior is not None, binding_generation=prior.binding_generation if prior else None
        )
        scope = grant.execution_scope
        return BindingStore(self.db, self.store.tenant_id, product_id, project_id, service_id), {
            "configuration_id": configuration.configuration_id,
            "configuration_revision": version.revision,
            "scope": view(configuration, version)["scope"],
            "connection_ref": version.settings["connection_ref"],
            "account_id": scope["provider_account_id"],
            "region": scope["region"],
            "role_arn": scope["provider_role_arn"],
            "owned_cleanup": scope["owned_resource"],
            "authorization": {
                "actor_id": self.actor.user_id,
                "delegation_id": grant.id,
                "delegation_revision": grant.revision,
                "authorized_at": datetime.now(UTC).isoformat(),
                "control_action": "cleanup",
            },
        }

    async def binding_authority(self, product_id, project_id, service_id, *, setup_action=None):
        from src.l5_service_identity import worker_id

        configuration, version = await self.current(product_id, project_id, service_id)
        if (
            service_id not in {"security", "finops", "well_architected", "transform", "devops"}
            or self.actor.account_type != "human"
            or configuration.owner_id != self.actor.user_id
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        selected = version.settings.get("transform", {}) if service_id == "transform" else version.settings
        if service_id == "devops":
            selected = version.settings.get("devops", {})
        connection = selected.get("hosting_connection_ref" if service_id == "devops" else "connection_ref")
        region = selected.get("region")
        if not isinstance(connection, dict) or not isinstance(region, str) or not region:
            raise HTTPException(409, detail={"code": "connection_not_selected"})
        credential = await self.db.get(UserCredential, connection.get("record_id"))
        if (
            credential is None
            or credential.id != connection.get("source_connection_id")
            or credential.org_id != self.store.tenant_id
            or credential.service != "aws"
            or credential.credential_type != "aws_role"
            or credential.aws_verified_version_id != connection.get("record_revision")
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        allowed = await self.source_rights(project_id)
        available = [
            projected
            for item, candidate in await _product_call(authorized_connections(self.db, self.actor))
            if (
                projected := _project_connection(
                    item, candidate, allowed, self.store.tenant_id, project_id, source_free=service_id in {"finops", "devops"}
                )
            )
        ]
        if connection not in [item.get("record_ref") for item in available]:
            raise HTTPException(404, detail={"code": "not_found"})
        try:
            verified_connection_evidence(credential)
        except HTTPException as exc:
            raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from exc
        selected_account = (credential.scopes or {}).get("account_id")
        selected_role = (credential.scopes or {}).get("role_arn")
        if (
            not isinstance(selected_account, str)
            or re.fullmatch(r"[0-9]{12}", selected_account) is None
            or not isinstance(selected_role, str)
            or re.fullmatch(rf"arn:aws:iam::{selected_account}:role/.+", selected_role) is None
        ):
            raise HTTPException(503, detail={"code": "selected_connection_unavailable"})
        grant = await self.db.scalar(
            select(CredentialServiceDelegation).where(
                CredentialServiceDelegation.tenant_id == self.store.tenant_id,
                CredentialServiceDelegation.product_id == product_id,
                CredentialServiceDelegation.project_id == project_id,
                CredentialServiceDelegation.credential_id == credential.id,
                CredentialServiceDelegation.created_by == self.actor.user_id,
                CredentialServiceDelegation.service_id == worker_id(service_id),
                CredentialServiceDelegation.parent_delegation_id.is_(None),
                CredentialServiceDelegation.operation_id.is_(None),
                (
                    CredentialServiceDelegation.execution_scope["purpose"].as_string().is_(None)
                    | (CredentialServiceDelegation.execution_scope["purpose"].as_string() != "devops_native_ui")
                ),
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > datetime.now(UTC),
            )
        )
        session_ref = {**connection, "record_type": "credential"}
        if grant is None or "credential:session" not in grant.operations or session_ref not in grant.sources:
            raise HTTPException(403, detail={"code": "delegation_denied"})
        setup_only = is_devops_setup_scope(grant)
        resource_only = is_devops_resource_scope(grant)
        if resource_only:
            if setup_action != "check":
                raise HTTPException(403, detail={"code": "resource_verification_consent_required"})
            await require_devops_resource_scope(self.db, grant)
        elif setup_only:
            await require_devops_setup_scope(self.db, grant, action=setup_action)
        else:
            await require_execution_scope(self.db, grant)
        if (
            service_id == "devops"
            and not (setup_only or resource_only)
            and not (
                set(grant.capabilities or []) & set(version.settings.get("capabilities") or []) & {"investigation", "release_readiness", "prevention"}
                and "product:read" in grant.operations
            )
        ):
            raise HTTPException(403, detail={"code": "delegation_denied"})
        store = BindingStore(self.db, self.store.tenant_id, product_id, project_id, service_id)
        verified = {
            "configuration_id": configuration.configuration_id,
            "configuration_revision": version.revision,
            "scope": view(configuration, version)["scope"],
            "connection_ref": connection,
            "executor_binding_ref": selected.get("executor_binding_ref"),
            "definition_ref": selected.get("definition_ref"),
            "account_id": selected_account,
            "region": region,
            "role_arn": selected_role,
            "authorization": {
                "actor_id": self.actor.user_id,
                "delegation_id": grant.id,
                "delegation_revision": grant.revision,
                "authorized_at": datetime.now(UTC).isoformat(),
            },
        }
        if service_id == "well_architected":
            from src.l5_wa_provider_selection import verify_provider_references

            settings = version.settings.get("well_architected") or {}
            role = settings.get("execution_role_ref") or {}
            source_store = settings.get("source_store_ref") or {}
            if (
                role.get("record_type") != "role"
                or source_store.get("record_type") != "artifact_store"
                or not version.settings.get("source_refs")
                or self.wa_qualify is None
            ):
                raise HTTPException(409, detail={"code": "provider_unqualified"})
            await verify_provider_references(self, project_id, version.settings)
            qualification = await self.wa_qualify(credential, version.settings)
            try:
                ProfileSelection(
                    self.store.tenant_id,
                    product_id,
                    project_id,
                    selected_account,
                    region,
                    selected_role,
                    role["record_id"],
                    tuple(settings["pillars"]),
                    qualification,
                ).fingerprint()
            except (ValueError, KeyError, TypeError):
                raise HTTPException(409, detail={"code": "provider_unqualified"}) from None
            verified["source_role_qualification"] = qualification
        if service_id == "finops":
            verified["finops"] = version.settings.get("finops") or {}
        if service_id == "devops":
            verified["authorization"]["binding_generation"] = grant.binding_generation
            if resource_only:
                verified["authorization"].update(purpose="devops_resource_verification", control_action="devops_resource_verification")
                verified["resource_selection"] = grant.execution_scope["resource_selection"]
            if setup_only:
                verified["authorization"].update(purpose="devops_setup", control_action="devops_setup")
                if grant.execution_scope.get("setup_action") == "link":
                    verified["authorization"].update(
                        setup_action="link",
                        native_resource_ref=grant.execution_scope["native_resource_ref"],
                        connection_ref=connection,
                        provider_identity={"account_id": selected_account, "region": region, "role_arn": selected_role},
                        configuration_hash=version.hash,
                    )
        return store, verified

    async def _activate_devops(self, configuration_id, settings):
        from src.l5_devops_activation import authorize_activation

        await authorize_activation(self, configuration_id, settings)

    async def _single_devops_setup(self, grant, key):
        # Serialize initial create/link intents, including distinct grants.
        # A lost response must reuse the original operation and its authority.
        await self.db.scalar(
            select(Configuration)
            .where(
                Configuration.tenant_id == grant.tenant_id,
                Configuration.configuration_id == grant.execution_scope["configuration_id"],
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        existing = (
            await self.db.scalars(
                select(SetupIntent).where(
                    SetupIntent.tenant_id == grant.tenant_id,
                    SetupIntent.configuration_id == grant.execution_scope["configuration_id"],
                    SetupIntent.service_id == "devops",
                    SetupIntent.request["action"].as_string().in_(("create", "link")),
                )
            )
        ).all()
        authorities = [await setup_authorization(self.db, item) for item in existing]
        if any(item.idempotency_key != key or authority.get("delegation_id") != grant.id for item, authority in zip(existing, authorities)):
            raise HTTPException(
                409,
                detail={
                    "code": "setup_already_retained",
                    "operation_id": existing[0].operation_id,
                    "message": (
                        "Read the existing binding operation and reconcile it with its original consent; "
                        "another create or link request cannot replace its retained setup identity."
                    ),
                },
            )

    async def dispatch_binding(self, operation):
        work = await self.db.scalar(
            select(SetupWork)
            .where(
                SetupWork.tenant_id == self.store.tenant_id,
                SetupWork.operation_id == operation.operation_id,
                SetupWork.completed_at.is_(None),
            )
            .order_by(SetupWork.generation.desc())
        )
        if self.publish is not None and work is not None:
            await dispatch_work(self.sessions, self.store.tenant_id, work.work_id, self.publish, work_model=SetupWork)

    async def start_binding(self, product_id, project_id, service_id, request, key, if_match=None):
        if service_id == "devops" and request.get("action") == "sync_knowledge":
            from src.l5_devops_knowledge import admit

            return await admit(self, product_id, project_id, request, key, if_match)
        if request.get("action") == "cleanup" and service_id in CLEANUP_SERVICES:
            previous = await self.db.scalar(
                select(SetupIntent)
                .where(
                    SetupIntent.tenant_id == self.store.tenant_id,
                    SetupIntent.product_id == product_id,
                    SetupIntent.project_id == project_id,
                    SetupIntent.service_id == service_id,
                    SetupIntent.actor_id == self.actor.user_id,
                    SetupIntent.idempotency_key == key,
                )
                .execution_options(populate_existing=True)
            )
            store, verified = await self.cleanup_binding_authority(product_id, project_id, service_id, request.get("binding_id"), prior=previous)
            operation = await store.control(request, key, if_match, verified)
            await self.dispatch_binding(operation)
            return operation
        store, verified = await self.binding_authority(product_id, project_id, service_id, setup_action=request.get("action"))
        grant = await self.db.get(CredentialServiceDelegation, verified["authorization"]["delegation_id"], populate_existing=True)
        if is_devops_setup_scope(grant):
            await self._single_devops_setup(grant, key)
        generation = 1
        if request.get("action") in {"check", "update", "suspend", "resume", "unlink", "cleanup"}:
            binding = await store.binding(request.get("binding_id"))
            generation = binding.generation + 1
            previous = await self.db.scalar(
                select(SetupIntent).where(
                    SetupIntent.tenant_id == self.store.tenant_id,
                    SetupIntent.product_id == product_id,
                    SetupIntent.project_id == project_id,
                    SetupIntent.service_id == service_id,
                    SetupIntent.actor_id == self.actor.user_id,
                    SetupIntent.idempotency_key == key,
                )
            )
            if previous is not None:
                generation = previous.binding_generation
        if is_devops_resource_scope(grant):
            if (
                request.get("action") != "check"
                or request.get("binding_id") != grant.execution_scope["binding_id"]
                or request.get("resource_selection") != grant.execution_scope["resource_selection"]
                or generation != grant.binding_generation
            ):
                raise HTTPException(403, detail={"code": "resource_verification_consent_required"})
            await require_devops_resource_scope(self.db, grant)
        elif is_devops_setup_scope(grant):
            if request.get("binding_id") is not None:
                raise HTTPException(422, detail={"code": "setup_scope_invalid"})
            if request.get("native_resource_ref") != grant.execution_scope.get("native_resource_ref"):
                raise HTTPException(403, detail={"code": "setup_consent_required"})
            await require_devops_setup_scope(self.db, grant, action=request.get("action"), binding_generation=generation)
        else:
            await require_execution_scope(self.db, grant, check_binding=True, binding_id=request.get("binding_id"), binding_generation=generation)
        if service_id == "devops" and request.get("action") not in {"create", "link", "check"}:
            raise HTTPException(409, detail={"code": "setup_action_unavailable"})
        if request.get("action") in {"check", "update", "suspend", "resume", "unlink", "cleanup"}:
            operation = await store.control(request, key, if_match, verified)
        else:
            operation = await store.start(request, key, verified)
        await self.dispatch_binding(operation)
        return operation

    async def reconcile_binding(self, product_id, project_id, service_id, operation_id, reason, key):
        intent = await self.db.get(SetupIntent, (self.store.tenant_id, operation_id), populate_existing=True)
        if intent is not None and intent.request.get("action") == "sync_knowledge":
            from src.l5_knowledge_resume import resume

            return await resume(self, product_id, project_id, service_id, operation_id, reason, key)
        if intent is not None and intent.request.get("action") == "cleanup" and service_id in CLEANUP_SERVICES:
            if (intent.product_id, intent.project_id, intent.service_id, intent.actor_id) != (product_id, project_id, service_id, self.actor.user_id):
                raise HTTPException(404, detail={"code": "not_found"})
            store, verified = await self.cleanup_binding_authority(product_id, project_id, service_id, intent.request.get("binding_id"), prior=intent)
            operation = await store.request_reconcile(operation_id, reason, key, verified)
            await self.dispatch_binding(operation)
            return operation
        store, verified = await self.binding_authority(
            product_id, project_id, service_id, setup_action=intent.request.get("action") if intent else None
        )
        grant = await self.db.get(CredentialServiceDelegation, verified["authorization"]["delegation_id"], populate_existing=True)
        intent = await self.db.get(SetupIntent, (self.store.tenant_id, operation_id), populate_existing=True)
        if intent is None:
            raise HTTPException(404, detail={"code": "not_found"})
        if is_devops_setup_scope(grant):
            await require_devops_setup_scope(self.db, grant, action=intent.request.get("action"), binding_generation=intent.binding_generation)
            authority = await setup_authorization(self.db, intent)
            if authority.get("delegation_id") != grant.id or authority.get("purpose") != "devops_setup":
                raise HTTPException(403, detail={"code": "setup_consent_required"})
        else:
            await require_execution_scope(
                self.db, grant, check_binding=True, binding_id=intent.request.get("binding_id"), binding_generation=intent.binding_generation
            )
        operation = await store.request_reconcile(operation_id, reason, key, verified)
        await self.dispatch_binding(operation)
        return operation

    async def launch_browser(self, product_id, project_id, binding_id, configuration_revision, request):
        from src.l5_observation_launch import start
        from src.orchestration.l5_observation_scope import ObservationError

        observation = await start(self, request, product_id, project_id, binding_id, configuration_revision)
        try:
            result = await self._launch_browser(product_id, project_id, binding_id, configuration_revision, request, observation)
        except ObservationError as error:
            receipt = await observation.finish(status=error.status, error_code=error.code) if observation is not None else None
            raise HTTPException(
                error.status, detail={"code": error.code}, headers={"X-ADP-Observation-Receipt": receipt} if receipt else None
            ) from None
        except HTTPException as error:
            if observation is not None:
                code = error.detail.get("code") if isinstance(error.detail, dict) else None
                receipt = await observation.finish(status=error.status_code, error_code=code)
                error.headers = {**(error.headers or {}), "X-ADP-Observation-Receipt": receipt}
            raise
        except Exception:
            if observation is not None:
                await observation.finish(status=503)
            raise
        if observation is not None:
            result = {**result, "observation_receipt_id": await observation.finish(status=200)}
        return result

    async def _browser_launch_authority(self, product_id, project_id, binding_id, configuration_revision, request, *, consume_rate_limit=True):
        if (
            self.actor.account_type != "human"
            or self.actor.auth_source != "jwt"
            or self.actor.token_use != "access"
            or self.actor.expires_at is None
            or self.actor.expires_at.astimezone(UTC) <= datetime.now(UTC)
        ):
            raise HTTPException(401, detail={"code": "session_required"})
        await require_native_launch_session(request.headers.get("Authorization", ""))
        if consume_rate_limit:
            try:
                limit = await request.app.state.ratelimit_service.consume_tool_rate_limit(
                    self.actor,
                    "l5-devops-browser-launch",
                )
            except Exception:
                raise HTTPException(503, detail={"code": "rate_limit_unavailable"}) from None
            if not limit.allowed:
                raise HTTPException(429, detail={"code": "rate_limited"}, headers={"Retry-After": str(max(1, limit.retry_after_seconds or 1))})
        origin = request.headers.get("origin")
        allowed_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(",")
        if origin is not None and origin not in [value.strip() for value in allowed_origins]:
            raise HTTPException(403, detail={"code": "origin_denied"})
        member, product, project = await _product_call(
            product_routes.scoped_product(
                self.db,
                self.actor,
                product_id,
                project_id,
            )
        )
        from src.products.models import Product
        from src.shared.identity.product_scope import owner_scope, product_member

        member = await product_member(self.db, user_id=self.actor.user_id, org_id=self.actor.org_id)
        if member is None or not await self.db.scalar(select(Product.id).where(Product.id == product_id, owner_scope(member, self.actor.org_id))):
            raise HTTPException(404, detail={"code": "not_found"})
        await self.db.refresh(product)
        await self.db.refresh(project)
        if product.archived_at is not None or project.archived_at is not None:
            raise HTTPException(404, detail={"code": "not_found"})
        configuration, version = await self.store.current(product_id, project_id, "devops")
        if configuration is None or version is None:
            raise HTTPException(404, detail={"code": "not_found"})
        await self.db.refresh(configuration)
        await self.db.refresh(version)
        owner = await product_member(self.db, user_id=configuration.owner_id, org_id=self.actor.org_id)
        if (
            owner is None
            or product.owner_user_id != configuration.owner_id
            or not await self.db.scalar(select(Product.id).where(Product.id == product_id, owner_scope(owner, self.actor.org_id)))
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        relationship = await self.relationship(product_id, project_id)
        if relationship is not None:
            await self.db.refresh(relationship)
        if relationship is None or relationship.valid_to is not None:
            raise HTTPException(404, detail={"code": "not_found"})
        from src.auth.l5_devops_native_scope import current_grant

        binding = await BindingStore(self.db, self.actor.org_id, product_id, project_id, "devops").binding(binding_id)
        native_grant = await current_grant(self, configuration, version, binding)
        native_policy = native_grant.execution_scope["native_ui_policy"] if native_grant is not None else None
        receipts = (
            await self.db.scalars(
                select(DevopsSpaceIntent)
                .where(
                    DevopsSpaceIntent.tenant_id == self.actor.org_id,
                    (DevopsSpaceIntent.operation_id == native_policy["setup_operation_id"])
                    if native_grant is not None
                    else DevopsSpaceIntent.configuration_revision == configuration_revision,
                    DevopsSpaceIntent.selection["binding_id"].as_string() == binding_id,
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        if len(receipts) != 1:
            raise HTTPException(404, detail={"code": "not_found"})
        receipt = receipts[0]
        selection = authorize_browser_launch(
            self.actor,
            member,
            configuration,
            version,
            receipt,
            product_id=product_id,
            project_id=project_id,
            binding_id=binding_id,
            configuration_revision=configuration_revision,
            relationship_revision=relationship.relationship_revision,
            action_allowed=product.owner_user_id == member.id or version.settings.get("visibility") == "product_team",
            owner_native_policy=native_policy,
        )
        devops = {**(version.settings.get("devops") or {}), **(native_policy or {})}
        connection = devops.get("hosting_connection_ref")
        identity = binding.provider_identity or {}
        native = binding.native_resource_ref or {}
        if (
            binding.state != "ready"
            or binding.configuration_id != configuration.configuration_id
            or binding.scope != view(configuration, version)["scope"]
            or not isinstance(connection, dict)
            or connection.get("tenant_id") != self.actor.org_id
            or connection.get("project_id") != project_id
            or binding.connection_ref != connection
            or native.get("record_type") != "agent_space"
            or native.get("source_connection_id") != connection.get("source_connection_id")
            or native.get("record_id") != receipt.native_space_id
            or identity.get("account_id") != selection.account_id
            or identity.get("region") != selection.region
            or devops.get("region") != selection.region
            or binding.gaps
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        launcher_role = devops.get("launcher_role_arn")
        operator_role = devops.get("operator_role_arn")
        if (
            not isinstance(launcher_role, str)
            or not isinstance(operator_role, str)
            or not federation._role_account(launcher_role, selection.account_id)
            or not federation._role_account(operator_role, selection.account_id)
            or launcher_role == operator_role
        ):
            raise HTTPException(503, detail={"code": "launch_configuration_unavailable"})
        credential = await self.db.get(UserCredential, connection.get("record_id"), populate_existing=True)
        if (
            credential is None
            or credential.user_id != configuration.owner_id
            or credential.org_id != self.actor.org_id
            or credential.id != connection.get("source_connection_id")
            or credential.service != "aws"
            or credential.credential_type != "aws_role"
            or credential.aws_verified_version_id != connection.get("record_revision")
            or identity.get("role_arn") != (credential.scopes or {}).get("role_arn")
            or identity.get("account_id") != (credential.scopes or {}).get("account_id")
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        try:
            verified_reference = _verified_aws_connection_ref(credential, self.actor.org_id, project_id)
        except HTTPException:
            raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from None
        if connection != verified_reference:
            raise HTTPException(404, detail={"code": "not_found"})
        grant = native_grant or await self.db.scalar(
            select(CredentialServiceDelegation)
            .where(
                CredentialServiceDelegation.tenant_id == self.actor.org_id,
                CredentialServiceDelegation.product_id == product_id,
                CredentialServiceDelegation.project_id == project_id,
                CredentialServiceDelegation.credential_id == credential.id,
                CredentialServiceDelegation.created_by == configuration.owner_id,
                CredentialServiceDelegation.service_id == worker_id("devops"),
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > datetime.now(UTC),
            )
            .execution_options(populate_existing=True)
        )
        if (
            grant is None
            or "credential:session" not in grant.operations
            or "native_ui" not in grant.capabilities
            or grant.binding_generation != binding.generation
            or {**connection, "record_type": "credential"} not in grant.sources
        ):
            raise HTTPException(403, detail={"code": "delegation_denied"})
        issuer = get_settings().gateway_base_url
        if native_grant is None:
            await require_execution_scope(self.db, grant, check_binding=True, binding_id=binding.binding_id, binding_generation=binding.generation)
        try:
            parsed_issuer = urlsplit(issuer)
        except (TypeError, ValueError):
            raise HTTPException(503, detail={"code": "launch_configuration_unavailable"}) from None
        if (
            parsed_issuer.scheme != "https"
            or not parsed_issuer.hostname
            or parsed_issuer.username
            or parsed_issuer.password
            or parsed_issuer.path not in {"", "/"}
            or parsed_issuer.query
            or parsed_issuer.fragment
            or not isinstance(receipt.native_space_id, str)
            or re.fullmatch(r"[A-Za-z0-9-]{3,128}", receipt.native_space_id) is None
        ):
            raise HTTPException(503, detail={"code": "launch_configuration_unavailable"})
        return SimpleNamespace(
            configuration=configuration,
            version=version,
            binding=binding,
            grant=grant,
            credential=credential,
            selection=selection,
            receipt=receipt,
            issuer=issuer,
            launcher_role=launcher_role,
            operator_role=operator_role,
        )

    @staticmethod
    def _browser_launch_stamp(current):
        return hashlib.sha256(
            json.dumps(
                {
                    "configuration": [current.configuration.configuration_id, current.version.revision, current.version.hash],
                    "binding": [
                        current.binding.binding_id,
                        current.binding.revision,
                        current.binding.generation,
                        current.binding.native_resource_ref,
                    ],
                    "grant": [current.grant.id, current.grant.revision, current.grant.execution_scope, str(current.grant.expires_at)],
                    "roles": [current.launcher_role, current.operator_role],
                    "connection": current.binding.connection_ref,
                    "receipt": current.receipt.operation_id,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    async def check_browser_launch(self, product_id, project_id, binding_id, configuration_revision, request):
        """Check current human launch authority without minting a session or recording arrival."""
        current = await self._browser_launch_authority(product_id, project_id, binding_id, configuration_revision, request)
        stamp = self._browser_launch_stamp(current)
        session = await self._devops_connection_session(
            current.credential, current.selection, current.receipt.operation_id, purpose="browser-access-check"
        )
        await federation.verify_operator_app(session.client("devops-agent"), current.receipt.native_space_id, current.operator_role)
        current = await self._browser_launch_authority(product_id, project_id, binding_id, configuration_revision, request, consume_rate_limit=False)
        if self._browser_launch_stamp(current) != stamp:
            raise HTTPException(403, detail={"code": "native_ui_consent_changed"})
        now = datetime.now(UTC)
        grant_expiry = current.grant.expires_at
        if grant_expiry.tzinfo is None:
            grant_expiry = grant_expiry.replace(tzinfo=UTC)
        return {
            "ready": True,
            "gaps": [],
            "capability": "native_ui",
            "scope": view(current.configuration, current.version)["scope"],
            "configuration_id": current.configuration.configuration_id,
            "configuration_revision": current.version.revision,
            "configuration_hash": current.version.hash,
            "binding_id": current.binding.binding_id,
            "binding_generation": current.binding.generation,
            "binding_revision": current.binding.revision,
            "checked_at": now.isoformat(),
            "valid_until": min(now + timedelta(seconds=60), self.actor.expires_at, grant_expiry).isoformat(),
        }

    async def _launch_browser(self, product_id, project_id, binding_id, configuration_revision, request, observation=None):
        current = await self._browser_launch_authority(product_id, project_id, binding_id, configuration_revision, request)
        stamp = self._browser_launch_stamp(current)
        if observation is not None:
            await observation.before_provider()
        session = await self._devops_connection_session(current.credential, current.selection, current.receipt.operation_id, purpose="browser-launch")
        async with httpx.AsyncClient() as client:
            result = await federation.mint_browser_launch(
                current.selection,
                native_space_id=current.receipt.native_space_id,
                launcher_role_arn=current.launcher_role,
                operator_role_arn=current.operator_role,
                user_id=self.actor.user_id,
                issuer=current.issuer,
                sts_client=session.client("sts"),
                operator_client=session.client("devops-agent"),
                expires_at=min(
                    self.actor.expires_at,
                    current.grant.expires_at.replace(tzinfo=UTC)
                    if current.grant.expires_at.tzinfo is None
                    else current.grant.expires_at.astimezone(UTC),
                ),
                federation_client=client,
            )
        refreshed = await self._browser_launch_authority(
            product_id, project_id, binding_id, configuration_revision, request, consume_rate_limit=False
        )
        if self._browser_launch_stamp(refreshed) != stamp:
            raise HTTPException(403, detail={"code": "native_ui_consent_changed"})
        return result

    @staticmethod
    def _require_execution_inputs(settings, configuration):
        gaps = execution_input_gaps(settings, scope={"tenant_id": configuration.tenant_id, "project_id": configuration.project_id})
        if gaps:
            raise HTTPException(
                409,
                detail={
                    "code": "execution_policy_unavailable",
                    "message": "Complete the saved classification and assessment limits, including an immutable budget policy reference, then retry.",
                    "gaps": gaps,
                },
            )

    async def _security_work_target(self, configuration, version, run_input, *, service_id="security"):
        product_id, project_id = configuration.product_id, configuration.project_id
        if self.actor.account_type != "human" or configuration.owner_id != self.actor.user_id or configuration.handoff_state != "linked":
            raise HTTPException(404, detail={"code": "not_found"})
        if version.settings.get("enabled") is not True:
            raise HTTPException(409, detail={"code": "configuration_disabled"})
        self._require_execution_inputs(version.settings, configuration)
        if not callable(self.publish):
            raise HTTPException(503, detail={"code": "work_queue_unavailable"})
        sources = run_input.get("sources")

        def source_selected(selected, source):
            return (
                isinstance(selected, dict)
                and isinstance(source, dict)
                and (
                    selected == source
                    or run_input.get("capability") == "code_review"
                    and selected.get("record_type") == "repository"
                    and source.get("record_type") == "repository_commit"
                    and all(selected.get(field) == source.get(field) for field in ("tenant_id", "project_id", "source_connection_id", "record_id"))
                )
            )

        def selected_sources(draft):
            return (
                isinstance(draft, list)
                and isinstance(sources, list)
                and len(draft) == len(sources)
                and all(source_selected(selected, source) for selected, source in zip(draft, sources, strict=True))
            )

        triggers = [
            trigger
            for trigger in version.settings.get("triggers", [])
            if trigger.get("kind") == "manual"
            and trigger.get("enabled") is True
            and trigger.get("capability") == run_input.get("capability")
            and trigger.get("binding_id") == run_input.get("binding_id")
            and selected_sources(trigger.get("source_refs"))
        ]
        if len(triggers) != 1 or not isinstance(sources, list) or not sources:
            raise HTTPException(404, detail={"code": "not_found"})
        allowed = await self.source_rights(project_id)
        if any(
            not isinstance(source, dict)
            or not any(source_selected(draft, source) for draft in version.settings.get("source_refs", []))
            or source.get("tenant_id") != self.store.tenant_id
            or source.get("project_id") != project_id
            or (source.get("source_connection_id"), source.get("record_id")) not in allowed
            for source in sources
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        binding = await (await self.binding_store(product_id, project_id, service_id)).binding(run_input["binding_id"])
        connection = version.settings.get("connection_ref")
        if (
            binding.configuration_id != configuration.configuration_id
            or binding.state != "ready"
            or binding.gaps
            or binding.connection_ref != connection
            or binding.scope != view(configuration, version)["scope"]
        ):
            raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})
        credential = await self.db.get(UserCredential, connection.get("record_id") if isinstance(connection, dict) else None)
        if (
            credential is None
            or credential.org_id != self.store.tenant_id
            or credential.user_id != configuration.owner_id
            or credential.id != connection.get("source_connection_id")
            or credential.aws_verified_version_id != connection.get("record_revision")
            or credential.service != "aws"
            or credential.credential_type != "aws_role"
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        try:
            verified_connection_evidence(credential)
        except HTTPException:
            raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from None
        registration = None
        if run_input["capability"] in {"code_review", "threat_model"}:
            identity = binding.provider_identity or {}
            native = binding.native_resource_ref or {}
            registration = current_registration(binding)
            model = current_model(binding) if run_input["capability"] == "threat_model" else None
            if (
                registration is None
                or run_input["capability"] == "threat_model"
                and model is None
                or identity.get("account_id") != (credential.scopes or {}).get("account_id")
                or identity.get("role_arn") != (credential.scopes or {}).get("role_arn")
                or identity.get("region") != "us-east-1"
                or native.get("tenant_id") != self.store.tenant_id
                or native.get("project_id") != project_id
                or native.get("source_connection_id") != credential.id
                or native.get("record_type") != "security_space"
                or not native.get("record_id")
            ):
                raise HTTPException(409, detail={"code": "source_readiness_unverified"})
        grants = (
            await self.db.scalars(
                select(CredentialServiceDelegation)
                .where(
                    CredentialServiceDelegation.tenant_id == self.store.tenant_id,
                    CredentialServiceDelegation.product_id == product_id,
                    CredentialServiceDelegation.project_id == project_id,
                    CredentialServiceDelegation.credential_id == credential.id,
                    CredentialServiceDelegation.created_by == configuration.owner_id,
                    CredentialServiceDelegation.service_id == worker_id(service_id),
                    CredentialServiceDelegation.parent_delegation_id.is_(None),
                    CredentialServiceDelegation.operation_id.is_(None),
                    (
                        CredentialServiceDelegation.execution_scope["purpose"].as_string().is_(None)
                        | (CredentialServiceDelegation.execution_scope["purpose"].as_string() != "devops_native_ui")
                    ),
                    CredentialServiceDelegation.revoked_at.is_(None),
                    CredentialServiceDelegation.expires_at > datetime.now(UTC),
                )
                .limit(2)
            )
        ).all()
        session_ref = {**connection, "record_type": "credential"}
        if (
            len(grants) != 1
            or grants[0].binding_generation != binding.generation
            or "source:pin" not in grants[0].operations
            or "credential:session" not in grants[0].operations
            or run_input["capability"] not in grants[0].capabilities
            or any(source not in grants[0].sources for source in [session_ref, *sources])
        ):
            raise HTTPException(403, detail={"code": "delegation_denied"})
        grant = grants[0]
        await require_execution_scope(self.db, grant, check_binding=True, binding_id=binding.binding_id, binding_generation=binding.generation)
        if self.admission_key is not None:
            grant = await issue_l5_work_delegation(
                self.db,
                self.actor,
                grant,
                credential,
                binding,
                connection,
                sources,
                run_input["capability"],
                version.settings["limits"]["deadline_seconds"],
                self.admission_operation,
                self.admission_key,
                previous_id=self.previous_delegation_id,
            )
        settings = version.settings
        verified = {
            "scope": binding.scope,
            "service_id": service_id,
            "actor_id": self.actor.user_id,
            "actor_type": "human",
            "configuration_id": configuration.configuration_id,
            "configuration_revision": version.revision,
            "configuration_hash": version.hash,
            "configuration_enabled": True,
            "profile_ref": settings["profile_ref"],
            "connection_ref": connection,
            "binding_connection_ref": binding.connection_ref,
            "binding_id": binding.binding_id,
            "binding_generation": binding.generation,
            "binding_state": binding.state,
            "delegation_id": grant.id,
            "delegation_version": grant.revision,
            "delegation_active": True,
            "trigger": {**triggers[0], "source_refs": sources},
            "adapter_revision": f"{service_id}-v1",
            "limits": settings["limits"],
            "visibility": settings["visibility"],
            "classification": settings["classification"],
            "causal_origin": "manual",
            "exclusions": settings.get("exclusions", []),
            "monitoring_policy": effective_monitoring(settings),
            "max_bytes": REPOSITORY_MAX_BYTES if service_id == "security" and run_input["capability"] == "code_review" else 6 * 1024 * 1024,
            "max_files": REPOSITORY_MAX_FILES if service_id == "security" and run_input["capability"] == "code_review" else 1000,
            **(
                {
                    "native_space_ref": binding.native_resource_ref,
                    "provider_identity": binding.provider_identity,
                    "source_registration": registration,
                    **({"native_model_ref": model} if model else {}),
                }
                if registration is not None
                else {}
            ),
        }
        source_client = self.security_source_client
        if source_client is None and self.admission_key:
            source_client = GatewayAdmissionSourceClient(
                self.db,
                self.actor,
                grant.id,
                verified,
                self.admission_operation,
                self.admission_key,
                service_id=service_id,
                run_input=run_input,
            )
        if service_id == "security":
            try:
                readiness, gaps, _ = assess_readiness(settings, configuration.configuration_id, [view_binding(binding)], delegation_active=True)
            except ValueError:
                raise HTTPException(409, detail={"code": "configuration_disabled"}) from None
            if readiness != "ready" or gaps:
                raise HTTPException(409, detail={"code": "configuration_disabled"})
        if not callable(getattr(source_client, "pin_source", None)):
            raise HTTPException(503, detail={"code": "source_authority_unavailable"})
        return verified, source_client

    @property
    def transform_submission_ready(self):
        return callable(self.publish) and callable(self.transform_work_authority) and self.transform_result_reader_ready

    @property
    def transform_result_reader_ready(self):
        return (
            self.actor.account_type == "human" and self.actor.user_id is not None and callable(getattr(self.transform_artifact_store, "read", None))
        )

    async def admission_authority(self, product_id, project_id, service_id, run_input=None):
        configuration, version = await self.current(product_id, project_id, service_id)
        if service_id in {"security", "well_architected"} and version.settings.get("enabled") is True:
            self._require_execution_inputs(version.settings, configuration)
        if service_id == "security":
            return await self._security_work_target(configuration, version, run_input or {})
        if service_id == "well_architected":
            reference, readback = await self._wa_admission_readback(configuration, version, run_input or {})
            if not callable(self.wa_authority):
                raise HTTPException(503, detail={"code": "work_authority_unavailable"})
            if not callable(self.publish):
                raise HTTPException(503, detail={"code": "work_queue_unavailable"})
            verified, source_client = await self.wa_authority(configuration, version, run_input, self.actor)
            if (
                not isinstance(verified, dict)
                or verified.get("native_profile_ref") != reference
                or verified.get("profile_hash") != readback["readiness"][0]["snapshot_hash"]
                or verified.get("scope") is None
                or verified["scope"].get("tenant_id") != reference["tenant_id"]
                or verified["scope"].get("project_id") != reference["project_id"]
                or verified.get("source_refs") != run_input.get("sources")
                or verified.get("generation_scope", {}).get("pillars") != version.settings["well_architected"]["pillars"]
                or not verified.get("native_creator")
                or source_client is None
                or not callable(getattr(source_client, "pin_source", None))
            ):
                raise HTTPException(409, detail={"code": "native_unqualified"})
            return verified, source_client
        if service_id == "transform":
            if self.actor.account_type != "human" or configuration.owner_id != self.actor.user_id or configuration.handoff_state != "linked":
                raise HTTPException(404, detail={"code": "not_found"})
            if version.settings.get("enabled") is not True:
                raise HTTPException(409, detail={"code": "configuration_disabled"})
            if not self.transform_submission_ready:
                raise HTTPException(503, detail={"code": "capability_unavailable"})
            if not (await self.transform_configuration_readiness(configuration, version, product_id, project_id))["ready"]:
                raise HTTPException(409, detail={"code": "configuration_disabled"})
            sources = (run_input or {}).get("sources")
            allowed = await _product_call(permitted_source_refs(self.db, self.actor, project_id))
            if (
                not isinstance(sources, list)
                or len(sources) != 1
                or any((source.get("source_connection_id"), source.get("record_id")) not in allowed for source in sources)
            ):
                raise HTTPException(404, detail={"code": "not_found"})
            store, authority = await self.binding_authority(product_id, project_id, service_id)
            binding = await store.binding((run_input or {}).get("binding_id"))
            identity = binding.provider_identity
            if (
                binding.configuration_id != configuration.configuration_id
                or binding.state != "ready"
                or binding.gaps
                or binding.scope != authority["scope"]
                or binding.connection_ref != authority["connection_ref"]
                or not isinstance(identity, dict)
                or any(not identity.get(field) or identity[field] != authority.get(field) for field in ("account_id", "role_arn", "region"))
            ):
                raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})
            verified, source_client = await self.transform_work_authority(configuration, version, binding, run_input, authority)
            if (
                not isinstance(verified, dict)
                or not callable(getattr(source_client, "pin_source", None))
                or verified.get("scope") != authority["scope"]
                or verified.get("connection_ref") != authority["connection_ref"]
                or verified.get("configuration_id") != configuration.configuration_id
                or verified.get("configuration_revision") != version.revision
                or verified.get("binding_id") != binding.binding_id
                or verified.get("binding_generation") != binding.generation
                or verified.get("provider_identity", identity) != identity
            ):
                raise HTTPException(409, detail={"code": "native_unqualified"})
            return {**verified, "provider_identity": dict(identity)}, source_client
        if service_id == "finops" and getattr(self, "finops_authority", None) is not None:
            if self.actor.account_type != "human" or configuration.owner_id != self.actor.user_id or configuration.handoff_state != "linked":
                raise HTTPException(404, detail={"code": "not_found"})
            if version.settings.get("enabled") is not True:
                raise HTTPException(409, detail={"code": "configuration_disabled"})
            if not callable(self.publish):
                raise HTTPException(503, detail={"code": "work_queue_unavailable"})
            verified, source_client = await self.finops_authority(configuration, version, run_input, self.actor)
            if not isinstance(verified, dict) or verified.get("native_qualified") is not True:
                raise HTTPException(409, detail={"code": "native_unqualified"})
            if (run_input or {}).get("sources") and not callable(getattr(source_client, "pin_source", None)):
                raise HTTPException(503, detail={"code": "source_authority_unavailable"})
            return verified, source_client
        if service_id != "devops":
            raise HTTPException(503, detail={"code": "work_authority_unavailable"})
        if self.actor.account_type != "human" or configuration.owner_id != self.actor.user_id or configuration.handoff_state != "linked":
            raise HTTPException(404, detail={"code": "not_found"})
        if version.state != "ready" or version.gaps or version.settings.get("enabled") is not True:
            raise HTTPException(409, detail={"code": "configuration_disabled"})
        if not callable(self.publish):
            raise HTTPException(503, detail={"code": "work_queue_unavailable"})
        capability = (run_input or {}).get("capability")
        options = (run_input or {}).get("devops_input") or {}
        devops = version.settings.get("devops") or {}
        triggers = [
            trigger
            for trigger in devops.get("trigger_policies", [])
            if (
                trigger.get("kind") == "manual"
                and trigger.get("enabled") is True
                and trigger.get("capability") == capability
                and capability in {"investigation", "release_verification", "release_readiness"}
                and trigger.get("binding_id") == (run_input or {}).get("binding_id")
                and (
                    capability in {"investigation", "release_verification"}
                    and trigger.get("resource_refs") == options.get("resource_refs", [])
                    and trigger.get("telemetry_refs") == options.get("telemetry_refs", [])
                    and (capability != "release_verification" or trigger.get("test_profile_ref") == options.get("test_profile_ref"))
                    or capability == "release_readiness"
                    and selected_repository(trigger, options.get("repository_ref"))
                )
            )
        ]
        if len(triggers) != 1:
            raise HTTPException(404, detail={"code": "not_found"})
        verified, source_client = await self._devops_work_target(configuration, version, triggers[0], actor_id=self.actor.user_id, actor_type="human")
        if capability != "release_readiness":
            return verified, source_client
        repository = options["repository_ref"]
        allowed = await _product_call(permitted_source_refs(self.db, self.actor, project_id))
        if (repository["source_connection_id"], repository["record_id"]) not in allowed:
            raise HTTPException(404, detail={"code": "not_found"})
        if self.release_authority is None:
            raise HTTPException(503, detail={"code": "release_prerequisite_unavailable"})
        qualified = await self.release_authority(run_input, verified, version)
        if not isinstance(qualified, dict):
            raise HTTPException(503, detail={"code": "release_prerequisite_unavailable"})
        eligibility = qualified.get("eligibility")
        source_client = qualified.get("source_client")
        if (
            not release_eligible(options, eligibility, verified["scope"], verified["connection_ref"], verified["native_space_ref"])
            or source_client is None
            or not callable(getattr(source_client, "pin_source", None))
            or not isinstance(qualified.get("max_bytes"), int)
            or not 1 <= qualified["max_bytes"] <= 64 * 1024 * 1024
            or not isinstance(qualified.get("max_files"), int)
            or not 1 <= qualified["max_files"] <= 1000
        ):
            raise HTTPException(409, detail={"code": "release_prerequisite_unavailable"})
        # The owned callback derives an exact child grant. Preserve the source
        # capture context verbatim so durable preparation and pinning agree.
        admitted = qualified.get("verified")
        if admitted is not None:
            unchanged = {
                key: value
                for key, value in verified.items()
                if key not in {"delegation_id", "delegation_version", "trigger", "adapter_revision", "max_bytes", "max_files"}
            }
            if (
                not isinstance(admitted, dict)
                or any(admitted.get(key) != value for key, value in unchanged.items())
                or admitted.get("release_eligibility") != eligibility
                or admitted.get("trigger")
                != {**verified["trigger"], "source_refs": [{**repository, "record_revision": options["base_revision"]}, repository]}
                or admitted.get("max_bytes") != qualified["max_bytes"]
                or admitted.get("max_files") != qualified["max_files"]
            ):
                raise HTTPException(409, detail={"code": "release_prerequisite_unavailable"})
            return admitted, source_client
        return {
            **verified,
            "release_eligibility": eligibility,
            "adapter_revision": "release-diff-v1",
            "max_bytes": qualified["max_bytes"],
            "max_files": qualified["max_files"],
        }, source_client

    async def _wa_admission_readback(self, configuration, version, run_input):
        if self.actor.account_type != "human" or configuration.owner_id != self.actor.user_id or configuration.handoff_state != "linked":
            raise HTTPException(404, detail={"code": "not_found"})
        if version.settings.get("enabled") is not True or set(version.gaps or []) - {
            "provider_unqualified",
            "catalog_mapping_unresolved",
            "execution_delegation",
        }:
            raise HTTPException(409, detail={"code": "configuration_disabled"})
        if run_input.get("configuration_revision") != version.revision:
            raise HTTPException(409, detail={"code": "stale_revision"})
        triggers = version.settings.get("triggers") or []
        if not any(
            trigger.get("kind") == "manual"
            and trigger.get("enabled") is True
            and trigger.get("capability") == "architecture_review"
            and trigger.get("binding_id") == run_input.get("binding_id")
            for trigger in triggers
        ):
            raise HTTPException(409, detail={"code": "trigger_disabled"})
        sources = run_input.get("sources")
        allowed = await self.source_rights(configuration.project_id)
        if (
            sources != version.settings.get("source_refs")
            or not isinstance(sources, list)
            or any(not isinstance(source, dict) or (source.get("source_connection_id"), source.get("record_id")) not in allowed for source in sources)
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        if self.wa_profile_client is None:
            raise HTTPException(503, detail={"code": "native_readback_unavailable"})
        store, verified = await self.binding_authority(configuration.product_id, configuration.project_id, "well_architected")
        if verified["configuration_id"] != configuration.configuration_id or verified["configuration_revision"] != version.revision:
            raise HTTPException(409, detail={"code": "stale_revision"})
        binding = await store.binding(run_input["binding_id"])
        reference = binding.native_resource_ref or {}
        identity = binding.provider_identity or {}
        if (
            binding.configuration_id != configuration.configuration_id
            or binding.scope != verified["scope"]
            or binding.connection_ref != verified["connection_ref"]
            or binding.state != "ready"
            or binding.gaps
            or reference.get("tenant_id") != self.store.tenant_id
            or reference.get("project_id") != configuration.project_id
            or reference.get("record_type") != "agent_profile"
            or reference.get("source_connection_id") != verified["connection_ref"]["record_id"]
            or any(identity.get(field) != verified[field] for field in ("account_id", "region", "role_arn"))
        ):
            raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})
        settings = version.settings["well_architected"]
        try:
            selection = ProfileSelection(
                self.store.tenant_id,
                configuration.product_id,
                configuration.project_id,
                verified["account_id"],
                verified["region"],
                verified["role_arn"],
                settings["execution_role_ref"]["record_id"],
                tuple(settings["pillars"]),
                verified["source_role_qualification"],
            )
            client = await self.wa_profile_client(verified, version)
            if client is None:
                raise ProfileIdentityError("native Profile client unavailable")
            observed = await ProfileBindingProvider(client, selection, verified["connection_ref"]).read(reference, verified)
        except (BotoCoreError, ClientError, ConnectionError, TimeoutError, ProfileIdentityError, KeyError, TypeError, ValueError):
            raise HTTPException(503, detail={"code": "native_readback_unavailable"}) from None
        if observed["state"] != "ready":
            changed = await self.db.execute(
                update(Binding)
                .where(
                    Binding.tenant_id == binding.tenant_id,
                    Binding.binding_id == binding.binding_id,
                    Binding.revision == binding.revision,
                    Binding.generation == binding.generation,
                    Binding.state == "ready",
                )
                .values(
                    state="blocked", revision=Binding.revision + 1, gaps=observed["gaps"], readiness=[*binding.readiness, *observed["readiness"]]
                ),
                execution_options={"synchronize_session": False},
            )
            if changed.rowcount != 1:
                await self.db.rollback()
                raise HTTPException(409, detail={"code": "stale_binding_generation"})
            await self.db.commit()
            raise HTTPException(409, detail={"code": "native_profile_drift"})
        return reference, observed

    async def incident_authority(self, receipt):
        configuration = await self.db.get(Configuration, (receipt.tenant_id, receipt.configuration_id))
        version = await self.db.get(ConfigurationVersion, (receipt.tenant_id, receipt.configuration_id, receipt.configuration_revision))
        if (
            configuration is None
            or version is None
            or configuration.product_id != receipt.product_id
            or configuration.project_id != receipt.project_id
            or configuration.service_id != "devops"
            or configuration.current_revision != version.revision
            or configuration.handoff_state != "linked"
            or self.actor.org_id != receipt.tenant_id
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        if version.state != "ready" or version.gaps or version.settings.get("enabled") is not True:
            raise HTTPException(409, detail={"code": "configuration_disabled"})
        if not callable(self.publish):
            raise HTTPException(503, detail={"code": "work_queue_unavailable"})
        devops = version.settings.get("devops") or {}
        sources = [
            source
            for source in devops.get("incident_sources", [])
            if (source.get("source_ref") == receipt.source_ref and source.get("trigger_id") == receipt.trigger_id)
        ]
        triggers = [
            trigger
            for trigger in devops.get("trigger_policies", [])
            if (
                trigger.get("trigger_id") == receipt.trigger_id
                and trigger == receipt.target
                and trigger.get("kind") == "native_event"
                and trigger.get("enabled") is True
                and trigger.get("capability") == "investigation"
            )
        ]
        if len(sources) != 1 or len(triggers) != 1 or sources[0].get("environment_ref") != (receipt.causal_origin or {}).get("environment_ref"):
            raise HTTPException(404, detail={"code": "not_found"})
        require_new_work(version.settings, triggers[0], event=receipt.causal_origin)
        verified, source_client = await self._devops_work_target(
            configuration, version, triggers[0], actor_id=receipt.source_ref["record_id"], actor_type="service", event_id=receipt.event_id
        )
        parent = await self.db.get(CredentialServiceDelegation, verified["delegation_id"], populate_existing=True)
        binding = await self.db.get(Binding, (receipt.tenant_id, verified["binding_id"]), populate_existing=True)
        credential = await self.db.get(UserCredential, verified["connection_ref"]["record_id"], populate_existing=True)
        child = await issue_l5_incident_delegation(
            self.db, receipt, parent, credential, binding, verified["connection_ref"], verified["limits"]["deadline_seconds"]
        )
        verified.update(
            actor_id=child.created_by,
            delegation_id=child.id,
            delegation_version=child.revision,
            event_source_ref=receipt.source_ref,
            event_kind=receipt.event_kind,
        )
        return verified, source_client

    async def _devops_work_context(self, configuration, version, trigger):
        """Read-only admission checks shared with the explicit readiness snapshot."""
        product_id, project_id = configuration.product_id, configuration.project_id
        require_new_work(version.settings, trigger)
        devops = version.settings.get("devops") or {}
        binding = await BindingStore(self.db, self.store.tenant_id, product_id, project_id, "devops").binding(trigger["binding_id"])
        scope = view(configuration, version)["scope"]
        connection = devops.get("hosting_connection_ref")
        if (
            binding.configuration_id != configuration.configuration_id
            or binding.scope != scope
            or binding.state != "ready"
            or binding.gaps
            or binding.native_resource_ref is None
            or binding.connection_ref != connection
            or not isinstance(connection, dict)
            or not isinstance(binding.native_resource_ref, dict)
            or any(
                binding.native_resource_ref.get(field) != value
                for field, value in (
                    ("tenant_id", self.store.tenant_id),
                    ("project_id", project_id),
                    ("source_connection_id", connection.get("source_connection_id")),
                    ("record_type", "agent_space"),
                )
            )
            or not binding.native_resource_ref.get("record_id")
        ):
            raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})
        credential = await self.db.get(UserCredential, connection.get("record_id"))
        if (
            credential is None
            or credential.id != connection.get("source_connection_id")
            or credential.org_id != self.store.tenant_id
            or credential.user_id != configuration.owner_id
            or credential.service != "aws"
            or credential.credential_type != "aws_role"
            or credential.aws_verified_version_id != connection.get("record_revision")
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        try:
            verified_connection_evidence(credential)
        except HTTPException:
            raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from None
        identity = credential.scopes or {}
        selected_account, selected_role = identity.get("account_id"), identity.get("role_arn")
        if (
            not isinstance(selected_account, str)
            or re.fullmatch(r"[0-9]{12}", selected_account) is None
            or not isinstance(selected_role, str)
            or re.fullmatch(rf"arn:aws:iam::{selected_account}:role/.+", selected_role) is None
            or not isinstance(binding.provider_identity, dict)
            or any(
                binding.provider_identity.get(field) != selected
                for field, selected in (
                    ("account_id", selected_account),
                    ("role_arn", selected_role),
                    ("region", devops.get("region")),
                )
            )
        ):
            raise HTTPException(409, detail={"code": "binding_or_delegation_unavailable"})
        session_ref = {**connection, "record_type": "credential"}
        grant = await self.db.scalar(
            select(CredentialServiceDelegation).where(
                CredentialServiceDelegation.tenant_id == self.store.tenant_id,
                CredentialServiceDelegation.product_id == product_id,
                CredentialServiceDelegation.project_id == project_id,
                CredentialServiceDelegation.credential_id == credential.id,
                CredentialServiceDelegation.created_by == configuration.owner_id,
                CredentialServiceDelegation.service_id == worker_id("devops"),
                CredentialServiceDelegation.parent_delegation_id.is_(None),
                CredentialServiceDelegation.operation_id.is_(None),
                (
                    CredentialServiceDelegation.execution_scope["purpose"].as_string().is_(None)
                    | (CredentialServiceDelegation.execution_scope["purpose"].as_string() != "devops_native_ui")
                ),
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > datetime.now(UTC),
            )
        )
        if (
            grant is None
            or "credential:session" not in grant.operations
            or trigger.get("capability") not in grant.capabilities
            or session_ref not in grant.sources
            or grant.binding_generation != binding.generation
        ):
            raise HTTPException(403, detail={"code": "delegation_denied"})
        await require_execution_scope(self.db, grant, check_binding=True, binding_id=binding.binding_id, binding_generation=binding.generation)
        limits = version.settings.get("limits") or {}
        try:
            cost = Decimal(limits["max_cost"])
        except (KeyError, InvalidOperation, TypeError):
            raise HTTPException(409, detail={"code": "execution_policy_unavailable"}) from None
        if (
            not cost.is_finite()
            or cost <= 0
            or limits.get("currency") != "USD"
            or (not limits.get("policy_ref") and not (trigger.get("kind") == "manual" and trigger.get("capability") == "investigation"))
            or not isinstance(limits.get("deadline_seconds"), int)
            or not 60 <= limits["deadline_seconds"] <= 86400
        ):
            raise HTTPException(409, detail={"code": "execution_policy_unavailable"})
        return binding, credential, grant

    async def _devops_work_target(self, configuration, version, trigger, *, actor_id, actor_type, event_id=None):
        binding, credential, grant = await self._devops_work_context(configuration, version, trigger)
        scope = view(configuration, version)["scope"]
        connection = version.settings["devops"]["hosting_connection_ref"]
        limits = version.settings["limits"]
        if self.admission_key is not None and trigger["capability"] in {"investigation", "release_verification"}:
            grant = await issue_l5_work_delegation(
                self.db,
                self.actor,
                grant,
                credential,
                binding,
                connection,
                [],
                trigger["capability"],
                limits["deadline_seconds"],
                self.admission_operation,
                self.admission_key,
                previous_id=self.previous_delegation_id,
            )
        return (
            {
                "scope": scope,
                "service_id": "devops",
                "actor_id": actor_id,
                "actor_type": actor_type,
                "configuration_id": configuration.configuration_id,
                "configuration_revision": version.revision,
                "configuration_hash": version.hash,
                "configuration_enabled": True,
                "profile_ref": version.settings["profile_ref"],
                "connection_ref": connection,
                "binding_connection_ref": binding.connection_ref,
                "binding_id": binding.binding_id,
                "binding_generation": binding.generation,
                "binding_state": binding.state,
                "delegation_id": grant.id,
                "delegation_version": grant.revision,
                "delegation_active": True,
                "trigger": trigger,
                "adapter_revision": "devops-release-verification-v2"
                if trigger["capability"] == "release_verification"
                else "devops-investigation-v1",
                "limits": limits,
                "visibility": version.settings["visibility"],
                "classification": version.settings["classification"],
                "native_space_ref": binding.native_resource_ref,
                "provider_identity": binding.provider_identity,
                "causal_origin": "incident" if event_id is not None else "manual",
                "exclusions": version.settings.get("exclusions", []),
                "monitoring_policy": effective_monitoring(version.settings),
                "max_bytes": 0,
                "max_files": 0,
                **({"event_id": event_id} if event_id is not None else {}),
            },
            None,
        )

    async def _require_devops_budget(self, session, limits, scope, now, replay):
        policy_ref = limits["policy_ref"]
        if (
            not isinstance(policy_ref, dict)
            or policy_ref.get("record_type") != "policy"
            or policy_ref.get("tenant_id") != scope["tenant_id"]
            or policy_ref.get("project_id") != scope["project_id"]
        ):
            raise HTTPException(409, detail={"code": "execution_policy_unavailable"})
        locked = await session.execute(
            text("UPDATE budget_configs SET budget_amount_usd = budget_amount_usd WHERE id = :policy_id AND org_id = :tenant_id"),
            {"policy_id": policy_ref.get("record_id"), "tenant_id": scope["tenant_id"]},
        )
        if locked.rowcount != 1:
            raise HTTPException(409, detail={"code": "execution_policy_unavailable"})
        previous = await replay()
        if previous is not None:
            return previous
        budget = await session.scalar(
            select(BudgetConfig)
            .where(
                BudgetConfig.id == policy_ref.get("record_id"),
                BudgetConfig.org_id == scope["tenant_id"],
                BudgetConfig.entity_type == "org",
                BudgetConfig.entity_id == scope["tenant_id"],
                BudgetConfig.enforcement_mode == "hard",
            )
            .with_for_update()
        )
        if budget is None or budget.period_type not in {"daily", "weekly", "monthly"}:
            raise HTTPException(409, detail={"code": "execution_policy_unavailable"})
        start, _ = get_period_start_end(PeriodType(budget.period_type), now.date())
        spent = await session.scalar(
            select(BudgetUsage.total_cost_usd).where(
                BudgetUsage.org_id == scope["tenant_id"],
                BudgetUsage.entity_type == "org",
                BudgetUsage.entity_id == scope["tenant_id"],
                BudgetUsage.period_type == budget.period_type,
                BudgetUsage.period_start == start,
            )
        ) or Decimal(0)
        existing = (
            await session.scalars(
                select(Run).where(
                    Run.tenant_id == scope["tenant_id"],
                    Run.service_id.in_(("devops", "well_architected", "finops")),
                    Run.origin == "adp_submitted",
                )
            )
        ).all()
        reserved = Decimal(0)
        for run in existing:
            admission = run.admission
            # Manual investigations carry a planning estimate, not a budget reservation.
            if (
                run.service_id == "devops"
                and (run.input or {}).get("capability") == "investigation"
                and isinstance(admission, dict)
                and admission.get("causal_origin") == "manual"
            ):
                continue
            try:
                admitted_at = datetime.fromisoformat(admission["admitted_at"].replace("Z", "+00:00"))
                if admission["limits"]["policy_ref"]["record_id"] == budget.id and admitted_at.date() >= start:
                    reserved += Decimal(admission["limits"]["max_cost"])
            except (KeyError, ValueError, TypeError, InvalidOperation):
                raise HTTPException(409, detail={"code": "execution_policy_unavailable"}) from None
        if Decimal(limits["max_cost"]) + reserved + spent > budget.budget_amount_usd:
            raise HTTPException(409, detail={"code": "budget_exhausted"})
        return None

    async def submit(self, product_id, project_id, service_id, run_input, idempotency_key, *, replay_only=False):
        if replay_only:
            existing = await self.db.get(
                RunReceipt, (self.actor.org_id, product_id, project_id, service_id, self.actor.user_id, "submit", idempotency_key)
            )
            if existing is None:
                raise HTTPException(409, detail={"code": "l5_effect_existing_replay_required"})
        if service_id == "finops" and getattr(self, "finops_authority", None) is None:
            raise HTTPException(409, detail={"code": "native_unqualified"})
        if service_id == "finops" and not finops_admissions_enabled():
            raise HTTPException(409, detail={"code": "finops_admissions_disabled"})
        if service_id in {"devops", "transform"} and self.actor.account_type == "human":
            receipt = await self.db.get(
                RunReceipt,
                (
                    self.actor.org_id,
                    product_id,
                    project_id,
                    service_id,
                    self.actor.user_id,
                    "submit",
                    idempotency_key,
                ),
            )
            if receipt is not None:
                configuration, _ = await self.current(product_id, project_id, service_id)
                run = await self.db.get(Run, (self.actor.org_id, receipt.run_id))
                if (
                    configuration.owner_id != self.actor.user_id
                    or run is None
                    or run.service_id != service_id
                    or run.configuration_id != configuration.configuration_id
                    or run.product_id != product_id
                    or run.project_id != project_id
                ):
                    raise HTTPException(404, detail={"code": "not_found"})
                if (service_id == "transform" or run.input.get("capability") == "release_readiness") and not await self.run_source_rights(
                    project_id, [run]
                ):
                    raise HTTPException(404, detail={"code": "not_found"})
                digest = hashlib.sha256(
                    json.dumps(
                        {
                            "input": run_input,
                            "event_id": None,
                            "exclusions": run.admission["exclusions"],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                if digest != receipt.request_hash or run.input != run_input:
                    raise HTTPException(409, detail={"code": "idempotency_conflict"})
                return run
        self.admission_key = idempotency_key
        self.admission_operation = "submit"
        self.previous_delegation_id = None
        verified, source_client = await self.admission_authority(product_id, project_id, service_id, run_input)
        reader = self.admission_reader(verified, run_input)
        return await submit_and_dispatch(
            self.db,
            self.sessions,
            source_client,
            self.publish,
            run_input,
            verified,
            idempotency_key,
            budget_guard=self._require_devops_budget,
            verification_reader=reader,
            **({"replay_only": True} if replay_only else {}),
        )

    def admission_reader(self, verified, run_input):
        reader = self.verification_reader
        if getattr_static(reader, "bind_admission", None) is not None:
            return reader.bind_admission(verified, run_input)
        return reader

    async def run_store(self, product_id, project_id, service_id):
        if service_id == "transform":
            await self.current(product_id, project_id, service_id)
        else:
            await self._authorize_product_scope(product_id, project_id)
        relationship = await self.relationship(product_id, project_id)
        if relationship is None:
            raise HTTPException(404, detail={"code": "not_found"})
        return RunStore(self.db, self.actor.org_id, product_id, project_id, service_id, self.actor.user_id, str(relationship.relationship_revision))

    async def run_source_rights(self, project_id, runs):
        from src.l5_service_identity import worker_id

        allowed = await _product_call(permitted_source_refs(self.db, self.actor, project_id))
        finops_runs = [run for run in runs if run.service_id == "finops"]
        transform_runs = [run for run in runs if run.service_id == "transform"]
        scoped_runs = finops_runs + transform_runs
        scoped_bindings = {}
        if scoped_runs:
            rows = (
                await self.db.scalars(
                    select(Binding).where(
                        Binding.tenant_id == self.actor.org_id,
                        Binding.binding_id.in_([run.binding_id for run in scoped_runs]),
                    )
                )
            ).all()
            scoped_bindings = {row.binding_id: row for row in rows}
            grants = (
                await self.db.scalars(
                    select(CredentialServiceDelegation).where(
                        CredentialServiceDelegation.id.in_(
                            [(run.admission or {}).get("delegation_id") for run in scoped_runs if (run.admission or {}).get("delegation_id")]
                        ),
                        CredentialServiceDelegation.tenant_id == self.actor.org_id,
                        CredentialServiceDelegation.service_id.in_((worker_id("finops"), worker_id("transform"))),
                        CredentialServiceDelegation.revoked_at.is_(None),
                        CredentialServiceDelegation.expires_at > datetime.now(UTC),
                    )
                )
            ).all()
            scoped_grants = {grant.id: grant for grant in grants}
        permitted_connections = set()
        if scoped_runs and self.actor.account_type == "human":
            connections = await _product_call(authorized_connections(self.db, self.actor))
            permitted_connections = {item["connection_ref"]["id"] for item, _ in connections if item.get("provider") == "aws"}

        def finops_connection_allowed(run):
            if run.service_id != "finops":
                return True
            binding = scoped_bindings.get(run.binding_id)
            connection = (run.admission or {}).get("connection_ref") or {}
            grant = scoped_grants.get((run.admission or {}).get("delegation_id"))
            return (
                self.actor.account_type == "human"
                and binding is not None
                and grant is not None
                and grant.service_id == worker_id("finops")
                and binding.service_id == "finops"
                and binding.state == "ready"
                and binding.generation == run.binding_generation
                and (binding.tenant_id, binding.product_id, binding.project_id) == (self.actor.org_id, run.product_id, project_id)
                and binding.connection_ref == connection
                and connection.get("record_type") == "connection"
                and connection.get("record_id") == connection.get("source_connection_id")
                and connection.get("record_id") in permitted_connections
                and (grant.product_id, grant.project_id, grant.credential_id) == (run.product_id, project_id, connection["record_id"])
                and grant.binding_generation == binding.generation
                and "credential:session" in grant.operations
                and "cost_inquiry" in grant.capabilities
                and {**connection, "record_type": "credential"} in grant.sources
            )

        def transform_connection_allowed(run):
            if run.service_id != "transform":
                return True
            binding = scoped_bindings.get(run.binding_id)
            admission = run.admission or {}
            connection = admission.get("connection_ref") or {}
            grant = scoped_grants.get(admission.get("delegation_id")) if admission.get("delegation_id") else None
            return (
                self.actor.account_type == "human"
                and run.tenant_id == self.actor.org_id
                and binding is not None
                and grant is not None
                and grant.service_id == worker_id("transform")
                and binding.service_id == "transform"
                and binding.state == "ready"
                and binding.generation == run.binding_generation
                and (binding.tenant_id, binding.product_id, binding.project_id) == (self.actor.org_id, run.product_id, project_id)
                and binding.connection_ref == connection
                and connection.get("record_type") == "connection"
                and connection.get("record_id") == connection.get("source_connection_id")
                and connection.get("record_id") in permitted_connections
                and (grant.product_id, grant.project_id, grant.credential_id, grant.created_by)
                == (run.product_id, project_id, connection["record_id"], self.actor.user_id)
                and grant.binding_generation == binding.generation
                and grant.revision == admission.get("delegation_version")
                and "credential:session" in grant.operations
                and run.input.get("capability") in grant.capabilities
                and {**connection, "record_type": "credential"} in grant.sources
            )

        return [
            run
            for run in runs
            if finops_connection_allowed(run)
            and transform_connection_allowed(run)
            and all(
                isinstance(source, dict)
                and source.get("tenant_id") == self.actor.org_id
                and source.get("project_id") == project_id
                and (source.get("source_connection_id"), source.get("record_id")) in allowed
                for source in (
                    [run.input.get("devops_input", {}).get("repository_ref")]
                    if run.service_id == "devops" and run.input.get("capability") == "release_readiness"
                    else run.input.get("sources", [])
                )
            )
        ]

    async def get_run(self, product_id, project_id, service_id, run_id):
        store = await self.run_store(product_id, project_id, service_id)
        run = await store.get(run_id)
        if run not in await self.run_source_rights(project_id, [run]):
            raise HTTPException(404, detail={"code": "not_found"})
        return run

    async def wa_native_observations(self, product_id, project_id, run_id):
        from src.l5_backend_well_architected import WellArchitectedService
        from src.l5_wa_observations import read_native_observations

        return await read_native_observations(WellArchitectedService(self), product_id, project_id, run_id)

    async def wa_qualification_prerequisites(self, product_id, project_id):
        from src.l5_wa_observations import read_prerequisites

        return await read_prerequisites(self, product_id, project_id)

    async def wa_cleanup_observations(self, product_id, project_id, run_id):
        from src.l5_backend_well_architected import WellArchitectedService
        from src.l5_wa_cleanup_observations import read_cleanup_observations

        return await read_cleanup_observations(WellArchitectedService(self), product_id, project_id, run_id)

    async def source_manifest(self, product_id, project_id, service_id, run_id, *, limit=None, cursor=None):
        from src.products.l5_retained_sources import manifest_response

        return await manifest_response(self, product_id, project_id, service_id, run_id, limit=limit, cursor=cursor)

    async def source_snapshot(self, product_id, project_id, service_id, run_id, snapshot_id, part, *, range_header=None):
        from src.products.l5_retained_sources import snapshot_response

        return await snapshot_response(self, product_id, project_id, service_id, run_id, snapshot_id, part, range_header=range_header)

    async def native_security_readback(self, product_id, project_id, run_id, action, *, owned_binding_id=None):
        from src.products.l5_security_readback import read_native

        return await read_native(self, product_id, project_id, run_id, action, owned_binding_id=owned_binding_id)

    async def security_recovery_state(self, product_id, project_id, run_id):
        from src.products.l5_security_readback import read_recovery_state

        return await read_recovery_state(self, product_id, project_id, run_id)

    async def devops_recovery_state(self, product_id, project_id, run_id):
        from src.products.l5_devops_recovery import read_recovery_state

        return await read_recovery_state(self, product_id, project_id, run_id)

    async def security_publication(self, product_id, project_id, run_id, publication_id):
        from src.products.l5_security_readback import read_publication

        return await read_publication(self, product_id, project_id, run_id, publication_id)

    async def security_submission_receipt(self, product_id, project_id, receipt_id):
        from src.products.l5_security_readback import read_submission_receipt

        return await read_submission_receipt(self, product_id, project_id, receipt_id)

    async def list_runs(self, product_id, project_id, service_id, cursor=None):
        store = await self.run_store(product_id, project_id, service_id)
        return await self.run_source_rights(project_id, await store.list(cursor=cursor))

    async def contribution_authority(self, run, publication):
        """Recheck a copied result using a fresh session, never the selection cache.

        This authorizes only the existing human metadata reader. It grants no
        consumer admission, service-account access or later-task qualification.
        """
        if self.actor.account_type != "human" or run.service_id not in {"devops", "finops", "transform"} or run.tenant_id != self.actor.org_id:
            return False
        async with self.sessions() as current:
            backend = GatewayConfigurationBackend(current, self.actor)
            latest = await backend.get_run(run.product_id, run.project_id, run.service_id, run.run_id)
            if any(
                getattr(latest, field) != getattr(run, field)
                for field in (
                    "tenant_id",
                    "product_id",
                    "project_id",
                    "service_id",
                    "binding_id",
                    "binding_generation",
                    "configuration_id",
                    "configuration_revision",
                    "scope",
                    "input",
                    "admission",
                )
            ):
                return False
            grant = await current.get(CredentialServiceDelegation, (latest.admission or {}).get("delegation_id"))
            if grant is None or not await current_l5_parent(current, grant):
                return False
            stored = await current.get(Publication, (run.tenant_id, publication.publication_id))
            return (
                stored is not None
                and stored.state == "published"
                and stored.invalidation_watermark == 0
                and stored.receipt is not None
                and all(
                    getattr(stored, field) == getattr(publication, field)
                    for field in (
                        "result_id",
                        "result_version",
                        "destination",
                        "schema_version",
                        "contribution_hash",
                        "payload",
                        "receipt",
                        "revision",
                    )
                )
            )

    async def result_store(self, product_id, project_id, service_id):
        if service_id == "transform" and not self.transform_result_reader_ready:
            raise HTTPException(503, detail={"code": "capability_unavailable"})

        runs = await self.list_runs(product_id, project_id, service_id)
        consumers = {}
        if service_id == "well_architected" and runs and self.actor.account_type == "human" and callable(self.wa_consumer_resolver):
            scope = {"tenant_id": self.actor.org_id, "product_id": product_id, "project_id": project_id}
            resolved = self.wa_consumer_resolver(scope)
            if isinstance(resolved, dict):
                for destination in ("product-context", "engineering-insights"):
                    client = resolved.get(destination)
                    if (
                        isinstance(client, WellArchitectedConsumerClient)
                        and client.destination == destination
                        and client.scope == scope
                        and client.reuse_path is not None
                    ):
                        consumers[destination] = client

        async def wa_source_authority(run):
            return run in await self.run_source_rights(project_id, [run])

        async def task_authority(run, task_id, actor_id):
            from src.l5_consumer_authority import task_authorized

            if actor_id != self.actor.user_id or self.actor.account_type != "human":
                return False
            async with self.sessions() as current:
                return await task_authorized(current, self.actor, run, task_id)

        async def wa_reuse_authority(run, result_id):
            from src.l5_consumer_authority import current as current_consumer

            async with self.sessions() as current:
                publication = await current.scalar(
                    select(Publication).where(
                        Publication.tenant_id == run.tenant_id,
                        Publication.result_id == result_id,
                        Publication.destination == "product-context",
                    )
                )
                if publication is None:
                    return False
                try:
                    await current_consumer(
                        current,
                        self.actor,
                        tenant_id=run.tenant_id,
                        contribution_id=publication.publication_id,
                        destination="product-context",
                        action="read",
                    )
                except HTTPException:
                    return False
                return True

        return ResultStore(
            self.db,
            runs,
            actor_id=self.actor.user_id if self.actor.account_type == "human" else None,
            transform_artifact_store=self.transform_artifact_store if service_id == "transform" else None,
            finops_answer_reader=self.finops_answer_reader if service_id == "finops" else None,
            contribution_authority=self.contribution_authority if service_id in {"devops", "finops", "transform"} else None,
            service_id=service_id,
            wa_source_authority=wa_source_authority if service_id == "well_architected" else None,
            wa_context_client=consumers.get("product-context"),
            wa_insights_client=consumers.get("engineering-insights"),
            task_authority=task_authority if service_id == "well_architected" else None,
            wa_reuse_authority=wa_reuse_authority if service_id == "well_architected" else None,
            artifact_authority={"tenant_id": self.actor.org_id, "actor_id": self.actor.user_id, "product_id": product_id, "project_id": project_id}
            if service_id == "transform"
            else None,
        )

    async def cancel_run(self, product_id, project_id, service_id, run_id, reason, idempotency_key):
        run = await self.get_run(product_id, project_id, service_id, run_id)
        store = await self.run_store(product_id, project_id, service_id)
        if service_id == "devops" and run.origin == "native":
            if self.actor.account_type != "human":
                raise HTTPException(404, detail={"code": "not_found"})
            return await store.cancel_native(run_id, reason, idempotency_key)
        return await store.cancel_queued(run_id, reason, idempotency_key)

    async def reconcile_run(self, product_id, project_id, service_id, run_id, reason, idempotency_key):
        await self.get_run(product_id, project_id, service_id, run_id)
        store = await self.run_store(product_id, project_id, service_id)
        return await reconcile_and_dispatch(store, self.sessions, self.publish, run_id, reason, idempotency_key)

    async def recover_devops_report(self, product_id, project_id, run_id, request, idempotency_key):
        from src.l5_devops_report_recovery import recover_report

        return await recover_report(self, product_id, project_id, run_id, request, idempotency_key)

    async def retry_run(self, product_id, project_id, service_id, run_id, reason, idempotency_key):
        await self.get_run(product_id, project_id, service_id, run_id)
        store = await self.run_store(product_id, project_id, service_id)
        original = await store.owned(run_id)
        receipt = await self.db.get(RunReceipt, store.receipt_key("retry", idempotency_key))
        if receipt is None:
            await store.retryable(run_id)
        self.admission_key = idempotency_key
        self.admission_operation = "retry"
        self.previous_delegation_id = original.admission.get("delegation_id") if service_id in {"security", "devops", "finops"} else None
        verified, source_client = await self.admission_authority(product_id, project_id, service_id, original.input)
        if isinstance(source_client, GatewayAdmissionSourceClient):
            source_client.retry_context = {"run_id": run_id, "reason": reason}
        return await retry_and_dispatch(
            self.db,
            self.sessions,
            source_client,
            self.publish,
            verified,
            run_id,
            reason,
            idempotency_key,
            budget_guard=self._require_devops_budget,
            verification_reader=self.admission_reader(verified, original.input),
        )

    async def _authorize_product_scope(self, product_id: str, project_id: str) -> None:
        grant, _, _ = await _product_call(
            product_routes.scoped_product(
                self.db,
                self.actor,
                product_id,
                project_id,
                self.delegation_id,
            )
        )
        if self.actor.account_type == "service":
            self.grant = grant
            self.store = ConfigurationStore(self.db, grant.tenant_id, grant.created_by)

    async def profile(self, product_id: str, project_id: str) -> dict:
        if self.actor.account_type == "service":
            await self._authorize_product_scope(product_id, project_id)
        return await _product_call(
            product_routes.get_profile(
                product_id,
                project_id,
                Response(),
                self.db,
                self.actor,
                self.delegation_id,
            )
        )

    async def relationship(self, product_id: str, project_id: str):
        return await self.db.scalar(
            select(ProductProjectBinding).where(
                ProductProjectBinding.tenant_id == self.store.tenant_id,
                ProductProjectBinding.product_id == product_id,
                ProductProjectBinding.project_id == project_id,
                ProductProjectBinding.valid_to.is_(None),
            )
        )

    async def current(self, product_id, project_id, service_id, *, repair_access_policy=False):
        await self.profile(product_id, project_id)
        configuration, version = await self.store.current(product_id, project_id, service_id)
        relationship = await self.relationship(product_id, project_id)
        if relationship is None or configuration.relationship_revision != str(relationship.relationship_revision):
            raise HTTPException(404, detail={"code": "not_found"})
        if repair_access_policy and service_id == "devops":
            from src.l5_devops_access_policy import without_policy

            await self.authorize_settings(without_policy(version.settings), project_id, service_id)
        else:
            await self.authorize_sources(version, project_id)
        return configuration, version

    async def source_rights(self, project_id):
        if self.actor.account_type == "service":
            if self.grant is None or self.grant.project_id != project_id:
                raise HTTPException(404, detail={"code": "not_found"})
            return {
                (binding.installation_id or binding.credential_id, binding.native_id)
                for binding in await service_visible_bindings(self.db, self.grant)
            }
        return await _product_call(permitted_source_refs(self.db, self.actor, project_id))

    async def authorize_sources(self, version, project_id):
        await self.authorize_settings(version.settings, project_id, version.settings.get("service_id"), provider_readback=False)

    async def configuration_connections(self, project_id):
        connections = await _product_call(authorized_connections(self.db, self.actor))
        scoped = []
        for descriptor, credential in connections:
            if descriptor.get("provider") == "aws" and isinstance(credential, UserCredential):
                await self.db.refresh(credential)
                try:
                    reference = _verified_aws_connection_ref(credential, self.store.tenant_id, project_id)
                except HTTPException:
                    continue
                descriptor = {**descriptor, "record_ref": reference}
            scoped.append((descriptor, credential))
        return scoped

    async def authorize_settings(self, settings, project_id, service_id=None, *, provider_readback=True):
        provider_refs = []
        if service_id == "well_architected":
            from src.l5_wa_provider_selection import stored_provider_references, verify_provider_references

            provider_refs = (
                await verify_provider_references(self, project_id, settings) if provider_readback else stored_provider_references(settings)
            )
        allowed = await self.source_rights(project_id)
        for reference in settings.get("source_refs") or []:
            if (
                not isinstance(reference, dict)
                or reference.get("tenant_id") != self.store.tenant_id
                or reference.get("project_id") != project_id
                or (reference.get("source_connection_id"), reference.get("record_id")) not in allowed
            ):
                raise HTTPException(404, detail={"code": "not_found"})
        connection = settings.get("connection_ref")
        if connection is not None and self.actor.account_type == "human":
            connections = await _product_call(authorized_connections(self.db, self.actor))
            available = {
                "coverage": "complete",
                "next_cursor": None,
                "items": [
                    projected
                    for item, candidate in connections
                    if (
                        projected := _project_connection(
                            item, candidate, allowed, self.store.tenant_id, project_id, source_free=service_id == "finops"
                        )
                    )
                ],
            }
            if connection not in authorized_connection_refs(available, self.store.tenant_id, project_id):
                raise HTTPException(404, detail={"code": "not_found"})
        transform = settings.get("transform")
        if transform is not None:
            if not isinstance(transform, dict):
                raise HTTPException(422, detail={"code": "validation_error"})
            selected = transform.get("connection_ref")
            if not isinstance(selected, dict):
                raise HTTPException(404, detail={"code": "not_found"})
            for field in ("connection_ref", "executor_binding_ref", "test_policy_ref", "definition_ref"):
                reference = transform.get(field)
                if reference is not None and (
                    not isinstance(reference, dict) or reference.get("tenant_id") != self.store.tenant_id or reference.get("project_id") != project_id
                ):
                    raise HTTPException(404, detail={"code": "not_found"})
            if self.actor.account_type == "human":
                connections = await _product_call(authorized_connections(self.db, self.actor))
                if selected not in [
                    projected.get("record_ref")
                    for item, candidate in connections
                    if item.get("provider") == "aws"
                    if (projected := _project_connection(item, candidate, allowed, self.store.tenant_id, project_id))
                ]:
                    raise HTTPException(404, detail={"code": "not_found"})
                credential = await self.db.get(UserCredential, selected.get("record_id"))
                if (
                    credential is None
                    or credential.user_id != self.actor.user_id
                    or credential.org_id != self.store.tenant_id
                    or credential.service != "aws"
                    or credential.credential_type != "aws_role"
                    or credential.id != selected.get("source_connection_id")
                    or credential.aws_verified_version_id != selected.get("record_revision")
                ):
                    raise HTTPException(404, detail={"code": "not_found"})
                try:
                    verified_connection_evidence(credential)
                except HTTPException:
                    raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from None
                account = (credential.scopes or {}).get("account_id")
                role = (credential.scopes or {}).get("role_arn")
                if (
                    not isinstance(account, str)
                    or re.fullmatch(r"[0-9]{12}", account) is None
                    or not isinstance(role, str)
                    or re.fullmatch(rf"arn:aws:iam::{account}:role/.+", role) is None
                ):
                    raise HTTPException(503, detail={"code": "selected_connection_unavailable"})
        devops = settings.get("devops")
        well_architected = settings.get("well_architected")
        if devops is not None or well_architected is not None:
            connections = await _product_call(authorized_connections(self.db, self.actor)) if self.actor.account_type == "human" else []
            available = {
                "coverage": "complete",
                "next_cursor": None,
                "items": [
                    projected
                    for item, candidate in connections
                    if (projected := _project_connection(item, candidate, allowed, self.store.tenant_id, project_id))
                ],
            }
            connection_refs = authorized_connection_refs(available, self.store.tenant_id, project_id)
            selected = devops if devops is not None else well_architected
            if devops is not None and devops.get("access_policy_ref") is not None and provider_readback:
                from src.l5_devops_access_policy import resolve_access_policy

                await resolve_access_policy(self, project_id=project_id, reference=devops["access_policy_ref"], settings=settings)
                selected = {key: value for key, value in devops.items() if key != "access_policy_ref"}
            resource_refs = []
            if devops is not None:
                from src.l5_devops_resources import require_workload_refs, stored_workload_refs

                resource_refs = (
                    await require_workload_refs(
                        self.db,
                        tenant_id=self.store.tenant_id,
                        project_id=project_id,
                        owner_id=self.actor.user_id if self.actor.account_type == "human" else self.store.owner_id,
                        settings=settings,
                    )
                    if provider_readback
                    else stored_workload_refs(settings)
                )
            for reference in _record_refs(selected):
                if reference.get("tenant_id") != self.store.tenant_id or reference.get("project_id") != project_id:
                    raise HTTPException(404, detail={"code": "not_found"})
                if reference in resource_refs:
                    continue
                if reference in provider_refs:
                    permitted = True
                elif reference.get("record_type") == "connection":
                    permitted = (
                        reference in connection_refs
                        if self.actor.account_type == "human"
                        else reference.get("source_connection_id") == reference.get("record_id")
                        and reference.get("record_id") in {connection_id for connection_id, _ in allowed}
                    )
                else:
                    permitted = (reference.get("source_connection_id"), reference.get("record_id")) in allowed
                if not permitted:
                    raise HTTPException(404, detail={"code": "not_found"})
        return provider_refs

    async def devops_resources(self, product_id, project_id, binding_id):
        from src.l5_devops_resources import resource_options

        config, version = await self.current(product_id, project_id, "devops")
        if self.actor.account_type != "human" or config.owner_id != self.actor.user_id:
            raise HTTPException(404, detail={"code": "not_found"})
        binding = await self.db.get(Binding, (self.store.tenant_id, binding_id))
        if binding is None or binding.configuration_id != config.configuration_id:
            raise HTTPException(404, detail={"code": "not_found"})
        options = await resource_options(
            self.db, tenant_id=self.store.tenant_id, project_id=project_id, owner_id=config.owner_id, settings=version.settings, binding_id=binding_id
        )
        if len(options) != 1:
            raise HTTPException(409, detail={"code": "resource_readback_unverified"})
        return options[0]

    async def devops_access_policy(self, product_id, project_id, binding_id):
        from src.l5_devops_access_policy import resolve_access_policy

        return await resolve_access_policy(self, product_id=product_id, project_id=project_id, binding_id=binding_id)

    async def get(self, product_id, project_id, service_id):
        return view(*await self.current(product_id, project_id, service_id))

    async def versions(self, product_id, project_id, service_id):
        configuration, _ = await self.current(product_id, project_id, service_id)
        versions = await self.db.scalars(
            select(ConfigurationVersion)
            .where(
                ConfigurationVersion.tenant_id == self.store.tenant_id,
                ConfigurationVersion.configuration_id == configuration.configuration_id,
                ConfigurationVersion.revision <= configuration.current_revision,
            )
            .order_by(ConfigurationVersion.revision.desc())
        )
        visible = []
        for historical in versions:
            if (
                self.actor.account_type == "human"
                and configuration.owner_id != self.actor.user_id
                and historical.settings.get("visibility") != "product_team"
            ):
                continue
            try:
                await self.authorize_sources(historical, project_id)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
                continue
            visible.append(view(configuration, historical))
        return visible

    async def version(self, product_id, project_id, service_id, revision):
        configuration, _ = await self.current(product_id, project_id, service_id)
        historical = await self.db.get(ConfigurationVersion, (self.store.tenant_id, configuration.configuration_id, revision))
        if historical is None or (
            self.actor.account_type == "human"
            and configuration.owner_id != self.actor.user_id
            and historical.settings.get("visibility") != "product_team"
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        await self.authorize_sources(historical, project_id)
        return view(configuration, historical)

    async def transform_readiness(self, settings, product_id, project_id):
        selected = (settings.get("transform") or {}).get("executor_binding_ref")
        qualified = None
        if selected is not None and self.transform_qualification_reader is not None:
            try:
                observation = await self.transform_qualification_reader(self.store.tenant_id, product_id, project_id, selected)
            except (TimeoutError, httpx.TransportError):
                observation = None
            if isinstance(observation, dict) and observation.get("status") == "qualified":
                reference = observation.get("executor_binding_ref")
                if (
                    isinstance(reference, dict)
                    and set(reference) == {"tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision"}
                    and all(
                        reference[field] == selected[field]
                        for field in ("tenant_id", "project_id", "source_connection_id", "record_type", "record_id")
                    )
                    and isinstance(reference["record_revision"], str)
                    and reference["record_revision"] not in {"", "HEAD", "latest", "null"}
                ):
                    qualified = reference
        readiness = transform_capability_readiness(settings, qualified_executor_ref=qualified)
        for item in readiness:
            if item["status"] == "ready" and not self.transform_submission_ready:
                item["status"] = "unqualified"
                item["gaps"] = [{"code": "submission_unavailable", "field": "transform", "owner": "executor", "affected_step": "execute"}]
        return readiness

    async def transform_configuration_readiness(self, configuration, version, product_id, project_id):
        readiness = await self.transform_readiness(version.settings, product_id, project_id)
        gaps = list(
            dict.fromkeys(
                [
                    gap
                    for gap in version.gaps
                    if gap
                    not in {
                        "test_policy_ref_missing",
                        "executor_binding_ref_missing",
                        "executor_unqualified",
                    }
                ]
                + [gap["code"] for item in readiness for gap in item["gaps"]]
            )
        )
        return {
            "ready": (
                version.settings.get("enabled") is True
                and configuration.handoff_state == "linked"
                and bool(readiness)
                and all(item["status"] == "ready" for item in readiness)
                and not gaps
            ),
            "gaps": gaps,
            "readiness": readiness,
            "provider_contacted": False,
        }

    async def check(self, product_id, project_id, service_id):
        configuration, version = await self.current(product_id, project_id, service_id)
        result = {"ready": version.state == "ready" and configuration.handoff_state == "linked", "gaps": version.gaps, "provider_contacted": False}
        if service_id in {"security", "finops", "well_architected"}:
            result = await project_readiness(self.db, configuration, version, service_id, await self.profile(product_id, project_id))
            if service_id == "security" and not callable(self.publish):
                result["gaps"].append("work_queue_unavailable")
            if service_id == "finops":
                if not finops_admissions_enabled():
                    result["gaps"].append("finops_admissions_disabled")
                if not callable(self.publish):
                    result["gaps"].append("work_queue_unavailable")
                if not callable(self.finops_authority):
                    result["gaps"].append("finops_authority_unavailable")
            if service_id == "well_architected" and (
                not callable(self.publish) or not callable(self.wa_authority) or self.wa_profile_client is None or not callable(self.wa_qualify)
            ):
                result["gaps"].append("provider_unqualified")
            result["gaps"] = list(dict.fromkeys(result["gaps"]))
            result["ready"] = not result["gaps"]
            return result
        if service_id == "transform":
            return await self.transform_configuration_readiness(configuration, version, product_id, project_id)
        if service_id == "devops":
            if "investigation" in version.settings.get("capabilities", []):
                from src.l5_devops_readiness import investigation_readiness

                result = await investigation_readiness(self, configuration, version)
            observations = []
            if self.devops_integration_observer is not None and (version.settings.get("devops") or {}).get("integrations"):
                observations = await self.devops_integration_observer(
                    self.store.tenant_id, product_id, project_id, version.settings, version.revision
                )
                result["provider_contacted"] = True
            result["integrations"] = integration_readiness(version.settings, observations, configuration_revision=version.revision)
            integrations_ready = all(
                item["status"] == "ready" and not item.get("events", {}).get("requested_mode_unavailable") for item in result["integrations"]
            )
            if not integrations_ready:
                result["gaps"] = list(dict.fromkeys([*result["gaps"], "integration_not_ready"]))
            result["ready"] = result["ready"] and integrations_ready
        return result

    async def options(self, product_id, project_id, service_id):
        profile = await self.profile(product_id, project_id)
        _, _, available = await self.profile_inputs(profile, product_id, project_id, service_id)
        options = configuration_options(profile, available, service_id)
        if service_id == "finops":
            # The existing switch admits inquiries, not adoption setup. A true
            # value is permission to check readiness, never runtime qualification.
            options["admissions_enabled"] = finops_admissions_enabled()
        if service_id == "transform":
            try:
                _, version = await self.current(product_id, project_id, service_id)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
            else:
                options["readiness"] = await self.transform_readiness(version.settings, product_id, project_id)
                options["unavailable_reasons"] = list(
                    dict.fromkeys(
                        [
                            reason
                            for reason in options["unavailable_reasons"]
                            if reason
                            not in {
                                "test_policy_ref_missing",
                                "executor_binding_ref_missing",
                                "executor_unqualified",
                            }
                        ]
                        + [gap["code"] for item in options["readiness"] for gap in item["gaps"]]
                    )
                )
        if service_id == "well_architected" and options["capabilities"]:
            try:
                checked = await self.check(product_id, project_id, service_id)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
            else:
                if checked["ready"]:
                    options["unavailable_reasons"] = [reason for reason in options["unavailable_reasons"] if reason != "provider_unqualified"]
        return options

    async def handoff(self, configuration, version, product_id, project_id, idempotency_key):
        product_capabilities = (
            [configuration.service_id] if configuration.service_id in {"devops", "finops", "well_architected"} else version.settings["capabilities"]
        )
        relationship = await self.relationship(product_id, project_id)
        if relationship is None or configuration.relationship_revision != str(relationship.relationship_revision):
            raise HTTPException(404, detail={"code": "not_found"})
        profile_ref = version.settings["profile_ref"]
        if profile_ref["source_connection_id"] != relationship.source_connection_id:
            raise HTTPException(404, detail={"code": "not_found"})
        reference = {
            "tenant_id": self.store.tenant_id,
            "project_id": project_id,
            "source_connection_id": relationship.source_connection_id,
            "record_type": "l5_configuration",
            "record_id": configuration.configuration_id,
            "record_revision": str(version.revision),
        }

        async def read_profile():
            return await self.profile(product_id, project_id)

        async def patch_profile(patch, revision, key):
            request_key = hashlib.sha256(f"{key}:{revision}".encode()).hexdigest()
            await link_l5_configuration(
                self.db,
                self.actor,
                product_id,
                project_id,
                reference,
                product_capabilities,
                revision,
                request_key,
            )

        key = hashlib.sha256(f"{self.actor.org_id}:{configuration.configuration_id}:{idempotency_key}".encode()).hexdigest()
        try:
            configuration.handoff_state = await link_configuration(
                read_profile,
                patch_profile,
                reference,
                product_capabilities,
                key,
            )
        except ValueError:
            configuration.handoff_state = "pending"
        await self.db.commit()
        return view(configuration, version)

    async def retry_handoff(self, product_id, project_id, service_id, idempotency_key, if_match):
        if self.actor.account_type != "human":
            raise HTTPException(404, detail={"code": "not_found"})
        configuration, version = await self.current(product_id, project_id, service_id)
        if if_match != f'"{version.hash}"':
            raise HTTPException(412, detail={"code": "stale_revision"})
        return await self.handoff(configuration, version, product_id, project_id, idempotency_key)

    async def profile_inputs(self, profile, product_id, project_id, service_id="security"):
        relationship = await self.relationship(product_id, project_id)
        if relationship is None:
            raise HTTPException(404, detail={"code": "not_found"})
        profile_ref = {
            "tenant_id": self.actor.org_id,
            "project_id": project_id,
            "source_connection_id": relationship.source_connection_id,
            "record_type": "product",
            "record_id": product_id,
            "record_revision": str(profile["revision"]),
        }
        allowed = await _product_call(permitted_source_refs(self.db, self.actor, project_id))
        connections = []
        for source, credential in await _product_call(authorized_connections(self.db, self.actor)):
            projected = _project_connection(source, credential, allowed, self.store.tenant_id, project_id, source_free=service_id == "finops")
            if projected is None:
                continue
            connections.append(projected)
        configuration = profile.get("configuration") or {}
        if service_id == "devops":
            candidates = (configuration.get("advanced") or {}).get("environments") or []
        elif service_id == "finops":
            candidates = []
        else:
            basic = configuration.get("basic") or {}
            candidates = [reference for field in ("repositories", "documents") for reference in basic.get(field) or []]
        environments = (configuration.get("advanced") or {}).get("environments") or []
        regions = [
            reference["record_id"]
            for reference in environments
            if isinstance(reference, dict)
            and reference.get("record_type") == "environment"
            and (reference.get("source_connection_id"), reference.get("record_id")) in allowed
            and any(
                connection.get("record_ref", {}).get("record_revision") == reference.get("record_revision")
                and connection["connection_ref"]["id"] == reference["source_connection_id"]
                for connection in connections
            )
        ]
        available = {
            "coverage": "complete",
            "next_cursor": None,
            "regions": list(dict.fromkeys(regions)),
            "items": [
                {
                    "connection_ref": connection["connection_ref"],
                    "record_ref": connection.get("record_ref"),
                    "resources": [
                        reference
                        for reference in candidates
                        if (reference.get("source_connection_id"), reference.get("record_id")) in allowed
                        and reference.get("source_connection_id") == connection["connection_ref"]["id"]
                    ],
                }
                for connection in connections
            ],
        }
        return relationship, profile_ref, available

    async def refresh(self, product_id, project_id, service_id, profile_ref, idempotency_key, if_match, *, choices=None):
        if self.actor.account_type != "human":
            raise HTTPException(404, detail={"code": "not_found"})
        configuration, previous = await self.current(product_id, project_id, service_id, repair_access_policy=True)
        profile = await self.profile(product_id, project_id)
        _, current_ref, available = await self.profile_inputs(profile, product_id, project_id, service_id)
        if profile_ref != current_ref:
            raise HTTPException(412, detail={"code": "stale_profile_revision"})
        if choices is not None and service_id in {"devops", "finops", "transform"}:
            await self.authorize_settings({**previous.settings, **choices}, project_id, service_id)
        if service_id == "well_architected":
            from src.l5_wa_provider_selection import add_provider_resources

            provider_refs = await self.authorize_settings({**previous.settings, **(choices or {})}, project_id, service_id)
            add_provider_resources(available, provider_refs)
        configuration, version = await self.store.refresh(
            configuration,
            previous,
            profile,
            current_ref,
            available,
            if_match,
            choices=choices,
        )
        return await self.handoff(configuration, version, product_id, project_id, idempotency_key)

    async def save(self, product_id, project_id, service_id, settings, idempotency_key, if_none_match, if_match):
        if self.actor.account_type != "human":
            raise HTTPException(404, detail={"code": "not_found"})
        if if_none_match == "*":
            profile = await self.profile(product_id, project_id)
            relationship, profile_ref, available = await self.profile_inputs(profile, product_id, project_id, service_id)
            choices = {key: value for key, value in settings.items() if key != "profile_ref"}
            replay = await self.store.replay_create(product_id, project_id, service_id, idempotency_key, settings)
            if replay is not None:
                configuration, version = await self.current(product_id, project_id, service_id)
                if configuration.configuration_id != replay[0].configuration_id:
                    raise HTTPException(409, detail={"code": "idempotency_conflict"})
                await self.authorize_sources(replay[1], project_id)
                if service_id == "devops" and configuration.handoff_state != "linked" and version.revision == replay[1].revision:
                    return await self.handoff(configuration, version, product_id, project_id, idempotency_key)
                return view(*replay)
            if "profile_ref" in settings and settings["profile_ref"] != profile_ref:
                raise HTTPException(422, detail={"code": "invalid_profile_ref"})
            if service_id in {"devops", "finops", "well_architected", "transform"}:
                provider_refs = await self.authorize_settings(settings, project_id, service_id)
                if service_id == "well_architected":
                    from src.l5_wa_provider_selection import add_provider_resources

                    add_provider_resources(available, provider_refs)
            configuration, version = await self.store.create(
                product_id,
                project_id,
                service_id,
                relationship.relationship_revision,
                profile,
                profile_ref,
                available,
                choices,
                idempotency_key,
                request_settings=settings,
            )
        else:
            configuration, previous = await self.current(product_id, project_id, service_id, repair_access_policy=True)
            pinned_revision = int(previous.settings["profile_ref"]["record_revision"])
            profile = await _product_call(
                product_routes.get_revision(
                    product_id,
                    project_id,
                    pinned_revision,
                    Response(),
                    self.db,
                    self.actor,
                )
            )
            _, pinned_ref, available = await self.profile_inputs(profile, product_id, project_id, service_id)
            if pinned_ref != previous.settings["profile_ref"]:
                raise HTTPException(409, detail={"code": "refresh_required"})
            if service_id in {"devops", "finops", "well_architected", "transform"}:
                provider_refs = await self.authorize_settings(settings, project_id, service_id)
                if service_id == "well_architected":
                    from src.l5_wa_provider_selection import add_provider_resources

                    add_provider_resources(available, provider_refs)
            configuration, version = await self.store.edit(
                configuration,
                previous,
                profile,
                available,
                settings,
                if_match,
                **({"idempotency_key": idempotency_key} if service_id == "security" else {}),
            )
        return await self.handoff(configuration, version, product_id, project_id, idempotency_key)


async def publish_l5_work(envelope, delay):
    publish = service_publisher("devops")
    if publish is None:
        raise RuntimeError("L5 DevOps work queue is not configured")
    await publish(envelope, delay)


async def l5_actor(
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    """Use the same verified workspace user as Product and credential authority."""
    if actor.account_type != "human":
        return actor
    try:
        member = await product_routes.current_owner(db, actor)
    except HTTPException:
        raise HTTPException(404, detail={"code": "not_found"}) from None
    # Do not mutate the authentication context shared by other request handlers.
    # model_copy retains the private signature-verified subject used by SSO.
    return actor.model_copy(update={"user_id": member.id, "team_id": member.team_id})


def backend(
    service_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(l5_actor),
    x_l5_delegation_id: str | None = Header(None, alias="X-L5-Delegation-ID"),
    response: Response = None,
):
    publish = None
    if service_id in {"security", "well_architected"}:
        publish = service_publisher(service_id)
    elif service_id == "devops" and service_publisher("devops") is not None:
        publish = publish_l5_work
    elif service_id == "finops" and getattr(request.app.state, "l5_finops_authority", None) is not None:
        publish = service_publisher("finops")
    finops_answer_reader = None
    if service_id == "finops":
        import os

        bucket = os.environ.get("L5_ARCHIVE_BUCKET")
        if bucket:
            from l5_services.adapters.finops.answer_reader import FinOpsAnswerReader

            finops_answer_reader = FinOpsAnswerReader(bucket, boto3.client("s3"))
    selected = GatewayConfigurationBackend(
        db,
        actor,
        publish=publish,
        delegation_id=x_l5_delegation_id,
        devops_integration_observer=getattr(request.app.state, "l5_devops_integration_observer", None) if service_id == "devops" else None,
        release_authority=getattr(request.app.state, "l5_devops_release_authority", None) if service_id == "devops" else None,
        prevention_provider=getattr(request.app.state, "l5_devops_prevention_provider", None) if service_id == "devops" else None,
        wa_qualify=getattr(request.app.state, "l5_wa_qualify", None) if service_id == "well_architected" else None,
        finops_authority=getattr(request.app.state, "l5_finops_authority", None) if service_id == "finops" else None,
        finops_answer_reader=finops_answer_reader,
        wa_authority=getattr(request.app.state, "l5_wa_authority", None) if service_id == "well_architected" else None,
        wa_profile_client=getattr(request.app.state, "l5_wa_profile_client", None) if service_id == "well_architected" else None,
        wa_consumer_resolver=getattr(request.app.state, "l5_wa_consumer_resolver", None) if service_id == "well_architected" else None,
        verification_reader=getattr(request.app.state, "l5_devops_verification_reader", None) if service_id == "devops" else None,
        transform_qualification_reader=getattr(request.app.state, "l5_transform_executor_qualification_reader", None)
        if service_id == "transform"
        else None,
        transform_work_authority=getattr(request.app.state, "l5_transform_work_authority", None) if service_id == "transform" else None,
        transform_artifact_store=getattr(request.app.state, "l5_transform_artifact_store", None) if service_id == "transform" else None,
    )
    if service_id == "transform" and callable(selected.transform_work_authority):
        publish = getattr(request.app.state, "l5_transform_work_publisher", None) or service_publisher("transform")
        selected.publish = publish if callable(publish) else None
    from src.l5_observation_effects import HEADER, EffectBackend
    from src.products.security_evidence_effect import HEADER as SECURITY_HEADER
    from src.products.security_evidence_effect import SecurityEffectBackend

    if getattr(request, "scope", {}).get("headers") and request.headers.get(SECURITY_HEADER) is not None:
        if (
            request.headers.get(HEADER) is not None
            or service_id != "security"
            or request.method != "PUT"
            or not request.url.path.endswith("/configuration")
        ):
            raise HTTPException(422, detail={"code": "security_evidence_operation_unsupported"})
        return SecurityEffectBackend(selected, request, response)

    if getattr(request, "scope", {}).get("headers") and request.headers.get(HEADER) is not None:
        path = request.url.path
        allowed = (request.method == "PUT" and path.endswith("/configuration")) or (
            request.method == "POST" and path.endswith(("/configuration/check", "/runs", "/l5/mcp/tools/call"))
        )
        if not allowed:
            raise HTTPException(422, detail={"code": "l5_effect_operation_unsupported"})
        return EffectBackend(selected, request, response)
    return selected


def load_incident_secret():
    async def load(secret_ref):
        if (
            not isinstance(secret_ref, str)
            or re.fullmatch(r"arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+", secret_ref) is None
        ):
            raise HTTPException(503, detail={"code": "source_secret_unavailable"})
        try:
            helper = SecretsManagerHelper(region_name=get_settings().aws_region)
            return await run_in_threadpool(helper.get_secret, secret_ref)
        except Exception:
            raise HTTPException(503, detail={"code": "source_secret_unavailable"}) from None

    return load


def incident_work_backend():
    publish = publish_l5_work if service_publisher("devops") is not None else None

    def selected(db, receipt):
        actor = SimpleNamespace(org_id=receipt.tenant_id, user_id=None)
        return GatewayConfigurationBackend(db, actor, publish=publish)

    return selected


router = APIRouter()
router.include_router(create_router(backend))
router.include_router(create_provider_selection_router(backend))
router.include_router(create_owner_delegation_router(backend))
router.include_router(create_setup_continuation_router(backend))
router.include_router(create_source_preflight_router(backend))

router.include_router(create_wa_reuse_router(backend))
router.include_router(create_wa_qualification_router(backend))


router.include_router(create_security_evidence_router(backend))


class L5ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(pattern=r"^l5___[a-z][a-z0-9_]{0,63}$")
    arguments: dict


@router.post("/v1/l5/mcp/tools/list")
async def list_l5_mcp_tools(actor: TokenContext = Depends(get_current_user)):
    catalogue = available_l5_catalogue()
    return {
        "tools": [
            {
                "name": tool["name"].replace(".", "___", 1),
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
                "outputSchema": tool["outputSchema"],
                "annotations": tool["annotations"],
            }
            for tool in catalogue["tools"]
        ]
    }


@router.post("/v1/l5/mcp/tools/call")
async def call_l5_mcp_tool(
    body: L5ToolCall,
    request: Request,
    response: Response = None,
    actor: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # The internal agent facade also calls this function directly, so resolve
    # the same workspace owner as REST here rather than only as a dependency.
    actor = await l5_actor(db, actor)
    arguments = body.arguments
    if arguments.get("service_id") not in {"security", "devops", "finops", "well_architected", "transform"}:
        raise HTTPException(404, detail={"code": "not_found"})
    if body.tool not in {tool["name"].replace(".", "___", 1) for tool in available_l5_catalogue()["tools"]}:
        raise HTTPException(503, "L5 tool operation unavailable")
    operation = l5_http_request(body.tool, arguments)
    if getattr(request, "scope", {}).get("headers") and request.headers.get("X-ADP-Observation-Effect") is not None:
        method, path = operation[:2]
        if not ((method == "PUT" and path.endswith("/configuration")) or (method == "POST" and path.endswith(("/configuration/check", "/runs")))):
            raise HTTPException(422, detail={"code": "l5_effect_operation_unsupported"})
    delegation_id = None
    if actor.account_type == "service":
        if not actor.agent_registry_id:
            raise HTTPException(404, detail={"code": "not_found"})
        grants = (
            await db.scalars(
                select(CredentialServiceDelegation.id).where(
                    CredentialServiceDelegation.tenant_id == actor.org_id,
                    CredentialServiceDelegation.service_id == actor.agent_registry_id,
                    CredentialServiceDelegation.product_id == arguments.get("product_id"),
                    CredentialServiceDelegation.project_id == arguments.get("project_id"),
                    (
                        CredentialServiceDelegation.execution_scope["purpose"].as_string().is_(None)
                        | (CredentialServiceDelegation.execution_scope["purpose"].as_string() != "devops_native_ui")
                    ),
                    CredentialServiceDelegation.revoked_at.is_(None),
                    CredentialServiceDelegation.expires_at > datetime.now(UTC),
                )
            )
        ).all()
        if len(grants) != 1:
            raise HTTPException(404, detail={"code": "not_found"})
        delegation_id = grants[0]
    selected_backend = backend(arguments["service_id"], request=request, db=db, actor=actor, x_l5_delegation_id=delegation_id, response=response)
    return await dispatch_l5(body.tool, arguments, selected_backend)


@router.post("/v1/devops/incident-sources/{source_id}/events", status_code=202, operation_id="receiveDevopsIncident")
async def receive_devops_incident(
    source_id: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    secret_loader=Depends(load_incident_secret),
    work_backend=Depends(incident_work_backend),
):
    response.headers["Cache-Control"] = "no-store"
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > 16384:
            raise HTTPException(413, detail={"code": "request_too_large"})
    receipt = await receive_incident_event(
        db,
        source_id=source_id,
        payload=bytes(payload),
        signature=request.headers.get("X-DevOps-Signature", ""),
        load_secret=secret_loader,
    )
    selected = work_backend(db, receipt)

    async def verify_target(current):
        return await selected.incident_authority(current)

    run = await admit_source_receipt(db, selected.sessions, receipt, verify_target, selected.publish, budget_guard=selected._require_devops_budget)
    return {"event_id": receipt.event_id, "state": "admitted", "run_id": run.run_id}
