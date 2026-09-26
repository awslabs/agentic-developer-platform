"""Gateway-only recovery observations and settlement; no worker provider credentials."""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.database import async_session_factory
from app.routers.heartbeat import _authenticated_submitter


class RecoveryRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request):
            try:
                return await handler(request)
            except (HTTPException, RequestValidationError):
                raise
            except Exception:
                # Provider/database errors may carry response bodies or credentials.
                raise HTTPException(503, "recovery observation unavailable") from None

        return guarded


router = APIRouter(
    route_class=RecoveryRoute,
    prefix="/internal/controller/recovery",
    tags=["controller-recovery"],
)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=255)
    org_id: str = Field(min_length=1, max_length=255)
    workspace_id: str = Field(min_length=1, max_length=255)
    holder: str = Field(min_length=1, max_length=255)
    attempt_id: str = Field(min_length=1, max_length=255)
    fence_token: int = Field(strict=True, ge=1)


class ObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim: Claim
    query_id: str = Field(min_length=1, max_length=255)


class InventoryRequest(ObservationRequest):
    allocation_id: str = Field(min_length=1, max_length=255)


class StatusRequest(ObservationRequest):
    idempotency_key: str = Field(min_length=1, max_length=255)


class SettlementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_id: str = Field(min_length=1, max_length=255)
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_id: str = Field(min_length=1, max_length=255)
    job_id: str = Field(min_length=1, max_length=255)
    attempt_id: str = Field(min_length=1, max_length=255)
    org_id: str = Field(min_length=1, max_length=255)
    workspace_id: str = Field(min_length=1, max_length=255)
    accounting: dict


def composition(request):
    value = getattr(request.app.state, "trust_composition", None)
    if value is None or not callable(getattr(value, "operation_connect", None)):
        raise HTTPException(503, "recovery operation store unavailable")
    return value


def permitted(submitter, org_id):
    if "controller_recovery/" + org_id not in submitter.lease_scopes:
        raise HTTPException(403, "recovery observer scope refused")


async def claim_operation(request, claim):
    from harness_jobs.identity import ResolvedPrincipal
    from harness_jobs.leases import read_lease
    from harness_jobs.recovery_grant import RecoveryGrant
    from superplane_executor.authority import VerifiedOperation

    async with composition(request).operation_connect() as connection:
        lease = await read_lease(connection, operation_id=claim.operation_id)
        if (
            lease is None
            or any(
                getattr(lease, key) != value
                for key, value in claim.model_dump().items()
            )
            or min(lease.expires_at, lease.runtime_deadline) <= datetime.now(UTC)
        ):
            raise HTTPException(403, "recovery claim expired or replaced")
        subject = await connection.fetchval(
            "SELECT subject FROM harness_recovery_claim_bindings WHERE operation_id=$1 AND fence_token=$2 AND org_id=$3 AND workspace_id=$4 AND holder=$5 AND attempt_id=$6",
            lease.operation_id,
            lease.fence_token,
            lease.org_id,
            lease.workspace_id,
            lease.holder,
            lease.attempt_id,
        )
        row = await connection.fetchrow(
            "SELECT o.job_id,o.plan_digest,o.request_payload,a.reservation_state,a.max_resource_units,a.max_runtime_seconds,a.max_cost_micros "
            "FROM harness_operations o JOIN harness_approval_consumption a USING(operation_id) "
            "WHERE o.operation_id=$1 AND o.org_id=$2 AND o.workspace_id=$3 "
            "AND a.org_id=o.org_id AND a.workspace_id=o.workspace_id AND a.plan_digest=o.plan_digest "
            "AND a.reservation_state IN ('confirmed','retained')",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
        )
    if not subject or row is None:
        raise HTTPException(403, "original recovery admission unavailable")
    actor = ResolvedPrincipal(
        lease.org_id, lease.workspace_id, subject, frozenset({"workspace:recover"})
    )
    return VerifiedOperation(RecoveryGrant(actor, lease), **dict(row))


async def claim_context(request, claim):
    from superplane_executor.plan import Plan

    operation = await claim_operation(request, claim)
    async with composition(request).operation_connect() as connection:
        from superplane_executor.deployment_registry import (
            require_deployment_registration,
        )

        await require_deployment_registration(connection, operation)
        target = await connection.fetchrow(
            "SELECT w.id::text AS workspace_id,w.org_id::text AS domain_org_id,w.namespace_name AS namespace,"
            "c.id::text AS cluster_id,c.eks_cluster_arn AS cluster_arn,c.endpoint,"
            "w.shared_cluster_id::text AS shared_cluster_id FROM workspaces w "
            "JOIN clusters c ON c.id=w.cluster_id AND c.org_id=w.org_id WHERE w.id::text=$1 AND w.org_id::text=$2",
            claim.workspace_id,
            claim.org_id,
        )
    if target is None:
        raise HTTPException(403, "original recovery registration unavailable")
    if target["shared_cluster_id"] is not None:
        raise HTTPException(403, "shared controller recovery is unavailable")
    target = dict(target)
    target.pop("shared_cluster_id")
    return operation, dict(target), Plan.read(operation, target)


