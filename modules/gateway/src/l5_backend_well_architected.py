"""Well-Architected authority using current Product and provider records."""

import hashlib
import json
import os
import re
from datetime import UTC

import boto3
import botocore.session
import httpx
from fastapi import HTTPException
from l5_services import runtime_support
from l5_services.adapters.well_architected.runtime_callbacks import PublicationClient
from l5_services.models.binding import Binding
from starlette.concurrency import run_in_threadpool

from src.auth.aws_connection_authority import connection_material, require_assumed_identity, verified_connection_evidence
from src.internal.sts_assume_service import assume_role
from src.shared.config import get_settings
from src.shared.models.vault import CredentialServiceDelegation, UserCredential
from src.shared.services.secrets_manager import SecretsManagerHelper


def source_bucket(settings):
    selected = (settings.get("well_architected") or {}).get("source_store_ref") or {}
    bucket = selected.get("record_id", "").removeprefix("arn:aws:s3:::")
    if selected.get("record_type") != "artifact_store" or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("selected source bucket unavailable")
    return bucket


def setup_policy(settings, account, expires_at):
    role = settings["well_architected"]["execution_role_ref"]["record_id"]
    if not re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+", role):
        raise HTTPException(409, detail={"code": "provider_unqualified"})
    conditions = {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}}
    statements = [
        {
            "Effect": "Allow",
            "Action": [
                "wellarchitected:GetAgentProfile",
                "wellarchitected:ListAgentContexts",
                "wellarchitected:UpdateAgentProfile",
                "wellarchitected:DeleteAgentProfile",
            ],
            "Resource": f"arn:aws:wellarchitected:{settings['region']}:{account}:agent-profile/*",
            "Condition": conditions,
        },
        {
            "Effect": "Allow",
            "Action": ["wellarchitected:CreateAgentProfile", "wellarchitected:ListAgentProfiles"],
            "Resource": "*",
            "Condition": {**conditions, "StringEquals": {"aws:RequestedRegion": settings["region"]}},
        },
        {"Effect": "Allow", "Action": ["iam:GetRole", "iam:SimulatePrincipalPolicy"], "Resource": role, "Condition": conditions},
        {
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": role,
            "Condition": {**conditions, "StringEquals": {"iam:PassedToService": "wellarchitected.amazonaws.com"}},
        },
        {"Effect": "Allow", "Action": "s3:GetBucketLocation", "Resource": f"arn:aws:s3:::{source_bucket(settings)}", "Condition": conditions},
        {"Effect": "Allow", "Action": "support:DescribeServices", "Resource": "*", "Condition": conditions},
    ]
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))


def cleanup_policy(reference, account, region, expires_at):
    """Exact owned Profile deletion and identity/absence reads; no source role."""
    arn = reference.get("record_id") if isinstance(reference, dict) else None
    if not isinstance(arn, str) or not re.fullmatch(rf"arn:aws:wellarchitected:{re.escape(region)}:{account}:agent-profile/[A-Za-z0-9_-]+", arn):
        raise HTTPException(409, detail={"code": "ownership_required"})
    conditions = {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}, "StringEquals": {"aws:RequestedRegion": region}}
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["wellarchitected:GetAgentProfile", "wellarchitected:ListAgentContexts", "wellarchitected:DeleteAgentProfile"],
                    "Resource": arn,
                    "Condition": conditions,
                },
                {"Effect": "Allow", "Action": ["wellarchitected:ListAgentProfiles"], "Resource": "*", "Condition": conditions},
            ],
        },
        separators=(",", ":"),
    )


