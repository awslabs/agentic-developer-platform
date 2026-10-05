"""Current ADP run identity and durable lease, verified through Gateway IAM transport."""

import asyncio
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import botocore.auth
import botocore.awsrequest
import botocore.session
import httpx
from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.identity import (
    OperationRefused,
    ResolvedPrincipal,
    decode_payload,
    admitted_credential_reference,
    admitted_credential_target,
    payload_digest,
)
from harness_jobs.leases import ExecutionLease
from harness_jobs.recovery_grant import RecoveryGrant


def read_token(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("not a file")
            value = source.read(8194).decode("ascii").rstrip("\r\n")
        if not 1 <= len(value) <= 8192 or any(
            ord(c) < 33 or ord(c) > 126 for c in value
        ):
            raise ValueError("invalid token")
        return value
    except (OSError, ValueError, UnicodeError):
        raise OperationRefused("execution credential unavailable") from None


@dataclass(frozen=True)
class VerifiedOperation:
    grant: ExecutionGrant | RecoveryGrant
    job_id: str
    plan_digest: str
    request_payload: str
    reservation_state: str
    max_resource_units: int
    max_runtime_seconds: int
    max_cost_micros: int

    @property
    def request(self):
        request = decode_payload(self.request_payload)
        if payload_digest(request) != self.plan_digest:
            raise OperationRefused("admitted operation digest mismatch")
        return request


class GatewayAuthority:
    def __init__(
        self,
        *,
        endpoint,
        region,
        run_credential_file,
        workload_token_file,
        session=None,
        client=None,
    ):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "execution authority requires an explicit HTTPS Gateway endpoint"
            )
        match = re.fullmatch(
            r"[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?",
            parsed.hostname,
        )
        self.region = region or (match.group(1) if match else "")
        if not self.region:
            raise ValueError("execution authority requires a signing region")
        self.endpoint = endpoint.rstrip("/")
        self.run_file = Path(run_credential_file)
        self.workload_file = Path(workload_token_file)
        self.session = session or botocore.session.get_session()
        self.client = client or httpx.AsyncClient(
            timeout=10, follow_redirects=False, trust_env=False
        )

    def _headers(self, url, data, *, bootstrap=False):
        credentials = self.session.get_credentials()
        if credentials is None:
            raise OperationRefused("executor IAM identity unavailable")
        headers = {
            "Content-Type": "application/json",
            "X-Adp-Workload-Token": read_token(self.workload_file),
        }
        if not bootstrap:
            headers["X-Adp-Run-Credential"] = read_token(self.run_file)
        request = botocore.awsrequest.AWSRequest(
            method="POST", url=url, data=data, headers=headers
        )
        botocore.auth.SigV4Auth(
            credentials.get_frozen_credentials(), "execute-api", self.region
        ).add_auth(request)
        return dict(request.headers)

    async def post(self, path, payload, *, bootstrap=False):
        if bootstrap and path not in {
            "/internal/v1/controller-execution/task/acquire",
            "/internal/v1/controller-execution/bootstrap",
        }:
            raise OperationRefused("unbound execution transport refused")
        url = self.endpoint + path
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers = await asyncio.to_thread(self._headers, url, data, bootstrap=bootstrap)
        try:
            response = await self.client.post(url, content=data, headers=headers)
            if response.status_code != 200 or len(response.content) > 262144:
                raise ValueError("authority refused")
            return response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            raise OperationRefused(
                "execution authority unavailable or refused"
            ) from None

    async def resolve(self, operation_id) -> VerifiedOperation:
        data = await self.post(
            "/internal/v1/controller-execution/authority",
            {"operation_id": operation_id},
        )
        return self._verified_operation(data, operation_id)

    def _verified_operation(self, data, operation_id, *, recovery_principal=None):
        try:
            if data["version"] != 1 or data["operation_id"] != operation_id:
                raise ValueError("version/identity mismatch")
            fields = {
                key: data[key]
                for key in (
                    "operation_id",
                    "org_id",
                    "workspace_id",
                    "holder",
                    "attempt_id",
                    "fence_token",
                    "attempts",
                    "max_attempts",
                )
            }
            for name in ("expires_at", "acquired_at", "runtime_deadline"):
                fields[name] = datetime.fromisoformat(data[name])
            lease = ExecutionLease(**fields)
            grant = (
                RecoveryGrant(recovery_principal, lease)
                if recovery_principal is not None
                else ExecutionGrant(
                    ResolvedPrincipal(
                        lease.org_id,
                        lease.workspace_id,
                        lease.holder,
                        frozenset({"workspace:provision"}),
                    ),
                    lease,
                )
            )
            operation = VerifiedOperation(
                grant,
                data["job_id"],
                data["plan_digest"],
                data["request_payload"],
                data["reservation_state"],
                data["max_resource_units"],
                data["max_runtime_seconds"],
                data["max_cost_micros"],
            )
            operation.request  # Verify the canonical admitted payload before returning it.
            return operation
        except (ValueError, KeyError, TypeError):
            raise OperationRefused("execution authority contract mismatch") from None

    def credential_request(self, operation):
        credential, service, label = admitted_credential_reference(
            operation.request_payload, operation.plan_digest
        )
        provider, account = admitted_credential_target(
            operation.request_payload, operation.plan_digest
        )
        lease = operation.grant.lease
        return {
            "operation_id": lease.operation_id,
            "attempt_id": lease.attempt_id,
            "job_id": operation.job_id,
            "org_id": lease.org_id,
            "workspace_id": lease.workspace_id,
            "credential_id": credential,
            "service": service,
            "label": label,
            "recipient": lease.holder,
            "provider": provider,
            "provider_account_id": account,
        }

    async def preflight(self, operation):
        result = await self.post(
            "/internal/v1/credential-delivery/preflight",
            self.credential_request(operation),
        )
        if result.get("admits_work") is not True:
            raise OperationRefused(
                "approved provider credential revoked or unavailable"
            )

    async def delivery_role(self, operation):
        request = self.credential_request(operation)
        result = await self.post("/internal/v1/credential-delivery", request)
        try:
            if (
                result["credential_type"] != "aws_role"
                or result["credential_id"] != request["credential_id"]
            ):
                raise ValueError("unsupported provider credential")
            value = json.loads(result["value"])
            if set(value) - {"role_arn", "external_id"} or not re.fullmatch(
                r"arn:aws:iam::"
                + re.escape(request["provider_account_id"])
                + r":role/[A-Za-z0-9+=,.@_/-]+",
                value["role_arn"],
            ):
                raise ValueError("approved provider role mismatch")
            if value.get("external_id") is not None and not isinstance(
                value["external_id"], str
            ):
                raise ValueError("invalid provider credential")
            return value
        except (KeyError, ValueError, TypeError):
            raise OperationRefused("approved AWS role credential unavailable") from None

    async def aclose(self):
        await self.client.aclose()