@asynccontextmanager
async def observation_provider(request, claim):
    import boto3
    from superplane_executor.provider import Provider
    from superplane_executor.skypilot import SkyPilot
    from superplane_executor.workspace import Workspace

    role = os.environ.get("SUPERPLANE_RECOVERY_OBSERVATION_ROLE_ARN", "")
    directory = os.environ.get("SUPERPLANE_RECOVERY_WORKSPACE_CREDENTIALS_DIR", "")
    management = os.environ.get("SUPERPLANE_MANAGEMENT_API_SERVER", "")
    sky_url = os.environ.get("SKYPILOT_URL", "")
    sky_token = os.environ.get("SKYPILOT_SERVICE_TOKEN_FILE", "")
    if not all((role, directory, management, sky_url, sky_token)):
        raise HTTPException(503, "recovery observation provider unavailable")

    class ReadWorkspace(Workspace):
        async def request(
            self, operation, target, method, path, *, body=None, headers=None
        ):
            if (
                method != "GET"
                or body is not None
                or headers
                or "/secrets" in path
                or "/exec" in path
                or "/proxy" in path
            ):
                raise HTTPException(403, "recovery workspace mutation refused")
            return await super().request(operation, target, method, path)

    class ReadProvider(Provider):
        async def session_for(self, operation, plan):
            if not re.fullmatch(
                r"arn:aws:iam::"
                + re.escape(plan.data["provider_account_id"])
                + r":role/[A-Za-z0-9+=,.@_/-]+",
                role,
            ):
                raise HTTPException(403, "recovery provider account refused")
            await claim_context(request, claim)
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "ec2:Describe*",
                            "ec2:SearchTransitGatewayRoutes",
                            "ec2:GetTransitGatewayRouteTableAssociations",
                            "eks:DescribeCluster",
                            "sts:GetCallerIdentity",
                            "ssm:GetCommandInvocation",
                        ],
                        "Resource": "*",
                    }
                ],
            }

            def assume():
                result = self.session.client(
                    "sts", region_name=plan.cluster_region
                ).assume_role(
                    RoleArn=role,
                    RoleSessionName="superplane-recovery-observe",
                    DurationSeconds=900,
                    Policy=json.dumps(policy),
                )["Credentials"]
                session = boto3.Session(
                    aws_access_key_id=result["AccessKeyId"],
                    aws_secret_access_key=result["SecretAccessKey"],
                    aws_session_token=result["SessionToken"],
                    region_name=plan.cluster_region,
                )
                identity = session.client(
                    "sts", region_name=plan.cluster_region
                ).get_caller_identity()
                prefix = f"arn:aws:sts::{plan.data['provider_account_id']}:assumed-role/{role.rsplit('/', 1)[-1]}/"
                if identity.get("Account") != plan.data[
                    "provider_account_id"
                ] or not identity.get("Arn", "").startswith(prefix):
                    raise HTTPException(403, "recovery provider identity refused")
                return session

            session = await asyncio.to_thread(assume)
            await claim_context(request, claim)
            return session, role

    pool = SimpleNamespace(acquire=composition(request).operation_connect)
    sky = SkyPilot(sky_url, sky_token)
    try:
        provider = ReadProvider(
            sky=sky,
            workspace=ReadWorkspace(directory, management),
            domain_pool=pool,
            execution_pool=pool,
        )
        provider.node_observation_authorize = lambda: claim_context(request, claim)
        yield provider
    finally:
        await sky.aclose()


@router.post("/inventory")
async def inventory(
    body: InventoryRequest,
    request: Request,
    submitter=Depends(_authenticated_submitter),
):
    permitted(submitter, body.claim.org_id)
    from workspace_provisioning.recovery_inventory import ProviderInventory

    async def context(lease):
        if any(
            getattr(lease, key) != value
            for key, value in body.claim.model_dump().items()
        ):
            raise HTTPException(403, "recovery claim changed")
        return await claim_context(request, body.claim)

    operation, _, _ = await claim_context(request, body.claim)
    async with observation_provider(request, body.claim) as provider:
        result = await ProviderInventory(provider=provider, context=context).snapshot(
            operation.grant.lease, body.allocation_id, body.query_id
        )
    await claim_context(request, body.claim)
    permitted(await _authenticated_submitter(request), body.claim.org_id)
    return {
        **result,
        "version": 1,
        "observation_only": True,
        "allocation_id": body.allocation_id,
        "claim": body.claim.model_dump(),
        "query_id": body.query_id,
        "checked_at": datetime.now(UTC).isoformat(),
    }