def qualify_source_role(provider, settings, identity):
    review = settings["well_architected"]
    role = review["execution_role_ref"]
    account, region = identity["account_id"], identity["region"]
    selected_role = role.get("record_id", "")
    if (
        role.get("record_type") != "role"
        or review.get("review_mode") != "source_only"
        or not re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+", selected_role)
        or not set(review.get("pillars") or []) <= {"SECURITY", "RESILIENCE", "PERFORMANCE", "COST_OPTIMIZATION"}
    ):
        raise ValueError("source-only execution role unavailable")
    iam = provider.client("iam", region_name=region)
    observed = iam.get_role(RoleName=selected_role.rsplit("/", 1)[-1])["Role"]
    statements = observed["AssumeRolePolicyDocument"]["Statement"]
    if isinstance(statements, dict):
        statements = [statements]
    if observed.get("Arn") != selected_role or not isinstance(statements, list) or not statements:
        raise ValueError("source-only execution trust is not qualified")
    for statement in statements:
        if (
            not isinstance(statement, dict)
            or statement.get("Effect") != "Allow"
            or set(statement) - {"Sid", "Effect", "Action", "Principal", "Condition"}
        ):
            raise ValueError("source-only execution trust contains an unsupported statement")
        actions = statement.get("Action")
        actions = [actions] if isinstance(actions, str) else actions or []
        principal = statement.get("Principal")
        if not isinstance(principal, dict) or set(principal) != {"Service"}:
            raise ValueError("source-only execution trust contains an unsupported principal")
        principals = principal["Service"]
        principals = [principals] if isinstance(principals, str) else principals
        conditions = statement.get("Condition") or {}
        allowed_conditions = {"StringEquals": {"aws:SourceAccount": account}}
        if "ArnLike" in conditions:
            allowed_conditions["ArnLike"] = {"aws:SourceArn": f"arn:aws:wellarchitected:{region}:{account}:agent-profile/*"}
        if actions != ["sts:AssumeRole"] or principals != ["wellarchitected.amazonaws.com"] or conditions != allowed_conditions:
            raise ValueError("source-only execution trust is not qualified")
    bucket = source_bucket(settings)
    location = provider.client("s3", region_name=region).get_bucket_location(Bucket=bucket)["LocationConstraint"] or "us-east-1"
    if location == "EU":
        location = "eu-west-1"
    if location != region:
        raise ValueError("source bucket and Profile must share a Region")
    resource = f"arn:aws:s3:::{bucket}/l5/well_architected/work/*"
    simulation = iam.simulate_principal_policy(PolicySourceArn=selected_role, ActionNames=["s3:GetObjectVersion"], ResourceArns=[resource])
    evaluations = simulation.get("EvaluationResults") or []
    if (
        len(evaluations) != 1
        or evaluations[0].get("EvalActionName") != "s3:GetObjectVersion"
        or evaluations[0].get("EvalResourceName") != resource
        or evaluations[0].get("EvalDecision") != "allowed"
        or evaluations[0].get("MissingContextValues")
        or simulation.get("IsTruncated")
    ):
        raise ValueError("versioned source-read permission is not qualified")
    provider.client("support", region_name="us-east-1").describe_services()
    return {
        "account_id": account,
        "region": region,
        "broker_role_arn": identity["role_arn"],
        "execution_role_arn": selected_role,
        "trust": "qualified",
        "source_permissions": "qualified",
        "support": "eligible",
    }


