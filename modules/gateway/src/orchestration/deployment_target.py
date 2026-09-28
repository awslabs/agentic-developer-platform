"""Resolve a reviewed physical target through a registered user's vault access."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import boto3
from botocore.config import Config

from src.internal.sts_assume_service import assume_role
from src.shared.services.secrets_manager import SecretsManagerHelper

from .deployment_manifest import PhysicalTarget, TargetEvidence
from .execution_policy import Action
from .review_cycle import CycleBlockedError
from .user_credentials import resolve_user_credential


@dataclass(frozen=True)
class ResolvedTarget:
    physical: PhysicalTarget
    credential_id: str
    credential_label: str
    principal_user_id: str
    role_arn: str
    observation: object | None = None


class DeploymentTargetResolver:
    def __init__(self, *, secrets=None, assume=assume_role, session_factory=boto3.Session):
        self.secrets, self.assume, self.session_factory = secrets, assume, session_factory

    async def resolve(
        self, session, *, entry, policy, principal_user_id, execution_id, inspect_target=None, authorize_scope=None, action=Action.DEPLOY
    ):
        if action not in {Action.DEPLOY, Action.EVALUATE}:
            raise CycleBlockedError("deployment_target_action_unsupported")
        if entry.connection_id not in policy.environment_connection_ids:
            raise CycleBlockedError("deployment_connection_not_permitted")
        authority = policy.user_credentials
        if authority is None or action not in authority.actions or entry.connection_id not in authority.vault_credential_ids:
            raise CycleBlockedError("deployment_user_credential_not_approved")
        credential = await resolve_user_credential(session, org_id=policy.org_id, user_id=principal_user_id, credential_id=entry.connection_id)
        if credential.credential_type != "aws_role" or credential.service != "aws":
            raise CycleBlockedError("deployment_connection_requires_registered_aws_role")
        if entry.resource_kind != "eks-namespace" or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}/[a-z0-9][a-z0-9-]{0,62}", entry.resource_id or ""
        ):
            raise CycleBlockedError("deployment_resource_boundary_unverifiable")
        secret = await asyncio.to_thread((self.secrets or SecretsManagerHelper()).get_secret, credential.secret_arn)
        config = json.loads(secret)
        role_arn = config.get("role_arn", "")
        match = re.fullmatch(r"arn:(aws|aws-us-gov|aws-cn):iam::([0-9]{12}):role/[A-Za-z0-9+=,.@_/-]+", role_arn)
        if match is None:
            raise CycleBlockedError("deployment_registered_role_invalid")
        if role_arn not in authority.aws_role_arns:
            raise CycleBlockedError("deployment_registered_role_not_approved")
        if authorize_scope is not None:
            authorize_scope(credential.id, role_arn)
        temporary = await asyncio.to_thread(
            self.assume,
            role_arn=role_arn,
            external_id=config.get("external_id"),
            session_duration_seconds=900,
            default_region=config.get("default_region", "us-east-1"),
            user_id=principal_user_id,
            agent_id="deployment-controller",
            task_id=execution_id,
            label=credential.label,
        )
        deadline = datetime.fromisoformat(temporary.expiration.replace("Z", "+00:00"))
        if deadline.tzinfo is None or deadline <= datetime.now(UTC):
            raise CycleBlockedError("deployment_target_credential_expired")
        cluster, namespace = entry.resource_id.split("/", 1)

        def read_identity():
            scoped = self.session_factory(
                aws_access_key_id=temporary.access_key_id,
                aws_secret_access_key=temporary.secret_access_key,
                aws_session_token=temporary.session_token,
                region_name=temporary.region,
            )
            bounded = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1})
            account = scoped.client("sts", config=bounded).get_caller_identity()["Account"]
            description = scoped.client("eks", config=bounded).describe_cluster(name=cluster)["cluster"]
            if (
                account != match[2]
                or description.get("arn") != f"arn:{match[1]}:eks:{temporary.region}:{account}:cluster/{cluster}"
                or description.get("name") != cluster
            ):
                raise CycleBlockedError("deployment_target_identity_mismatch")
            return account, inspect_target(scoped, description, namespace) if inspect_target else None

        account, observation = await asyncio.to_thread(read_identity)
        return ResolvedTarget(
            PhysicalTarget(
                provider="aws",
                account_id=account,
                region=temporary.region,
                resource_kind="eks-namespace",
                resource_id=f"{cluster}/{namespace}",
                evidence=TargetEvidence(source=f"registered-aws-role:{credential.id}", verified_at=datetime.now(UTC).isoformat()),
            ),
            credential.id,
            credential.label,
            principal_user_id,
            role_arn,
            observation,
        )