@router.post("/observe")
async def observe(
    body: StatusRequest, request: Request, submitter=Depends(_authenticated_submitter)
):
    from superplane_executor.recovery_observation import observe_request

    permitted(submitter, body.claim.org_id)
    operation, target, plan = await claim_context(request, body.claim)
    async with composition(request).operation_connect() as connection:
        call = await connection.fetchrow(
            "SELECT c.* FROM harness_provider_call_intent c "
            "WHERE c.idempotency_key=$1 AND c.operation_id=$2 AND c.org_id=$3 AND c.workspace_id=$4 "
            "AND c.provider='aws' AND c.target=$5 AND c.allocation_id=$6",
            body.idempotency_key,
            body.claim.operation_id,
            body.claim.org_id,
            body.claim.workspace_id,
            plan.cluster_name,
            operation.request.parameters["allocation_id"],
        )
    if call is None:
        raise HTTPException(403, "journalled provider request unavailable")
    from superplane_executor.recovery_workload import selected_call

    selected_call(operation, plan, call)
    # The domain transport journal and shared intent are separate authorities.
    # A mismatched journal must not disappear through a permissive LEFT JOIN.
    async with async_session_factory() as db:
        journal = (
            (
                await db.execute(
                    text(
                        "SELECT * FROM controller_provider_requests WHERE idempotency_key=:key"
                    ),
                    {"key": body.idempotency_key},
                )
            )
            .mappings()
            .first()
        )
    if journal is not None and any(
        journal[key] != expected
        for key, expected in {
            "operation_id": call["operation_id"],
            "org_id": call["org_id"],
            "workspace_id": call["workspace_id"],
            "operation_kind": call["operation_kind"],
            "cluster_name": call["target"],
        }.items()
    ):
        raise HTTPException(403, "original transport journal binding changed")
    request_id = journal["request_id"] if journal is not None else None

    async def authorize():
        current, current_target, current_plan = await claim_context(request, body.claim)
        if (
            current.request != operation.request
            or current.plan_digest != operation.plan_digest
            or current_target != target
            or current_plan != plan
        ):
            raise HTTPException(403, "original recovery context changed")

    async with observation_provider(request, body.claim) as provider:
        outcome, reference = await observe_request(
            provider,
            operation,
            plan,
            operation_kind=call["operation_kind"],
            request_id=request_id,
            target=target,
            call=call,
            authorize=authorize,
        )
    await authorize()
    permitted(await _authenticated_submitter(request), body.claim.org_id)
    return {
        "version": 1,
        "observation_only": True,
        "claim": body.claim.model_dump(),
        "query_id": body.query_id,
        "checked_at": datetime.now(UTC).isoformat(),
        "outcome": outcome,
        "provider_ref": reference,
    }


@router.post("/lifecycle")
async def lifecycle(
    body: StatusRequest, request: Request, submitter=Depends(_authenticated_submitter)
):
    from app.services.lifecycle_recovery import observe_lifecycle

    permitted(submitter, body.claim.org_id)
    result = await observe_lifecycle(request, body)
    permitted(await _authenticated_submitter(request), body.claim.org_id)
    return {
        **result,
        "version": 1,
        "observation_only": True,
        "claim": body.claim.model_dump(),
        "query_id": body.query_id,
        "checked_at": datetime.now(UTC).isoformat(),
    }


@router.post("/account-creation")
async def account_creation(
    body: StatusRequest, request: Request, submitter=Depends(_authenticated_submitter)
):
    from app.services.account_creation_recovery import observe_account

    permitted(submitter, body.claim.org_id)
    result = await observe_account(request, body)
    permitted(await _authenticated_submitter(request), body.claim.org_id)
    return {
        **result,
        "version": 1,
        "observation_only": True,
        "claim": body.claim.model_dump(),
        "query_id": body.query_id,
        "checked_at": datetime.now(UTC).isoformat(),
    }


@router.post("/bootstrap")
async def bootstrap(
    body: StatusRequest, request: Request, submitter=Depends(_authenticated_submitter)
):
    from app.services.bootstrap_recovery import observe_bootstrap

    permitted(submitter, body.claim.org_id)
    result = await observe_bootstrap(request, body)
    permitted(await _authenticated_submitter(request), body.claim.org_id)
    return {
        **result,
        "version": 1,
        "observation_only": True,
        "claim": body.claim.model_dump(),
        "query_id": body.query_id,
        "checked_at": datetime.now(UTC).isoformat(),
    }


@router.post("/settlement")
async def settlement(
    body: SettlementRequest,
    request: Request,
    submitter=Depends(_authenticated_submitter),
):
    permitted(submitter, body.org_id)
    ledger = getattr(composition(request), "ledger", None)
    if not callable(getattr(ledger, "deliver_settlement", None)):
        raise HTTPException(503, "recovery settlement ledger unavailable")
    receipt = await ledger.deliver_settlement(**body.model_dump())
    return {"receipt_id": receipt}