class WellArchitectedService:
    def __init__(self, backend):
        self.backend = backend

    async def selected_provider(self, credential, settings, *, session_policy=None, audit_run_id=None, service_grant=None):
        from src.auth.l5_schedule_scope import schedule_product_scope

        actor = self.backend.actor
        tenant_id, owner_id = actor.org_id, actor.user_id
        if service_grant is not None:
            current = await schedule_product_scope(
                self.backend.db, actor, service_grant.id, service_grant.product_id, service_grant.project_id, "credential:session"
            )
            from src.l5_service_identity import worker_id

            if current is None or current.service_id != worker_id("well_architected") or current.credential_id != credential.id:
                raise HTTPException(404, detail={"code": "not_found"})
            tenant_id, owner_id = current.tenant_id, current.created_by
        verified_connection_evidence(credential)
        connection = settings.get("connection_ref") or {}
        if (
            (actor.account_type != "human" and service_grant is None)
            or credential.org_id != tenant_id
            or credential.user_id != owner_id
            or credential.id != connection.get("record_id")
            or credential.id != connection.get("source_connection_id")
            or credential.aws_verified_version_id != connection.get("record_revision")
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        evidence = verified_connection_evidence(credential)
        helper = SecretsManagerHelper(region_name=get_settings().aws_region)
        try:
            current = await run_in_threadpool(helper.current_version_id, credential.secret_arn)
            if current != credential.aws_verified_version_id:
                raise ValueError("selected connection changed")
            secret, served = await run_in_threadpool(helper.get_secret_at_version, credential.secret_arn, current)
            if served != current:
                raise ValueError("selected connection changed")
            role, external_id, account = connection_material(json.loads(secret), credential.scopes or {}, credential)
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "sts:GetCallerIdentity",
                            "wellarchitected:GetAgentProfile",
                            "wellarchitected:ListAgentContexts",
                            "iam:GetRole",
                            "iam:SimulatePrincipalPolicy",
                            "s3:GetBucketLocation",
                            "support:DescribeServices",
                        ],
                        "Resource": "*",
                    }
                ],
            }
            issued = await run_in_threadpool(
                assume_role,
                role_arn=role,
                external_id=external_id,
                session_duration_seconds=900,
                default_region=settings["region"],
                user_id=owner_id,
                agent_id="l5-well-architected-admission",
                task_id=audit_run_id or self.backend.admission_key or "profile-readback",
                label=credential.label,
                session_policy=json.dumps(session_policy if session_policy is not None else policy, separators=(",", ":")),
            )
            require_assumed_identity(issued, role)
            provider = boto3.Session(
                aws_access_key_id=issued.access_key_id,
                aws_secret_access_key=issued.secret_access_key,
                aws_session_token=issued.session_token,
                region_name=settings["region"],
            )
            observed = await run_in_threadpool(provider.client("sts", region_name=settings["region"]).get_caller_identity)
            if observed.get("Account") != account or observed.get("Arn") != issued.assumed_role_arn:
                raise ValueError("selected provider identity changed")
            if await run_in_threadpool(helper.current_version_id, credential.secret_arn) != current:
                raise ValueError("selected connection changed")
            await self.backend.db.refresh(credential)
            if verified_connection_evidence(credential) != evidence:
                raise ValueError("selected connection changed")
            if (
                service_grant is not None
                and await schedule_product_scope(
                    self.backend.db, actor, service_grant.id, service_grant.product_id, service_grant.project_id, "credential:session"
                )
                is None
            ):
                raise ValueError("selected service consent changed")
            return provider, {"account_id": account, "region": settings["region"], "role_arn": role}
        except Exception as error:
            raise HTTPException(503, detail={"code": "selected_connection_unavailable"}) from error

    async def qualify(self, credential, settings):
        provider, identity = await self.selected_provider(credential, settings)
        try:
            return await run_in_threadpool(qualify_source_role, provider, settings, identity)
        except Exception as error:
            raise HTTPException(409, detail={"code": "provider_unqualified"}) from error

    async def profile_client(self, verified, version):
        credential = await self.backend.db.get(UserCredential, verified["connection_ref"]["record_id"])
        if credential is None:
            raise HTTPException(404, detail={"code": "not_found"})
        provider, identity = await self.selected_provider(credential, version.settings)
        if any(identity[field] != verified[field] for field in ("account_id", "region", "role_arn")):
            raise HTTPException(409, detail={"code": "selected_connection_changed"})
        return provider.client("wellarchitected", region_name=identity["region"])

    async def work_authority(self, configuration, version, run_input, actor):
        if actor != self.backend.actor:
            raise HTTPException(404, detail={"code": "not_found"})
        verified, sources = await self.backend._security_work_target(configuration, version, run_input, service_id="well_architected")
        binding = await self.backend.db.get(Binding, (actor.org_id, verified["binding_id"]))
        grant = await self.backend.db.get(CredentialServiceDelegation, verified["delegation_id"])
        review = version.settings["well_architected"]
        goals = review.get("goal_refs") or []
        if any(goal not in run_input["sources"] for goal in goals):
            raise HTTPException(404, detail={"code": "not_found"})
        expiry = grant.expires_at.replace(tzinfo=grant.expires_at.tzinfo or UTC).isoformat()
        model = botocore.session.get_session().get_service_model("wellarchitected")
        verified.update(
            native_profile_ref=binding.native_resource_ref,
            profile_hash=binding.native_resource_ref["record_revision"],
            source_refs=run_input["sources"],
            provider_identity=binding.provider_identity,
            native_creator=binding.provider_identity["role_arn"],
            generation_scope={"pillars": review["pillars"]},
            goal_refs=goals,
            generation_context={},
            generation_model_hash=hashlib.sha256(json.dumps(model._service_description, sort_keys=True).encode()).hexdigest(),
            architecture_coverage={},
            source_retention={"required_until": expiry, "policy_expires_at": expiry},
        )
        sources.verified = verified
        version.state, version.gaps = "ready", []
        await self.backend.db.flush()
        return verified, sources

    def consumer_resolver(self, scope):
        if (
            self.backend.actor.account_type != "human"
            or scope.get("tenant_id") != self.backend.actor.org_id
            or set(scope) != {"tenant_id", "product_id", "project_id"}
            or not all(isinstance(value, str) and value for value in scope.values())
        ):
            raise HTTPException(404, detail={"code": "not_found"})
        configured = os.environ.get("L5_WA_PEER_CONTRACTS")
        if not configured:
            return {}
        contracts = json.loads(configured)
        PublicationClient.validate_contracts(contracts)
        from src.l5_consumer_clients import HumanWAConsumer

        return {
            destination: HumanWAConsumer(
                actor=self.backend.actor, sessions=self.backend.sessions, scope=scope, destination=destination, contract=contract
            )
            for destination, contract in contracts.items()
        }


class PeerTransport:
    def _client(self):
        return httpx.AsyncClient(
            base_url=runtime_support.gateway_url(),
            auth=runtime_support.SignedGateway(get_settings().aws_region),
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        )

    async def get(self, path, **kwargs):
        async with self._client() as transport:
            return await transport.get(path, **kwargs)

    async def post(self, path, **kwargs):
        async with self._client() as transport:
            return await transport.post(path, **kwargs)


class ScopedCallback:
    def __init__(self, method):
        self.method = method

    def bind_backend(self, backend):
        return getattr(WellArchitectedService(backend), self.method)

    def __call__(self, backend, *args):
        return self.bind_backend(backend)(*args)


def gateway_backends():
    return {
        "l5_wa_authority": ScopedCallback("work_authority"),
        "l5_wa_profile_client": ScopedCallback("profile_client"),
        "l5_wa_consumer_resolver": ScopedCallback("consumer_resolver"),
        "l5_wa_qualify": ScopedCallback("qualify"),
    }
