"""Issue scoped AWS sessions only after current L5 work and Product delegation checks."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from l5_services.adapters.finops.binding import binding_readback
from l5_services.api.bindings import BindingStore, _digest, _intent_hash, view_binding
from l5_services.api.configuration import view as view_configuration
from l5_services.configuration_readiness import assess_readiness
from l5_services.devops.recovery import recovery_deadline as devops_recovery_deadline
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import EventReceipt
from l5_services.models.run import Run
from l5_services.models.setup import BindingOperation, SetupIntent, SetupWork
from l5_services.models.work import Work
from l5_services.security_recovery import recovery_deadline as security_recovery_deadline
from l5_services.security_registration import current_model, current_registration, observed_registration
from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.aws_connection_authority import require_assumed_identity, verified_connection_evidence
from src.auth.l5_cleanup_scope import current_cleanup_scope, is_cleanup_scope
from src.auth.l5_devops_setup_scope import current_devops_resource_scope, current_devops_setup_scope, is_devops_resource_scope, is_devops_setup_scope
from src.auth.l5_execution_scope import current_execution_scope
from src.auth.vault_service import _visible_credential_filter
from src.internal.assume_role_routes import AssumeRoleRequestBody, assume_bound_role, get_secrets_manager
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.credential_binding import BindingResult
from src.internal.l5_devops_policy import run_policy as devops_run_policy
from src.internal.l5_devops_policy import setup_policy as devops_setup_policy
from src.internal.l5_product_routes import service_actor
from src.internal.l5_transform_policy import run_policy as transform_run_policy
from src.internal.l5_transform_policy import setup_policy as transform_setup_policy
from src.internal.service_authorization import require_service_operation
from src.l5_service_identity import WORKER_IDS, service_for_worker
from src.products.models import Product
from src.products.routes import scoped_product
from src.products.source_routes import allowed_source_ref, service_visible_bindings
from src.shared.database import get_db
from src.shared.identity.product_scope import current_l5_parent, owner_scope, product_member, service_product_scope
from src.shared.models.base import utcnow
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from src.shared.services.secrets_manager import SecretsManagerHelper

router = APIRouter(prefix="/internal/v1/l5", tags=["internal-l5"], dependencies=[Depends(verify_internal_or_irsa)])


class ConnectionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=255)
    project_id: str = Field(min_length=1, max_length=255)
    source_connection_id: str = Field(min_length=1, max_length=255)
    record_type: str = Field(min_length=1, max_length=255)
    record_id: str = Field(min_length=1, max_length=255)
    record_revision: str = Field(min_length=1, max_length=512)


class CredentialSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delegation_id: str = Field(min_length=1, max_length=255)
    delegation_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=255)
    attempt_id: str = Field(min_length=1, max_length=255)
    connection_ref: ConnectionRef
    lease_owner: str | None = Field(default=None, min_length=1, max_length=255)
    lease_fence: int | None = None


@dataclass(frozen=True)
class L5WorkPermit:
    tenant_id: str
    product_id: str
    project_id: str
    source_connection_id: str
    operation_id: str
    attempt_id: str
    lease_fence: int
    binding_generation: int
    expires_at: datetime
    admitted_source_snapshot_ids: frozenset[str] = frozenset()
    session_policy: str | None = None
    lease_owner: str | None = None
    setup_verified: dict | None = None
    provider_identity: dict | None = None


WorkAuthorizer = Callable[[CredentialServiceDelegation, CredentialSessionRequest], Awaitable[L5WorkPermit]]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _security_space_arn(identity: dict, reference: dict) -> str:
    account_id = identity.get("account_id") if isinstance(identity, dict) else None
    region = identity.get("region") if isinstance(identity, dict) else None
    space_id = reference.get("record_id") if isinstance(reference, dict) else None
    if (
        not isinstance(account_id, str)
        or not re.fullmatch(r"[0-9]{12}", account_id)
        or region != "us-east-1"
        or not isinstance(reference, dict)
        or reference.get("record_type") != "security_space"
        or not isinstance(space_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", space_id)
    ):
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    return f"arn:aws:securityagent:{region}:{account_id}:agent-space/{space_id}"


def _security_setup_policy(
    account_id: str,
    region: str,
    expires_at: datetime,
    intent: SetupIntent,
    operation: BindingOperation,
    reference: dict | None,
    owned: bool,
    registration: dict | None = None,
) -> str:
    condition = {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}}
    statements = []
    global_actions = ["securityagent:ListAgentSpaces"] if operation.action in {"create", "cleanup"} else []
    if operation.action == "create" and reference is None:
        global_actions.insert(0, "securityagent:CreateAgentSpace")
    if global_actions:
        statements.append(
            {
                "Effect": "Allow",
                "Action": global_actions,
                "Resource": "*",
                "Condition": {**condition, "StringEquals": {"aws:RequestedRegion": region}},
            }
        )
    if reference is None:
        if operation.action != "create":
            raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
        resource = f"arn:aws:securityagent:{region}:{account_id}:agent-space/*"
        scoped_condition = {**condition, "StringEquals": {"aws:ResourceTag/adp-setup-operation": intent.operation_id}}
        actions = ["securityagent:BatchGetAgentSpaces"]
    else:
        resource = _security_space_arn({"account_id": account_id, "region": region}, reference)
        scoped_condition = condition
        actions = ["securityagent:BatchGetAgentSpaces"]
        if owned and operation.action in {"check", "update", "resume"}:
            actions.extend(
                [
                    "securityagent:AddArtifact",
                    "securityagent:CreateThreatModel",
                    "securityagent:ListThreatModels",
                    "securityagent:BatchGetThreatModels",
                ]
            )
        if owned and operation.action == "cleanup":
            actions.append("securityagent:DeleteAgentSpace")
    statements.append({"Effect": "Allow", "Action": actions, "Resource": resource, "Condition": scoped_condition})
    if owned and operation.action in {"check", "update", "resume"}:
        if registration is not None:
            if (
                observed_registration({"iamRoles": [registration.get("service_role")], "s3Buckets": [registration.get("bucket")]}, account_id)
                != registration
            ):
                raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
            source_role = registration["service_role"]
        else:
            # Same deterministic identity as infra/security-sources; no wildcard
            # PassRole when native registration has not yet been persisted.
            scope = [intent.tenant_id, intent.product_id, intent.project_id, intent.configuration_id]
            source_role = f"arn:aws:iam::{account_id}:role/adp/security/ADP-Security-{_digest(scope)[:16]}"
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["iam:PassRole"],
                "Resource": source_role,
                "Condition": {**condition, "StringEquals": {"iam:PassedToService": "securityagent.amazonaws.com"}},
            }
        )
    if operation.action == "create" and reference is None:
        # CreateAgentSpace's tag-on-create authorization also checks TagResource.
        # The provider ID is not known yet; constrain the tag to this setup intent.
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["securityagent:TagResource"],
                "Resource": resource,
                "Condition": {
                    **condition,
                    "StringEquals": {"aws:RequestedRegion": region, "aws:RequestTag/adp-setup-operation": intent.operation_id},
                    "ForAllValues:StringEquals": {"aws:TagKeys": ["adp-setup-operation"]},
                },
            }
        )
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))


def _security_session_policy(binding: Binding, registration: dict, expires_at: datetime) -> str:
    identity = binding.provider_identity or {}
    resource = _security_space_arn(identity, binding.native_resource_ref)
    if (
        not isinstance(registration, dict)
        or observed_registration({"iamRoles": [registration.get("service_role")], "s3Buckets": [registration.get("bucket")]}, identity["account_id"])
        != registration
    ):
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    condition = {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}}
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "securityagent:BatchGetAgentSpaces",
                    "securityagent:UpdateAgentSpace",
                    "securityagent:CreateCodeReview",
                    "securityagent:StartCodeReviewJob",
                    "securityagent:BatchGetCodeReviewJobs",
                    "securityagent:ListCodeReviewJobsForCodeReview",
                    "securityagent:ListCodeReviews",
                    "securityagent:BatchGetCodeReviews",
                    "securityagent:ListCodeReviewJobTasks",
                    "securityagent:ListFindings",
                    "securityagent:BatchGetFindings",
                    "securityagent:StopCodeReviewJob",
                ],
                "Resource": resource,
                "Condition": condition,
            },
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"],
                "Resource": f"arn:aws:s3:::{registration['bucket']}/security-agent/source/*",
                "Condition": condition,
            },
            {
                "Effect": "Allow",
                "Action": ["iam:PassRole"],
                "Resource": registration["service_role"],
                "Condition": {**condition, "StringEquals": {"iam:PassedToService": "securityagent.amazonaws.com"}},
            },
        ],
    }
    return json.dumps(policy, separators=(",", ":"))


def _security_observation_policy(binding: Binding, capability: str, expires_at: datetime) -> str:
    actions = (
        ["BatchGetCodeReviewJobs", "ListCodeReviewJobTasks", "ListFindings", "BatchGetFindings"]
        if capability == "code_review"
        else ["BatchGetThreatModelJobs", "ListThreatModelJobTasks", "BatchGetThreatModelJobTasks", "ListThreats", "BatchGetThreats"]
    )
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [f"securityagent:{action}" for action in actions],
                    "Resource": _security_space_arn(binding.provider_identity, binding.native_resource_ref),
                    "Condition": {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}},
                }
            ],
        },
        separators=(",", ":"),
    )


def _threat_model_session_policy(binding: Binding, registration: dict, expires_at: datetime) -> str:
    identity = binding.provider_identity or {}
    resource = _security_space_arn(identity, binding.native_resource_ref)
    # Reuse the validated exact-role restriction; document updates require a
    # serviceRole even when the persistent model already records that role.
    pass_role = json.loads(_security_session_policy(binding, registration, expires_at))["Statement"][-1]
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "securityagent:BatchGetThreatModels",
                        "securityagent:AddArtifact",
                        "securityagent:UpdateThreatModel",
                        "securityagent:StartThreatModelJob",
                        "securityagent:ListArtifacts",
                        "securityagent:ListThreatModelJobs",
                        "securityagent:BatchGetThreatModelJobs",
                        "securityagent:ListThreatModelJobTasks",
                        "securityagent:BatchGetThreatModelJobTasks",
                        "securityagent:ListThreats",
                        "securityagent:BatchGetThreats",
                        "securityagent:StopThreatModelJob",
                    ],
                    "Resource": resource,
                    "Condition": {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}},
                },
                pass_role,
            ],
        },
        separators=(",", ":"),
    )


def _wa_session_policy(binding: Binding, run: Run, expires_at: datetime) -> str:
    reference = binding.native_resource_ref if isinstance(binding.native_resource_ref, dict) else {}
    identity = binding.provider_identity if isinstance(binding.provider_identity, dict) else {}
    connection = binding.connection_ref if isinstance(binding.connection_ref, dict) else {}
    profile = reference.get("record_id")
    snapshot_hash = reference.get("record_revision")
    if (
        run.input.get("capability") != "architecture_review"
        or run.admission.get("native_profile_ref") != reference
        or run.admission.get("profile_hash") != snapshot_hash
        or reference.get("record_type") != "agent_profile"
        or (reference.get("tenant_id"), reference.get("project_id"), reference.get("source_connection_id"))
        != (run.tenant_id, run.project_id, connection.get("source_connection_id"))
        or not isinstance(profile, str)
        or not isinstance(snapshot_hash, str)
        or not re.fullmatch(r"[a-f0-9]{64}", snapshot_hash)
        or not isinstance(identity.get("account_id"), str)
        or not isinstance(identity.get("region"), str)
        or not re.fullmatch(r"[0-9]{12}", identity["account_id"])
        or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]", identity["region"])
        or not re.fullmatch(
            rf"arn:aws:wellarchitected:{re.escape(identity['region'])}:{re.escape(identity['account_id'])}:agent-profile/[A-Za-z0-9_-]+",
            profile,
        )
        or not any(isinstance(item, dict) and item.get("snapshot_hash") == snapshot_hash for item in (binding.readiness or []))
    ):
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "wellarchitected:GetAgentProfile",
                        "wellarchitected:ListAgentContexts",
                        "wellarchitected:StartAgentRecommendationGeneration",
                        "wellarchitected:ListAgentRecommendationGenerations",
                        "wellarchitected:GetAgentRecommendationGeneration",
                        "wellarchitected:ListAgentRecommendations",
                        "wellarchitected:GetAgentRecommendation",
                    ],
                    "Resource": [profile, f"arn:aws:wellarchitected:{identity['region']}:{identity['account_id']}:agent-recommendation/*"],
                    "Condition": {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}},
                }
            ],
        },
        separators=(",", ":"),
    )


def _finops_session_policy(selection: dict, account_id: str, expires_at: datetime, *, setup=False) -> str:
    billing = selection.get("billing_scope") or {}
    view = billing.get("view_ref")
    role = selection.get("data_role_ref")
    if (
        not isinstance(view, str)
        or not re.fullmatch(rf"arn:aws:billing::{account_id}:billingview/[A-Za-z0-9_-]+", view)
        or not isinstance(role, str)
        or not re.fullmatch(rf"arn:aws:iam::{account_id}:role/[A-Za-z0-9_+=,.@/-]+", role)
    ):
        raise HTTPException(409, detail={"code": "billing_scope_unverified"})
    condition = {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}}
    statements = [
        {
            "Effect": "Allow",
            "Action": [
                f"finops-agent:{action}"
                for action in (
                    "GetAgentSpace",
                    "ListAgentSpaces",
                    "ListConnections",
                    "GetConnection",
                    "ListIntegrations",
                    "CreateOneTimeLoginSession",
                )
            ],
            "Resource": "*",
            "Condition": condition,
        },
        {
            "Effect": "Allow",
            "Action": ["billing:GetBillingView"] if setup else ["ce:GetCostAndUsage", "billing:GetBillingViewData"],
            "Resource": view,
            "Condition": condition,
        },
        {
            "Effect": "Allow",
            "Action": ["iam:GetRole", "iam:ListRolePolicies", "iam:GetRolePolicy", "iam:ListAttachedRolePolicies", "iam:SimulatePrincipalPolicy"]
            if setup
            else ["iam:GetRole"],
            "Resource": role,
            "Condition": condition,
        },
    ]
    if setup:
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["iam:GetPolicy", "iam:GetPolicyVersion"],
                "Resource": [f"arn:aws:iam::{account_id}:policy/*", "arn:aws:iam::aws:policy/*"],
                "Condition": condition,
            }
        )
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))


async def verify_l5_setup_work(db: AsyncSession, grant: CredentialServiceDelegation, body: CredentialSessionRequest) -> L5WorkPermit:
    await db.refresh(grant)
    now = utcnow()
    denied = HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    work = await db.get(SetupWork, (grant.tenant_id, body.attempt_id), populate_existing=True)
    if (
        work is None
        or work.operation_id != body.operation_id
        or work.completed_at is not None
        or work.blocked_at is not None
        or work.lease_owner != body.lease_owner
        or not body.lease_owner
        or work.fence != body.lease_fence
        or work.fence < 1
        or work.lease_until is None
        or _aware(work.lease_until) <= now
    ):
        raise denied
    intent = await db.get(SetupIntent, (grant.tenant_id, work.operation_id), populate_existing=True)
    operation = await db.get(BindingOperation, (grant.tenant_id, work.operation_id), populate_existing=True)
    from l5_services.setup_authority import continuation_rows, setup_authorization

    authority = await setup_authorization(db, intent) if intent is not None else {}
    if intent is not None and intent.service_id == "devops" and intent.authorization.get("purpose") == "devops_setup":
        continuations = await continuation_rows(db, intent)
        if continuations and (
            continuations[-1].receipt["scope"] != grant.execution_scope
            or continuations[-1].receipt["expires_at"] != _aware(grant.expires_at).isoformat()
        ):
            raise denied
    service_id = service_for_worker(grant.service_id)
    cleanup_only = is_cleanup_scope(grant)
    setup_only = is_devops_setup_scope(grant)
    resource_only = is_devops_resource_scope(grant)
    capabilities = {
        "security": {"code_review", "threat_model"},
        "devops": {"investigation", "release_readiness", "prevention"},
        "finops": {"cost_inquiry"},
        "well_architected": {"architecture_review"},
        "transform": {"refactor", "dependency_upgrade"},
    }.get(service_id, set())
    if (
        intent is None
        or service_id not in {"security", "devops", "finops", "well_architected", "transform"}
        or intent.service_id != service_id
        or grant.revision != body.delegation_revision
        or (service_id in {"finops", "transform"} and intent.request.get("action") != "link")
        or (intent.authorization.get("binding_generation", intent.binding_generation) if intent.service_id == "devops" else intent.binding_generation)
        != grant.binding_generation
        or operation is None
        or operation.state not in {"accepted", "running", "outcome_unknown", "waiting"}
        or operation.action != intent.request.get("action")
        or (operation.action not in {"create", "link"} and operation.binding_id != intent.request.get("binding_id"))
        or (intent.product_id, intent.project_id, intent.actor_id) != (grant.product_id, grant.project_id, grant.created_by)
        or authority.get("delegation_id") != grant.id
        or authority.get("delegation_revision") != grant.revision
        or intent.intent_hash != _intent_hash(intent)
        or intent.request.get("action") not in {"create", "link", "check", "update", "suspend", "resume", "unlink", "cleanup"}
        or grant.parent_delegation_id is not None
        or grant.operation_id is not None
        or grant.attempt_id is not None
        or grant.lease_fence is not None
        or grant.revoked_at is not None
        or _aware(grant.expires_at) <= now
        or body.connection_ref.model_dump() not in (grant.sources or [])
        or "credential:session" not in (grant.operations or [])
        or (not (cleanup_only or setup_only or resource_only) and not capabilities.intersection(grant.capabilities or []))
        or (
            cleanup_only
            and (
                operation.action != "cleanup"
                or intent.request.get("owned_cleanup_confirmed") is not True
                or intent.authorization.get("control_action") != "cleanup"
                or intent.authorization.get("owned_cleanup_hash") != _digest(grant.execution_scope.get("owned_resource"))
            )
        )
    ):
        raise denied
    config = await db.get(Configuration, (grant.tenant_id, intent.configuration_id), populate_existing=True)
    if resource_only and (
        operation.action != "check"
        or intent.authorization.get("purpose") != "devops_resource_verification"
        or intent.authorization.get("control_action") != "devops_resource_verification"
        or intent.request.get("resource_selection") != grant.execution_scope.get("resource_selection")
        or intent.request.get("binding_id") != grant.execution_scope.get("binding_id")
    ):
        raise denied
    if setup_only and (
        operation.action != grant.execution_scope.get("setup_action", "create")
        or intent.request.get("native_resource_ref") != grant.execution_scope.get("native_resource_ref")
        or intent.authorization.get("control_action") != "devops_setup"
        or intent.authorization.get("purpose") != "devops_setup"
    ):
        raise denied
    if setup_only:
        retained = (
            await db.scalars(
                select(SetupIntent.operation_id)
                .where(
                    SetupIntent.tenant_id == grant.tenant_id,
                    SetupIntent.configuration_id == intent.configuration_id,
                    SetupIntent.service_id == "devops",
                    SetupIntent.request["action"].as_string().in_(("create", "link")),
                )
                .limit(2)
            )
        ).all()
        if retained != [intent.operation_id]:
            raise denied
    valid_scope = (
        await current_devops_resource_scope(db, grant, admitted=True)
        if resource_only
        else await current_devops_setup_scope(db, grant, action=operation.action, binding_generation=intent.binding_generation)
        if setup_only
        else await current_cleanup_scope(db, grant, binding_id=operation.binding_id, binding_generation=intent.binding_generation, admitted=True)
        if cleanup_only
        else await current_execution_scope(
            db, grant, check_binding=True, binding_id=operation.binding_id, binding_generation=intent.binding_generation
        )
    )
    if not valid_scope:
        raise denied
    if grant.execution_scope is not None and intent.configuration_id != grant.execution_scope["configuration_id"]:
        raise denied
    revision = intent.request.get("configuration_revision")
    version = await db.get(ConfigurationVersion, (grant.tenant_id, intent.configuration_id, revision), populate_existing=True)
    selected = body.connection_ref.model_dump()
    connection = {**selected, "record_type": "connection"}
    settings = version.settings if version is not None else {}
    hosting = settings.get(service_id, {}) if service_id in {"devops", "transform"} else settings
    if (
        config is None
        or version is None
        or config.service_id != service_id
        or (config.product_id, config.project_id, config.owner_id, config.current_revision, config.handoff_state)
        != (grant.product_id, grant.project_id, grant.created_by, revision, "linked")
        or (not (cleanup_only or setup_only or resource_only) and version.settings.get("enabled") is not True)
        or hosting.get("hosting_connection_ref" if intent.service_id == "devops" else "connection_ref") != connection
        or (intent.service_id == "security" and hosting.get("region") != "us-east-1")
        or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", hosting.get("region", ""))
        or view_configuration(config, version)["scope"] != intent.scope
    ):
        raise denied
    if intent.service_id == "devops" and (
        operation.action not in {"create", "link", "check"}
        or not (setup_only or resource_only)
        and not (set(grant.capabilities or []) & set(settings.get("capabilities") or []) & {"investigation", "release_readiness", "prevention"})
    ):
        raise denied
    binding = None
    if service_id == "security" and operation.binding_id is not None or service_id != "security" and operation.action not in {"create", "link"}:
        binding = await db.get(Binding, (grant.tenant_id, operation.binding_id), populate_existing=True)
        if (
            binding is None
            or binding.service_id != service_id
            or binding.generation != intent.binding_generation
            or binding.configuration_id != config.configuration_id
            or binding.scope != intent.scope
            or binding.connection_ref != connection
            or binding.state == "cleaned"
            or binding.state == "cleanup_pending"
            and (operation.action != "cleanup" or service_id != "security" and not cleanup_only)
        ):
            raise denied
    credential = await db.get(UserCredential, grant.credential_id, populate_existing=True)
    identity = (credential.scopes or {}) if credential is not None else {}
    account_id = identity.get("account_id")
    region = hosting.get("region")
    role_arn = identity.get("role_arn")
    if (
        selected["record_type"] != "credential"
        or selected["record_id"] != grant.credential_id
        or selected["source_connection_id"] != grant.credential_id
        or credential is None
        or credential.aws_verified_version_id != selected["record_revision"]
        or not isinstance(account_id, str)
        or not re.fullmatch(r"[0-9]{12}", account_id)
        or not isinstance(role_arn, str)
        or not role_arn.startswith(f"arn:aws:iam::{account_id}:role/")
    ):
        raise denied
    expires_at = min(_aware(grant.expires_at), _aware(work.lease_until))
    if service_id == "security":
        if binding is not None and any((binding.provider_identity or {}).get(field) != identity.get(field) for field in ("account_id", "role_arn")):
            raise denied
        reference = (
            binding.native_resource_ref
            if binding is not None
            else intent.request.get("native_resource_ref")
            if operation.action == "link"
            else operation.native_resource_ref
            if operation.action == "create" and operation.native_resource_ref
            else None
        )
        if reference is None and operation.action == "create" and operation.native_candidate_id:
            reference = {
                "tenant_id": grant.tenant_id,
                "project_id": grant.project_id,
                "source_connection_id": credential.id,
                "record_type": "security_space",
                "record_id": operation.native_candidate_id,
            }
        if reference is not None and (
            not isinstance(reference, dict)
            or (reference.get("tenant_id"), reference.get("project_id"), reference.get("source_connection_id"))
            != (grant.tenant_id, grant.project_id, credential.id)
        ):
            raise denied
        receipt = (binding.creation_receipt or {}) if binding is not None else {}
        owned = (
            binding is not None
            and binding.ownership == "adp_created"
            and receipt.get("native_resource_ref") == binding.native_resource_ref
            and receipt.get("provider_identity") == binding.provider_identity
            and receipt.get("operation_id")
            and receipt.get("intent_hash")
        )
        if (
            operation.action == "create"
            and binding is not None
            and (not owned or receipt.get("operation_id") != intent.operation_id)
            or operation.action == "link"
            and binding is not None
            and reference != intent.request.get("native_resource_ref")
            or operation.action == "cleanup"
            and (not owned or intent.owned_resource_receipt != binding.creation_receipt)
        ):
            raise denied
        registration = (
            next((item.get("registration") for item in (binding.readiness or []) if item.get("kind") == "code_review_source_registration"), None)
            if binding
            else None
        )
        policy = _security_setup_policy(account_id, region, expires_at, intent, operation, reference, bool(owned), registration)
    verified = {
        "configuration_id": config.configuration_id,
        "configuration_revision": revision,
        "scope": intent.scope,
        "connection_ref": connection,
        "account_id": account_id,
        "role_arn": role_arn,
        "region": region,
        "authorization": authority,
    }
    if cleanup_only:
        if binding is None or intent.owned_resource_receipt != binding.creation_receipt:
            raise denied
        verified["owned_cleanup"] = grant.execution_scope["owned_resource"]
    if intent.service_id == "devops":
        if operation.action == "link":
            binding = await db.scalar(
                select(Binding).where(
                    Binding.tenant_id == grant.tenant_id,
                    Binding.configuration_id == config.configuration_id,
                    Binding.connection_key == _digest(connection),
                    Binding.native_key == _digest(intent.request.get("native_resource_ref")),
                )
            )
        from l5_services.devops.setup import setup_binding_id

        verified["binding_id"] = binding.binding_id if binding is not None else setup_binding_id(intent)
        if resource_only:
            from src.internal.l5_devops_policy import resource_policy

            verified["resource_selection"] = grant.execution_scope["resource_selection"]
            policy = resource_policy(intent, verified, expires_at, binding)
        else:
            policy = devops_setup_policy(intent, verified, expires_at, binding)
    elif service_id == "finops":
        selection = version.settings.get("finops") or {}
        reference = intent.request.get("native_resource_ref") or {}
        if (
            selection.get("agent_mode") != "adopt"
            or reference.get("record_type") != "finops_agent"
            or reference.get("record_id") != selection.get("native_agent_ref")
            or any(reference.get(field) != intent.scope.get(field) for field in ("tenant_id", "project_id"))
            or reference.get("source_connection_id") != connection["source_connection_id"]
        ):
            raise denied
        verified["finops"] = selection
        policy = _finops_session_policy(selection, account_id, expires_at, setup=True)
    elif service_id == "well_architected":
        from src.l5_backend_well_architected import cleanup_policy, setup_policy

        if cleanup_only:
            try:
                await BindingStore(db, grant.tenant_id, grant.product_id, grant.project_id, service_id)._wa_cleanup_safe(binding, intent.operation_id)
            except ValueError:
                raise denied from None
            policy = cleanup_policy(binding.native_resource_ref, account_id, region, expires_at)
        else:
            policy = setup_policy(version.settings, account_id, expires_at)
            verified["well_architected_settings"] = version.settings
    elif service_id == "transform":
        policy = transform_setup_policy(version.settings, intent, {"account_id": account_id, "region": region, "role_arn": role_arn}, expires_at)
    return L5WorkPermit(
        grant.tenant_id,
        grant.product_id,
        grant.project_id,
        grant.credential_id,
        body.operation_id,
        body.attempt_id,
        work.fence,
        grant.binding_generation,
        expires_at,
        session_policy=policy,
        lease_owner=work.lease_owner,
        setup_verified=verified,
    )


async def verify_l5_work(db: AsyncSession, grant: CredentialServiceDelegation, body: CredentialSessionRequest) -> L5WorkPermit:
    from l5_services.devops.recovery import readback_authority

    from src.auth.l5_devops_report_scope import is_report_recovery

    await db.refresh(grant)
    now = utcnow()
    denied = HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    if (
        service_for_worker(grant.service_id) is None
        or grant.revoked_at is not None
        or grant.revision != body.delegation_revision
        or (grant.operation_id is not None and grant.operation_id != body.operation_id)
        or (grant.lease_fence is not None and (not isinstance(grant.lease_fence, int) or grant.lease_fence < 1))
        or (grant.operation_id is None and (grant.parent_delegation_id is None or grant.attempt_id is not None or grant.lease_fence is not None))
        or not isinstance(grant.binding_generation, int)
        or grant.binding_generation < 1
        or _aware(grant.expires_at) <= now
        or body.connection_ref.model_dump() not in (grant.sources or [])
    ):
        raise denied
    if not await current_l5_parent(db, grant):
        raise denied
    work = await db.get(Work, (grant.tenant_id, body.attempt_id), populate_existing=True)
    if (
        work is None
        or work.run_id != body.operation_id
        or work.completed_at is not None
        or work.blocked_at is not None
        or not work.lease_owner
        or work.lease_until is None
        or _aware(work.lease_until) <= now
    ):
        raise denied
    run = await db.get(Run, (grant.tenant_id, work.run_id), populate_existing=True)
    recovery_until = (
        (devops_recovery_deadline if run.service_id == "devops" else security_recovery_deadline)(run, work.kind, now) if run is not None else None
    )
    authority = (
        readback_authority(run, work.kind, now) if run is not None and run.service_id == "devops" else run.admission if run is not None else {}
    )
    if (
        run is None
        or not isinstance(run.admission, dict)
        or (run.product_id, run.project_id, run.service_id, run.binding_generation)
        != (grant.product_id, grant.project_id, service_for_worker(grant.service_id), grant.binding_generation)
        or (run.state in {"cancelled", "expired", "blocked", "failed", "succeeded"} and recovery_until is None)
        or (_aware(run.deadline_at) <= now and recovery_until is None)
        or (recovery_until is not None and body.connection_ref.record_type != "credential")
        or authority.get("delegation_id") != grant.id
        or authority.get("delegation_version") != grant.revision
        or (is_report_recovery(grant) and recovery_until is None)
        or run.admission.get("binding_generation") != grant.binding_generation
        or run.admission.get("actor_id") != grant.created_by
        or not isinstance(run.admission.get("scope"), dict)
        or any(run.admission.get("scope", {}).get(field) != getattr(run, field) for field in ("tenant_id", "product_id", "project_id"))
    ):
        raise denied
    config = await db.get(Configuration, (grant.tenant_id, run.configuration_id), populate_existing=True)
    if run.service_id == "devops" and run.admission.get("event_id") is not None:
        event = await db.get(
            EventReceipt,
            (run.tenant_id, run.product_id, run.project_id, run.service_id, run.trigger_id, run.admission["event_id"]),
            populate_existing=True,
        )
        if (
            event is None
            or event.state != "admitted"
            or event.run_id != run.run_id
            or event.event_kind != "native_event"
            or run.admission.get("event_kind") != event.event_kind
            or event.source_ref != run.admission.get("event_source_ref")
            or event.configuration_id != run.configuration_id
            or event.configuration_revision != run.configuration_revision
            or event.run_input != run.input
        ):
            raise denied
    if not await current_execution_scope(db, grant, check_binding=True, binding_id=run.binding_id, binding_generation=run.binding_generation):
        raise denied
    if grant.execution_scope is not None and run.configuration_id != grant.execution_scope["configuration_id"]:
        raise denied
    version = await db.get(ConfigurationVersion, (grant.tenant_id, run.configuration_id, run.configuration_revision), populate_existing=True)
    binding = await db.get(Binding, (grant.tenant_id, run.binding_id), populate_existing=True)
    if (
        config is None
        or version is None
        or binding is None
        or (config.product_id, config.project_id, config.service_id, config.owner_id, config.current_revision, config.handoff_state)
        != (grant.product_id, grant.project_id, run.service_id, grant.created_by, run.configuration_revision, "linked")
        or version.settings.get("enabled") is not True
        or binding.state != "ready"
        or binding.gaps
        or (binding.product_id, binding.project_id, binding.service_id, binding.configuration_id, binding.generation)
        != (grant.product_id, grant.project_id, run.service_id, config.configuration_id, grant.binding_generation)
        or binding.connection_ref
        != (
            version.settings.get("devops", {}).get("hosting_connection_ref")
            if run.service_id == "devops"
            else version.settings.get("transform", {}).get("connection_ref")
            if run.service_id == "transform"
            else version.settings.get("connection_ref")
        )
        or binding.connection_ref != run.admission.get("connection_ref")
        or binding.scope != run.admission.get("scope")
        or (
            run.service_id == "devops"
            and (
                not isinstance(binding.connection_ref, dict)
                or not isinstance(binding.native_resource_ref, dict)
                or binding.native_resource_ref.get("record_type") != "agent_space"
                or (
                    run.input.get("capability") in {"investigation", "release_verification"}
                    and (run.input.get("sources", []) != [] or run.admission.get("source_snapshots"))
                )
                or (
                    binding.native_resource_ref.get("tenant_id"),
                    binding.native_resource_ref.get("project_id"),
                    binding.native_resource_ref.get("source_connection_id"),
                )
                != (run.tenant_id, run.project_id, binding.connection_ref.get("source_connection_id"))
                or run.admission.get("native_space_ref") != binding.native_resource_ref
                or run.admission.get("provider_identity") != binding.provider_identity
                or (binding.provider_identity or {}).get("region") != version.settings.get("devops", {}).get("region")
            )
        )
        or run.input.get("capability") not in (grant.capabilities or [])
    ):
        raise denied
    if version.state != "ready" or version.gaps:
        try:
            readiness, gaps, _ = assess_readiness(
                version.settings, config.configuration_id, [view_binding(binding)], delegation_active=True, service_id=run.service_id
            )
        except ValueError:
            raise denied from None
        if readiness != "ready" or gaps:
            raise denied
    selected = body.connection_ref.model_dump()
    if run.service_id == "transform":
        from src.auth.l5_transform_branch_scope import occurrence_source

        sources = run.input.get("sources")
        visible = await service_visible_bindings(db, grant)
        permitted = [allowed_source_ref(grant, source) for source in visible]
        if (
            not isinstance(sources, list)
            or len(sources) != 1
            or any(
                (source not in version.settings.get("source_refs", []) and source != occurrence_source(grant)) or source not in permitted
                for source in sources
            )
            or [snapshot.get("source_ref") for snapshot in run.admission.get("source_snapshots", [])] != sources
        ):
            raise denied
    if run.service_id == "security":
        # Freshness gates admission. Continuations retain those verified pins
        # for the run's finite deadline, while the checks above still require
        # current consent, configuration, binding generation and a live lease.
        try:
            admitted_at = datetime.fromisoformat(run.admission["admitted_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise denied from None
        if admitted_at.tzinfo is None or admitted_at > now:
            raise denied
        registration = current_registration(binding, now=admitted_at)
        model = current_model(binding, now=admitted_at) if run.input.get("capability") == "threat_model" else None
        if (
            registration is None
            or run.admission.get("native_space_ref") != binding.native_resource_ref
            or run.admission.get("provider_identity") != binding.provider_identity
            or run.input.get("capability") == "code_review"
            and run.admission.get("source_registration") != registration
            or run.input.get("capability") == "threat_model"
            and (model is None or run.admission.get("native_model_ref") != model)
            or run.input.get("capability") not in {"code_review", "threat_model"}
        ):
            raise denied
        if recovery_until is not None:
            from src.products.source_routes import source_ref_authorized

            sources = run.input.get("sources")
            snapshots = run.admission.get("source_snapshots") or []
            visible = await service_visible_bindings(db, grant)
            if (
                not sources
                or [snapshot.get("source_ref") for snapshot in snapshots] != sources
                or any(not any(source_ref_authorized(grant, candidate, source) for candidate in visible) for source in sources)
            ):
                raise denied
    if run.service_id == "finops":
        readback = binding_readback(binding)
        if (
            readback is None
            or readback.get("configuration_revision") != run.configuration_revision
            or run.admission.get("native_agent_ref") != binding.native_resource_ref
            or run.admission.get("billing_scope") != readback["billing_scope"]
            or run.admission.get("native_agent_display_name") != readback["native_agent_display_name"]
            or run.admission.get("provider_identity") != binding.provider_identity
        ):
            raise denied
    if selected["record_type"] == "credential":
        if not body.lease_owner or body.lease_owner != work.lease_owner or body.lease_fence != work.fence:
            raise denied
        credential = await db.get(UserCredential, grant.credential_id, populate_existing=True)
        attempt = await db.scalar(
            select(WorkerAttempt)
            .where(
                WorkerAttempt.tenant_id == grant.tenant_id,
                WorkerAttempt.run_id == run.run_id,
                WorkerAttempt.operation == (TRANSFORM_SUBMIT_OPERATION if run.service_id == "transform" else "submit"),
            )
            .execution_options(populate_existing=True)
        )
        if (
            selected["source_connection_id"] != grant.credential_id
            or selected["record_id"] != grant.credential_id
            or (
                run.service_id in {"security", "transform"} and (attempt is None or attempt.status not in {"prepared", "accepted", "outcome_unknown"})
            )
            or (recovery_until is not None and (attempt is None or attempt.status != "accepted" or attempt.native_job_ref != run.native_job_ref))
            or (run.service_id == "transform" and work.kind not in {"submit", "reconcile", "poll", "collect"})
            or credential is None
            or credential.aws_verified_version_id != selected["record_revision"]
            or not isinstance(binding.provider_identity, dict)
            or run.admission.get("provider_identity") != binding.provider_identity
            or any(binding.provider_identity.get(field) != (credential.scopes or {}).get(field) for field in ("account_id", "role_arn"))
        ):
            raise denied
        if run.service_id == "security":
            try:
                verified_connection_evidence(credential)
            except HTTPException:
                raise denied from None
    else:
        snapshots = run.admission.get("source_snapshots") or []
        if not any(snapshot.get("source_ref") == selected for snapshot in snapshots):
            raise denied
        visible = await service_visible_bindings(db, grant)
        from src.products.source_routes import source_ref_authorized

        if not any(source_ref_authorized(grant, source_binding, selected) for source_binding in visible):
            raise denied
    snapshots = run.admission.get("source_snapshots") or []
    admitted_ids = frozenset(snapshot["snapshot_id"] for snapshot in snapshots if snapshot.get("source_ref") == selected)
    if grant.operation_id is None or (is_report_recovery(grant) and grant.attempt_id is None):
        if not body.lease_owner or body.lease_owner != work.lease_owner or body.lease_fence != work.fence:
            raise denied
        bound = await db.execute(
            update(CredentialServiceDelegation)
            .where(
                CredentialServiceDelegation.id == grant.id,
                (
                    CredentialServiceDelegation.operation_id == run.run_id
                    if is_report_recovery(grant)
                    else CredentialServiceDelegation.operation_id.is_(None)
                ),
                CredentialServiceDelegation.attempt_id.is_(None),
                CredentialServiceDelegation.lease_fence.is_(None),
                CredentialServiceDelegation.revoked_at.is_(None),
            )
            .values(operation_id=run.run_id, attempt_id=work.work_id, lease_fence=work.fence)
        )
        if bound.rowcount != 1:
            await db.rollback()
            raise denied
        await db.commit()
        return await verify_l5_work(db, grant, body)
    if grant.attempt_id != work.work_id:
        previous = await db.get(Work, (grant.tenant_id, grant.attempt_id), populate_existing=True)
        if (
            previous is None
            or previous.run_id != run.run_id
            or (
                previous.completed_at is None and previous.blocked_at is None and (previous.lease_until is None or _aware(previous.lease_until) > now)
            )
            or not body.lease_owner
            or body.lease_owner != work.lease_owner
            or body.lease_fence != work.fence
        ):
            raise denied
        rebound = await db.execute(
            update(CredentialServiceDelegation)
            .where(
                CredentialServiceDelegation.id == grant.id,
                CredentialServiceDelegation.operation_id == run.run_id,
                CredentialServiceDelegation.attempt_id == previous.work_id,
                CredentialServiceDelegation.lease_fence == grant.lease_fence,
                CredentialServiceDelegation.revoked_at.is_(None),
            )
            .values(attempt_id=work.work_id, lease_fence=work.fence)
        )
        if rebound.rowcount != 1:
            await db.rollback()
            raise denied
        await db.commit()
        return await verify_l5_work(db, grant, body)
    if grant.lease_fence != work.fence:
        if (
            not body.lease_owner
            or body.lease_owner != work.lease_owner
            or body.lease_fence != work.fence
            or not isinstance(grant.lease_fence, int)
            or work.fence <= grant.lease_fence
        ):
            raise denied
        renewed = await db.execute(
            update(CredentialServiceDelegation)
            .where(
                CredentialServiceDelegation.id == grant.id,
                CredentialServiceDelegation.operation_id == run.run_id,
                CredentialServiceDelegation.attempt_id == work.work_id,
                CredentialServiceDelegation.lease_fence == grant.lease_fence,
                CredentialServiceDelegation.revoked_at.is_(None),
            )
            .values(lease_fence=work.fence)
        )
        if renewed.rowcount != 1:
            await db.rollback()
            raise denied
        await db.commit()
        return await verify_l5_work(db, grant, body)
    expires_at = min(_aware(grant.expires_at), recovery_until or _aware(run.deadline_at), _aware(work.lease_until))
    policy = (
        _security_observation_policy(binding, run.input["capability"], expires_at)
        if recovery_until is not None and run.service_id == "security"
        else _security_session_policy(binding, registration, expires_at)
        if selected["record_type"] == "credential" and run.service_id == "security" and run.input.get("capability") == "code_review"
        else _threat_model_session_policy(binding, registration, expires_at)
        if selected["record_type"] == "credential" and run.service_id == "security" and run.input.get("capability") == "threat_model"
        else _wa_session_policy(binding, run, expires_at)
        if selected["record_type"] == "credential" and run.service_id == "well_architected"
        else devops_run_policy(binding, run, expires_at, work_kind=work.kind)
        if selected["record_type"] == "credential" and run.service_id == "devops"
        else _finops_session_policy(version.settings.get("finops") or {}, binding.provider_identity["account_id"], expires_at)
        if selected["record_type"] == "credential" and run.service_id == "finops"
        else transform_run_policy(version.settings, binding, run, expires_at)
        if selected["record_type"] == "credential" and run.service_id == "transform"
        else None
    )
    return L5WorkPermit(
        grant.tenant_id,
        grant.product_id,
        grant.project_id,
        selected["source_connection_id"],
        body.operation_id,
        body.attempt_id,
        work.fence,
        binding.generation,
        expires_at,
        admitted_ids,
        policy,
        work.lease_owner,
        provider_identity=binding.provider_identity if selected["record_type"] == "credential" else None,
    )


def get_l5_work_authorizer(db: AsyncSession = Depends(get_db)) -> WorkAuthorizer:
    async def authorize(grant: CredentialServiceDelegation, body: CredentialSessionRequest) -> L5WorkPermit:
        from src.auth.l5_devops_knowledge_scope import is_knowledge_scope

        if (grant.execution_scope or {}).get("purpose") == "devops_knowledge_native_publication":
            from src.internal.l5_knowledge_work import verify_knowledge_native_work

            return await verify_knowledge_native_work(db, grant, body)
        if is_knowledge_scope(grant):
            from src.internal.l5_knowledge_work import verify_knowledge_source_work

            return await verify_knowledge_source_work(db, grant, body)
        if grant.parent_delegation_id is None and grant.operation_id is None:
            return await verify_l5_setup_work(db, grant, body)
        return await verify_l5_work(db, grant, body)

    return authorize


def active_grant(
    grant: CredentialServiceDelegation,
    body: CredentialSessionRequest,
    permit: L5WorkPermit,
    *,
    minimum_remaining: timedelta = timedelta(seconds=900),
) -> datetime:
    expiry = grant.expires_at.replace(tzinfo=UTC) if grant.expires_at.tzinfo is None else grant.expires_at
    work_expiry = permit.expires_at.replace(tzinfo=UTC) if permit.expires_at.tzinfo is None else permit.expires_at
    if (
        grant.revoked_at is not None
        or grant.revision != body.delegation_revision
        or (
            (grant.operation_id != body.operation_id or grant.attempt_id != body.attempt_id or grant.lease_fence != permit.lease_fence)
            if permit.setup_verified is None
            else (
                grant.parent_delegation_id is not None
                or grant.operation_id is not None
                or grant.attempt_id is not None
                or grant.lease_fence is not None
            )
        )
        or (
            permit.session_policy is not None
            and (not body.lease_owner or body.lease_owner != permit.lease_owner or body.lease_fence != permit.lease_fence)
        )
        or grant.binding_generation != permit.binding_generation
        or permit.lease_fence < 1
        or permit.binding_generation < 1
        or (permit.tenant_id, permit.product_id, permit.project_id, permit.source_connection_id, permit.operation_id, permit.attempt_id)
        != (grant.tenant_id, grant.product_id, grant.project_id, grant.installation_id or grant.credential_id, body.operation_id, body.attempt_id)
    ):
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    deadline = min(expiry, work_expiry)
    if deadline <= utcnow() + minimum_remaining:
        raise HTTPException(403, detail={"code": "delegation_expiring"})
    return deadline


async def verify_assumed_session(session, role_arn: str, account_id: str) -> dict:
    if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]", session.region):
        raise HTTPException(403, detail={"code": "selected_role_mismatch"})

    def inspect():
        client = boto3.Session(
            aws_access_key_id=session.access_key_id,
            aws_secret_access_key=session.secret_access_key,
            aws_session_token=session.session_token,
            region_name=session.region,
        ).client("sts")
        return client.get_caller_identity()

    try:
        identity = await asyncio.to_thread(inspect)
    except Exception as exc:
        raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from exc
    if identity.get("Account") != account_id:
        raise HTTPException(403, detail={"code": "selected_role_mismatch"})
    try:
        require_assumed_identity(SimpleNamespace(assumed_role_arn=identity.get("Arn")), role_arn)
    except (HTTPException, AttributeError, TypeError) as exc:
        raise HTTPException(403, detail={"code": "selected_role_mismatch"}) from exc
    return {
        "account_id": account_id,
        "role_arn": role_arn,
        "region": session.region,
        "verified_at": utcnow().isoformat().replace("+00:00", "Z"),
    }


@router.post("/credential-session", operation_id="getL5CredentialSession")
async def credential_session(
    body: CredentialSessionRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    secrets_manager: SecretsManagerHelper = Depends(get_secrets_manager),
    work_authorizer: WorkAuthorizer = Depends(get_l5_work_authorizer),
):
    return await issue_credential_session(body, request, response, db, secrets_manager, work_authorizer)


@router.post("/security/setup-document", operation_id="getSecuritySetupDocument")
async def security_setup_document(
    body: CredentialSessionRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    work_authorizer: WorkAuthorizer = Depends(get_l5_work_authorizer),
):
    from src.internal.l5_security_setup_source import setup_document

    return await setup_document(body, request, response, db, work_authorizer)


async def issue_credential_session(body, request, response, db, secrets_manager, work_authorizer, *, knowledge=False):
    actor = service_actor(request)
    require_service_operation(request, "l5:knowledge-publication" if knowledge else "l5:credential-session")
    grant = await db.get(CredentialServiceDelegation, body.delegation_id)
    if grant is None:
        raise HTTPException(403, detail={"code": "delegation_denied"})
    cleanup_only = is_cleanup_scope(grant)
    setup_only = is_devops_setup_scope(grant)
    resource_only = is_devops_resource_scope(grant)
    validate_active = active_grant
    if knowledge:
        from src.auth.l5_knowledge_native_consent import current_native_grant
        from src.internal.l5_knowledge_work import native_active_grant

        intent = await db.get(SetupIntent, (grant.tenant_id, body.operation_id), populate_existing=True)
        if actor.agent_registry_id != grant.service_id:
            raise HTTPException(403, detail={"code": "delegation_denied"})
        await current_native_grant(db, grant, intent)
        validate_active = native_active_grant
    elif resource_only:
        if actor.agent_registry_id != grant.service_id or not await current_devops_resource_scope(db, grant, admitted=True):
            raise HTTPException(403, detail={"code": "delegation_denied"})
    elif setup_only:
        if actor.agent_registry_id != grant.service_id or not await current_devops_setup_scope(db, grant):
            raise HTTPException(403, detail={"code": "delegation_denied"})
    elif cleanup_only:
        if actor.agent_registry_id != grant.service_id or not await current_cleanup_scope(db, grant, admitted=True):
            raise HTTPException(403, detail={"code": "delegation_denied"})
    else:
        grant = await service_product_scope(db, actor, grant.id, grant.product_id, grant.project_id, "credential:session")
    if grant is None or body.connection_ref.tenant_id != grant.tenant_id or body.connection_ref.project_id != grant.project_id:
        raise HTTPException(403, detail={"code": "delegation_denied"})
    if body.connection_ref.model_dump() not in (grant.sources or []):
        raise HTTPException(403, detail={"code": "delegation_denied"})
    if not (cleanup_only or setup_only or resource_only or knowledge):
        await scoped_product(db, actor, grant.product_id, grant.project_id, grant.id)
    credential = await db.get(UserCredential, grant.credential_id)
    if (
        credential is None
        or credential.org_id != grant.tenant_id
        or credential.service != "aws"
        or credential.credential_type != "aws_role"
        or body.connection_ref.source_connection_id != credential.id
        or body.connection_ref.record_type != "credential"
        or body.connection_ref.record_id != credential.id
        or body.connection_ref.record_revision != credential.aws_verified_version_id
    ):
        raise HTTPException(403, detail={"code": "delegation_denied"})
    member = await product_member(db, user_id=grant.created_by, org_id=grant.tenant_id)
    if member is None or not await db.scalar(select(Product.id).where(Product.id == grant.product_id, owner_scope(member, grant.tenant_id))):
        raise HTTPException(403, detail={"code": "delegation_denied"})
    owner = actor.model_copy(
        update={
            "account_type": "human",
            "user_id": member.id,
            "team_id": member.team_id,
            "org_id": grant.tenant_id,
            "is_admin": False,
        }
    )
    visible = _visible_credential_filter(owner)
    if not visible or not await db.scalar(select(UserCredential.id).where(UserCredential.id == credential.id, or_(*visible))):
        raise HTTPException(403, detail={"code": "delegation_denied"})
    try:
        verified_connection_evidence(credential)
    except HTTPException as exc:
        raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from exc
    permit = await work_authorizer(grant, body)
    if not isinstance(permit, L5WorkPermit):
        raise HTTPException(503, detail={"code": "work_authority_unavailable"})
    deadline = validate_active(grant, body, permit, minimum_remaining=timedelta(0))
    if not permit.session_policy or not body.lease_owner or body.lease_fence != permit.lease_fence:
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    selected_id = credential.id
    selected_version = credential.aws_verified_version_id
    selected_role = (credential.scopes or {}).get("role_arn")
    selected_account = (credential.scopes or {}).get("account_id")
    if not isinstance(selected_role, str) or not isinstance(selected_account, str):
        raise HTTPException(503, detail={"code": "selected_connection_unavailable"})

    async def validate(selected, *, revalidate=False):
        if selected.id != selected_id or selected.org_id != grant.tenant_id:
            raise HTTPException(403, detail={"code": "delegation_denied"})
        if revalidate:
            await db.refresh(grant)
            await db.refresh(selected)
            latest_permit = await work_authorizer(grant, body)
            if not isinstance(latest_permit, L5WorkPermit) or latest_permit != permit:
                raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
            validate_active(grant, body, latest_permit, minimum_remaining=timedelta(0))
            if body.connection_ref.model_dump() not in (grant.sources or []):
                raise HTTPException(403, detail={"code": "delegation_denied"})
            current_member = await product_member(db, user_id=grant.created_by, org_id=grant.tenant_id)
            if current_member is None or not await db.scalar(
                select(Product.id).where(Product.id == grant.product_id, owner_scope(current_member, grant.tenant_id))
            ):
                raise HTTPException(403, detail={"code": "delegation_denied"})
            visible = _visible_credential_filter(owner)
            if not visible or not await db.scalar(select(UserCredential.id).where(UserCredential.id == selected.id, or_(*visible))):
                raise HTTPException(403, detail={"code": "delegation_denied"})
            if selected.aws_verified_version_id != selected_version or (selected.scopes or {}).get("role_arn") != selected_role:
                raise HTTPException(409, detail={"code": "selected_connection_changed"})
        from src.agentauth.broker_identity import verify_selected_user_credential

        await verify_selected_user_credential(request, selected, db=db, revalidate=revalidate)

    bound = BindingResult(
        resolved_user_id=member.id,
        from_registry=True,
        drift_detected=False,
        body_user_id=None,
        tenant_id=grant.tenant_id,
    )
    delivered = await assume_bound_role(
        AssumeRoleRequestBody(user_id=member.id, agent_id=grant.service_id, task_id=body.operation_id, label=credential.label),
        request,
        db,
        secrets_manager,
        binding=bound,
        validate=validate,
        session_policy=permit.session_policy,
        audit_fields={"l5_delegation_id": grant.id, "l5_operation_id": body.operation_id, "l5_attempt_id": body.attempt_id},
    )
    identity = await verify_assumed_session(delivered, selected_role, selected_account)
    if permit.provider_identity is not None and any(
        identity.get(field) != permit.provider_identity.get(field) for field in ("account_id", "role_arn", "region")
    ):
        raise HTTPException(409, detail={"code": "selected_connection_changed"})
    expires_at = datetime.fromisoformat(delivered.expiration.replace("Z", "+00:00"))
    if expires_at.tzinfo is None or not utcnow() < min(expires_at, deadline):
        raise HTTPException(409, detail={"code": "stale_delegation_or_attempt"})
    await validate(credential, revalidate=True)
    setup_verified = permit.setup_verified
    if setup_verified is not None and grant.service_id == WORKER_IDS["well_architected"] and not cleanup_only:
        from src.l5_backend_well_architected import qualify_source_role

        provider = boto3.Session(
            aws_access_key_id=delivered.access_key_id,
            aws_secret_access_key=delivered.secret_access_key,
            aws_session_token=delivered.session_token,
            region_name=identity["region"],
        )
        try:
            qualification = await asyncio.to_thread(qualify_source_role, provider, setup_verified["well_architected_settings"], identity)
        except Exception as error:
            raise HTTPException(409, detail={"code": "provider_unqualified"}) from error
        await validate(credential, revalidate=True)
        setup_verified = {**setup_verified, "source_role_qualification": qualification}
    try:
        verified_connection_evidence(credential)
    except HTTPException as exc:
        raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from exc
    response.headers["Cache-Control"] = "no-store"
    return {
        "access_key_id": delivered.access_key_id,
        "secret_access_key": delivered.secret_access_key,
        "session_token": delivered.session_token,
        "expires_at": min(expires_at, deadline).isoformat().replace("+00:00", "Z"),
        "provider_identity": identity,
        "delegation_revision": grant.revision,
        "operation_id": body.operation_id,
        "attempt_id": body.attempt_id,
        **({"setup_verified": setup_verified} if setup_verified is not None else {}),
    }


@router.post("/knowledge-publication-session", operation_id="getL5KnowledgePublicationSession")
async def knowledge_publication_session(
    body: CredentialSessionRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    secrets_manager: SecretsManagerHelper = Depends(get_secrets_manager),
    work_authorizer: WorkAuthorizer = Depends(get_l5_work_authorizer),
):
    from src.auth.l5_knowledge_native_consent import NATIVE_PURPOSE, current_native_grant, native_scope, original_authority

    actor = service_actor(request)
    require_service_operation(request, "l5:knowledge-publication")
    source = await db.get(CredentialServiceDelegation, body.delegation_id, populate_existing=True)
    if source is None or actor.agent_registry_id != source.service_id:
        raise HTTPException(403, detail={"code": "knowledge_native_consent_required"})
    intent = await db.get(SetupIntent, (source.tenant_id, body.operation_id), populate_existing=True)
    original = await original_authority(db, intent)
    if original.id != source.id or original.revision != body.delegation_revision:
        raise HTTPException(403, detail={"code": "knowledge_source_consent_required"})
    expected = native_scope(intent, source)
    candidates = (
        await db.scalars(
            select(CredentialServiceDelegation).where(
                CredentialServiceDelegation.parent_delegation_id == source.id,
                CredentialServiceDelegation.operation_id == body.operation_id,
                CredentialServiceDelegation.revoked_at.is_(None),
                CredentialServiceDelegation.expires_at > utcnow(),
                CredentialServiceDelegation.execution_scope["purpose"].as_string() == NATIVE_PURPOSE,
            )
        )
    ).all()
    eligible = [grant for grant in candidates if grant.execution_scope == expected]
    if len(eligible) != 1:
        raise HTTPException(403 if not eligible else 409, detail={"code": "knowledge_native_consent_required"})
    native = eligible[0]
    await current_native_grant(db, native, intent)
    native_body = body.model_copy(update={"delegation_id": native.id, "delegation_revision": native.revision})
    result = await issue_credential_session(native_body, request, response, db, secrets_manager, work_authorizer, knowledge=True)
    return {
        **result,
        "source_delegation_id": source.id,
        "source_delegation_revision": source.revision,
        "native_delegation_id": native.id,
        "native_delegation_revision": native.revision,
    }
