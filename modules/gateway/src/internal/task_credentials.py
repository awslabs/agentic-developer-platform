"""A restricted session of the existing worker principal for customer STS calls.

Customer roles may trust that exact principal. The returned session permits only
cross-account role assumption, never platform resources. Kubernetes authorization
does not honor IAM session policies, so source-role EKS access is checked before
every issuance and prevents delivery until removed through the scoped rollout.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import ssl
from datetime import UTC, datetime
from pathlib import Path

import boto3
import httpx
import yaml
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.internal.auth_deps import verify_internal_or_irsa
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.audit import AuditLog

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/v1", tags=["internal"])
_ROLE = re.compile(r"arn:(aws(?:-[a-z-]+)?):iam::([0-9]{12}):role/(.+)\Z")
_SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class TaskCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    invocation_id: str


def task_session_policy(partition: str, account: str, targets: list[str] | None = None) -> dict:
    """Explicit denies also cover resource-policy grants to role sessions."""
    sts_actions = ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"]
    scope = {"Resource": targets} if targets is not None else {"NotResource": f"arn:{partition}:iam::{account}:role/*"}
    if targets is not None and (
        not targets
        or any(not _ROLE.fullmatch(role) or _ROLE.fullmatch(role)[1] != partition or _ROLE.fullmatch(role)[2] == account for role in targets)
    ):
        raise ValueError("no permitted customer role targets")
    statements = [
        {"Effect": "Allow", "Action": sts_actions, **scope},
        {"Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
        {"Effect": "Deny", "NotAction": [*sts_actions, "sts:GetCallerIdentity"], "Resource": "*"},
        {"Effect": "Deny", "Action": sts_actions, "Resource": f"arn:{partition}:iam::{account}:role/*"},
    ]
    if targets is not None:
        statements.append({"Effect": "Deny", "Action": sts_actions, "NotResource": targets})
    return {"Version": "2012-10-17", "Statement": statements}


def _aws_auth_config(cluster_info: dict) -> dict:
    # Read the same cluster inspected through EKS, not an unrelated local
    # ConfigMap if configuration points at a different cluster.
    context = ssl.create_default_context(cadata=base64.b64decode(cluster_info["certificateAuthority"]["data"], validate=True).decode())
    endpoint = cluster_info["endpoint"]
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise ValueError("cluster endpoint unavailable")
    token = (_SA / "token").read_text().strip()
    if not token:
        raise ValueError("cluster identity unavailable")
    with httpx.Client(base_url=endpoint, verify=context, trust_env=False, follow_redirects=False, timeout=5) as client:
        response = client.get("/api/v1/namespaces/kube-system/configmaps/aws-auth", headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    data = response.json().get("data", {})
    if not isinstance(data, dict):
        raise ValueError("cluster access mappings unavailable")
    return data


def verify_source_role_isolation(eks, *, cluster: str, role_arn: str, account: str) -> None:
    cluster_info = eks.describe_cluster(name=cluster)["cluster"]
    mode = cluster_info["accessConfig"]["authenticationMode"]
    if mode not in {"API", "API_AND_CONFIG_MAP", "CONFIG_MAP"}:
        raise ValueError("cluster authentication mode unavailable")
    if mode != "CONFIG_MAP":
        try:
            eks.describe_access_entry(clusterName=cluster, principalArn=role_arn)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        else:
            raise ValueError("task source role retains EKS access")
    if mode != "API":
        mappings = _aws_auth_config(cluster_info)
        for name in ("mapRoles", "mapUsers", "mapAccounts"):
            entries = yaml.safe_load(mappings.get(name, "[]"))
            if entries is None:
                entries = []
            if not isinstance(entries, list):
                raise ValueError("cluster access mappings unavailable")
            for entry in entries:
                if name == "mapAccounts":
                    if not isinstance(entry, str | int) or not re.fullmatch(r"[0-9]{12}", str(entry)):
                        raise ValueError("cluster account mapping unavailable")
                    if str(entry) == account:
                        raise ValueError("task source account retains EKS access")
                else:
                    key = "rolearn" if name == "mapRoles" else "userarn"
                    if not isinstance(entry, dict) or not isinstance(entry.get(key), str):
                        raise ValueError("cluster principal mapping unavailable")
                    # aws-auth historically strips IAM role paths. Both forms
                    # name this same principal and must block source delivery.
                    normalized_role = role_arn.rsplit(":role/", 1)[0] + ":role/" + role_arn.rsplit("/", 1)[-1]
                    if entry[key] in {role_arn, normalized_role}:
                        raise ValueError("task source role retains EKS access")


def issue_task_session(*, invocation_id: str, not_after: datetime, targets: list[str] | None = None) -> dict:
    role_arn = os.environ.get("AGENT_TASK_SOURCE_ROLE_ARN", "")
    cluster = os.environ.get("AGENT_TASK_SOURCE_EKS_CLUSTER", "")
    match = _ROLE.fullmatch(role_arn)
    if not match or not cluster or os.environ.get("AGENT_TASK_SOURCE_ISOLATION_CONFIRMED", "false").lower() != "true":
        raise ValueError("task source identity is not configured")
    partition, account, _ = match.groups()
    region = get_settings().aws_region
    sts = boto3.client("sts", region_name=region)
    if sts.get_caller_identity()["Account"] != account:
        raise ValueError("task source identity is outside the platform account")
    verify_source_role_isolation(boto3.client("eks", region_name=region), cluster=cluster, role_arn=role_arn, account=account)
    seconds = min(3600, int((not_after - datetime.now(UTC)).total_seconds()) - 30)
    if seconds < 900:
        raise ValueError("task source authorization is shorter than the STS minimum")
    policy = json.dumps(task_session_policy(partition, account, targets), separators=(",", ":"))
    if len(policy) > 2048:
        raise ValueError("customer role scope exceeds the STS session-policy limit")
    result = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="adp-task-" + re.sub(r"[^a-zA-Z0-9+=,.@_-]", "-", invocation_id)[:55],
        DurationSeconds=seconds,
        Policy=policy,
    )
    credentials = result["Credentials"]
    expiry = credentials["Expiration"]
    if not isinstance(expiry, datetime) or expiry.tzinfo is None or not datetime.now(UTC) < expiry <= not_after:
        raise ValueError("task source session exceeds its authorization")
    return {
        "Version": 1,
        "AccessKeyId": credentials["AccessKeyId"],
        "SecretAccessKey": credentials["SecretAccessKey"],
        "SessionToken": credentials["SessionToken"],
        "Expiration": expiry.isoformat(),
    }


@router.post("/worker-task-credentials")
async def worker_task_credentials(
    body: TaskCredentialRequest, request: Request, response: Response, _: None = Depends(verify_internal_or_irsa), db: AsyncSession = Depends(get_db)
) -> dict:
    grant = getattr(request.state, "agent_broker_grant", None)
    if grant is None or grant.expires_at is None or not grant.is_live(datetime.now(UTC)):
        raise HTTPException(404, "not found")
    try:
        result = await run_in_threadpool(issue_task_session, invocation_id=body.invocation_id, not_after=grant.expires_at)
    except (ValueError, OSError, KeyError, ClientError, BotoCoreError, httpx.HTTPError, yaml.YAMLError):
        logger.warning("Worker task source refused", extra={"invocation_id": body.invocation_id, "grant_id": grant.grant_id})
        raise HTTPException(503, "customer task identity unavailable") from None
    from src.agentauth.broker_identity import verify_broker_worker

    # Isolation/provider lookups may be slow. Recheck live authority before any
    # source credentials leave the gateway; revoked work receives no session.
    await verify_broker_worker(request)
    current = getattr(request.state, "agent_broker_grant", None)
    expiry = datetime.fromisoformat(result["Expiration"])
    if current is None or current.expires_at is None or not current.is_live(datetime.now(UTC)) or expiry > current.expires_at:
        raise HTTPException(404, "not found")
    db.add(
        AuditLog(
            org_id=current.tenant_id,
            event_type="worker_task_session_issued",
            actor_id=body.user_id,
            details={
                "invocation_id": body.invocation_id,
                "grant_id": current.grant_id,
                "source_role_arn": os.environ.get("AGENT_TASK_SOURCE_ROLE_ARN"),
                "expires_at": result["Expiration"],
            },
        )
    )
    # Persist attribution before delivery; never record the access key or token.
    await db.commit()
    await verify_broker_worker(request)
    current = request.state.agent_broker_grant
    if current.expires_at is None or not current.is_live(datetime.now(UTC)) or expiry > current.expires_at:
        raise HTTPException(404, "not found")
    response.headers["Cache-Control"] = "no-store"
    return result
