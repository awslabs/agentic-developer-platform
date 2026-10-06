"""Deliver admitted domain outbox rows to the protected ADP operation producer."""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

import botocore.auth
import botocore.awsrequest
import botocore.session
import httpx
from harness_jobs.identity import decode_payload, payload_digest
from harness_jobs.outbox import DispatchOutbox

from app.operation_activation import dispatch_enabled

logger = logging.getLogger(__name__)
PREFIX = "/internal/v1/controller-execution"

# A workload operation has its own immutable registration. Workspace lifecycle
# pointers remain reserved for workspace bootstrap/retirement.
_WORKLOAD_REGISTERED = """EXISTS (
 SELECT 1 FROM controller_deployment_operations cd
 JOIN deployments dep ON dep.id::text=cd.deployment_id
  AND dep.org_id::text=cd.org_id AND dep.workspace_id::text=cd.workspace_id
 WHERE cd.operation_id=o.operation_id AND cd.org_id=o.org_id
  AND cd.workspace_id=o.workspace_id AND cd.action=o.action
)"""
_CONTROL_REGISTERED = """EXISTS (
 SELECT 1 FROM workspace_lifecycle_control_operations control
 WHERE control.operation_id=o.operation_id AND control.org_id=o.org_id
  AND control.workspace_id=o.workspace_id AND control.plan_digest=o.plan_digest
  AND control.source_bootstrap_operation_id=w.provisioning_operation_id
  AND control.phase='prepare-retirement-access'
  AND w.status IN ('Active','active') AND w.is_default=false
)"""
_REGISTERED = (
    "((o.action='provision' AND w.provisioning_operation_id=o.operation_id) OR "
    "(o.action='teardown' AND w.teardown_operation_id=o.operation_id) OR "
    + _WORKLOAD_REGISTERED
    + " OR "
    + _CONTROL_REGISTERED
    + ")"
)

# Historical paid identity is sufficient for observation. Execution additionally
# requires a current confirmed reservation; Gateway rechecks live human approval.
# The same query selects candidates and rereads each candidate just before dispatch.
_RECOVERABLE = (
    """
SELECT o.*, d.adp_org_id,
       CASE WHEN l.holder IS NULL THEN 'execution' ELSE 'recovery' END AS mode
FROM harness_operations o
JOIN workspaces w ON w.id::text=o.workspace_id AND w.org_id::text=o.org_id
JOIN organizations d ON d.id=w.org_id
JOIN harness_approval_consumption a ON a.operation_id=o.operation_id
 AND a.org_id=o.org_id AND a.workspace_id=o.workspace_id AND a.plan_digest=o.plan_digest
JOIN operation_budget_reservations r ON r.reservation_id=a.reservation_id
 AND r.job_id=o.job_id AND r.attempt_id=o.attempt_id
 AND r.org_id=o.org_id AND r.workspace_id=o.workspace_id
 AND r.max_resource_units=a.max_resource_units
 AND r.max_runtime_seconds=a.max_runtime_seconds AND r.max_cost_micros=a.max_cost_micros
JOIN harness_dispatch_outbox b ON b.operation_id=o.operation_id
LEFT JOIN harness_operation_leases l ON l.operation_id=o.operation_id
WHERE """
    + _REGISTERED
    + """
 AND a.reservation_state IN ('confirmed','retained','released')
 AND r.state IN ('confirmed','retained','released')
 AND l.closed_at IS NULL
 AND ((l.holder IS NOT NULL AND (l.expires_at<=clock_timestamp()
                               OR l.runtime_deadline<=clock_timestamp()))
      OR (l.holder IS NULL AND b.delivered_at IS NOT NULL
          AND b.abandoned_at IS NULL AND o.state IN ('pending','running')
          AND o.cancel_requested_at IS NULL AND NOT o.cleanup_required
          AND a.reservation_state='confirmed' AND r.state='confirmed'))
"""
)


class ProducerTransport:
    """API workload IAM identity only; execution tokens are never producer inputs."""

    def __init__(self, endpoint, region, *, session=None, client=None):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("operation producer requires an explicit HTTPS endpoint")
        match = re.fullmatch(
            r"[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?",
            parsed.hostname,
        )
        self.region = region or (match.group(1) if match else "")
        if not self.region:
            raise ValueError("operation producer requires an explicit signing region")
        self.endpoint = endpoint.rstrip("/")
        self.session = session or botocore.session.get_session()
        self.client = client or httpx.AsyncClient(
            timeout=10, follow_redirects=False, trust_env=False
        )

    def _headers(self, url, encoded):
        credentials = self.session.get_credentials()
        if credentials is None:
            raise RuntimeError("operation producer IAM identity unavailable")
        request = botocore.awsrequest.AWSRequest(
            method="POST",
            url=url,
            data=encoded,
            headers={"Content-Type": "application/json"},
        )
        botocore.auth.SigV4Auth(
            credentials.get_frozen_credentials(), "execute-api", self.region
        ).add_auth(request)
        return dict(request.headers)

    async def post(self, route, payload):
        url = self.endpoint + PREFIX + route
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        headers = await asyncio.to_thread(self._headers, url, encoded)
        response = await self.client.post(url, content=encoded, headers=headers)
        if response.status_code != 200 or len(response.content) > 65536:
            raise RuntimeError("operation producer refused or unavailable")
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("operation producer response is invalid")
        return value

    async def aclose(self):
        await self.client.aclose()


class OperationDispatcher:
    def __init__(
        self, connect, transport, *, policy_for=None, interval=5, enabled=True
    ):
        self.connect, self.transport, self.policy_for = connect, transport, policy_for
        self.outbox = DispatchOutbox()
        if type(enabled) is not bool:
            raise ValueError("operation dispatch enabled must be a boolean")
        self.enabled = enabled
        self.interval = interval
        self._task = None
        self._recovery_cursor = ""

    async def _policy(self, org_id):
        if self.policy_for is not None:
            return self.policy_for(org_id)
        # Organization transport identity is canonical installed state, not a
        # workspace lifecycle profile. Workload-only installations use it too.
        async with self.connect() as connection:
            adp_org_id = await connection.fetchval(
                "SELECT adp_org_id FROM organizations WHERE id::text=$1", org_id
            )
        if not adp_org_id:
            raise RuntimeError("operation organization binding unavailable")
        return SimpleNamespace(adp_org_id=adp_org_id)

    async def _registration(self, row):
        request = decode_payload(row["request_payload"])
        if request.parameters.get("lifecycle_phase") == "prepare-retirement-access":
            from workspace_provisioning.control_registry import registration_values

            async with self.connect() as connection:
                registered = await connection.fetchrow(
                    "SELECT * FROM workspace_lifecycle_control_operations "
                    "WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3",
                    row["operation_id"],
                    row["org_id"],
                    row["workspace_id"],
                )
                if registered is None:
                    return False
                values = await registration_values(
                    connection,
                    operation_id=row["operation_id"],
                    org_id=row["org_id"],
                    workspace_id=row["workspace_id"],
                    source_bootstrap_operation_id=registered[
                        "source_bootstrap_operation_id"
                    ],
                    request_id=registered["request_id"],
                )
            return all(registered[key] == value for key, value in values.items())
        deployment_id = request.parameters.get("controller_deployment_id")
        if deployment_id is None:
            return True
        from superplane_executor.deployment_registry import registration_values

        async with self.connect() as connection:
            values = await registration_values(
                connection,
                operation_id=row["operation_id"],
                org_id=row["org_id"],
                workspace_id=row["workspace_id"],
                deployment_id=deployment_id,
            )
            registered = await connection.fetchrow(
                "SELECT * FROM controller_deployment_operations WHERE operation_id=$1",
                row["operation_id"],
            )
        return registered is not None and all(
            registered[key] == value for key, value in values.items()
        )

    async def verify_run(self, **identity):
        policy = await self._policy(identity["org_id"])
        data = await self.transport.post(
            "/verify-run", {"domain": "superplane", **identity}
        )
        if data.get("adp_org_id") != policy.adp_org_id:
            return None
        return data

    async def ready(self, org_id):
        try:
            policy = await self._policy(org_id)
            data = await self.transport.post(
                "/producer-readiness", {"domain": "superplane", "org_id": org_id}
            )
            return (
                data.get("version") == 1
                and data.get("ready") is True
                and data.get("domain") == "superplane"
                and data.get("org_id") == org_id
                and data.get("domain_org_id") == org_id
                and data.get("adp_org_id") == policy.adp_org_id
            )
        except Exception:
            return False

    async def binding_ready(self, org_id, expected):
        """A fresh installed-worker read is required before each lifecycle admission."""
        from datetime import UTC, datetime, timedelta

        if not self.enabled or not dispatch_enabled():
            return False
        try:
            policy = await self._policy(org_id)
            proof = await self.transport.post(
                "/binding-proof", {"domain": "superplane", "org_id": org_id}
            )
            checked_at = datetime.fromisoformat(proof["checked_at"])
            now = datetime.now(UTC)
            return (
                proof.get("version") == 1
                and proof.get("installed") is True
                and proof.get("domain") == "superplane"
                and proof.get("org_id") == org_id
                and proof.get("adp_org_id") == policy.adp_org_id
                and checked_at.tzinfo is not None
                and now - timedelta(seconds=60) <= checked_at <= now
                and all(proof.get(key) == value for key, value in expected.items())
            )
        except Exception:
            return False

    async def _lifecycle_ready(self, row):
        parameters = decode_payload(row["request_payload"]).parameters
        if (
            "runtime_config_sha256" in parameters
            and "lifecycle_phase" not in parameters
        ):
            return False
        if "lifecycle_phase" not in parameters:
            return True
        from app.operation_activation import (
            expected_lifecycle_binding,
            require_admission_enabled,
        )
        from app.services.provisioning import ProvisioningUnavailable

        try:
            require_admission_enabled(lifecycle=True)
            expected = expected_lifecycle_binding()
        except ProvisioningUnavailable:
            return False
        return await self.binding_ready(row["org_id"], expected)

    async def deliver(self, envelope):
        if not self.enabled or not dispatch_enabled():
            return False
        # Re-read the durable registration after claim. Creation admission commits
        # independently; a lost API transaction must not dispatch an orphan request.
        async with self.connect() as connection:
            row = await connection.fetchrow(
                "SELECT o.*,d.adp_org_id FROM harness_operations o "
                "JOIN workspaces w ON w.id::text=o.workspace_id AND w.org_id::text=o.org_id "
                "JOIN organizations d ON d.id=w.org_id "
                "WHERE o.operation_id=$1 AND " + _REGISTERED,
                envelope.operation_id,
            )
        if row is None or row["state"] not in {"pending", "running"}:
            return False
        if (
            any(
                row[name] != getattr(envelope, name)
                for name in (
                    "operation_id",
                    "job_id",
                    "attempt_id",
                    "org_id",
                    "workspace_id",
                    "action",
                    "request_payload",
                )
            )
            or payload_digest(decode_payload(row["request_payload"]))
            != row["plan_digest"]
        ):
            return False
        if not await self._registration(row) or not await self._lifecycle_ready(row):
            return False
        policy = await self._policy(envelope.org_id)
        if policy.adp_org_id != row["adp_org_id"]:
            return False
        request = {
            "domain": "superplane",
            "mode": "execution",
            **{
                key: getattr(envelope, key)
                for key in (
                    "operation_id",
                    "job_id",
                    "attempt_id",
                    "org_id",
                    "workspace_id",
                )
            },
        }
        return await self._dispatch(request, policy)

    async def _dispatch(self, request, policy):
        if not self.enabled or not dispatch_enabled():
            return False
        data = await self.transport.post("/dispatch", request)
        try:
            deadline = datetime.fromisoformat(data["not_after"])
            return (
                all(data.get(key) == value for key, value in request.items())
                and data.get("version") == 1
                and data.get("domain_org_id") == request["org_id"]
                and data.get("adp_org_id") == policy.adp_org_id
                and data.get("status") == "pending"
                and isinstance(data.get("invocation_id"), str)
                and bool(data["invocation_id"])
                and data.get("principal") == data["invocation_id"] + "#1"
                and deadline.tzinfo is not None
                and deadline > datetime.now(UTC)
            )
        except (KeyError, ValueError, TypeError):
            return False

    async def recover_once(self, *, limit=10):
        """Resume paid tasks from durable leases without changing admission IDs.

        There is no local completion flag: a lost reply or process restart must
        resend until the shared lease advances. Gateway deduplicates the exact
        generation before queue publication. The cursor bounds each pass and keeps
        one persistently unavailable operation from starving later candidates.
        """
        if not self.enabled or not dispatch_enabled():
            return ()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("recovery batch must contain between 1 and 100 tasks")
        async with self.connect() as connection:
            rows = await connection.fetch(
                _RECOVERABLE
                + " AND o.operation_id>$1 ORDER BY o.operation_id LIMIT $2",
                self._recovery_cursor,
                limit,
            )
        self._recovery_cursor = rows[-1]["operation_id"] if len(rows) == limit else ""
        delivered = []
        for candidate in rows:
            try:
                # No DB lock spans the HTTP call. Gateway independently locks and
                # verifies current lease eligibility before any task is published.
                async with self.connect() as connection:
                    row = await connection.fetchrow(
                        _RECOVERABLE + " AND o.operation_id=$1",
                        candidate["operation_id"],
                    )
                if row is None or (
                    payload_digest(decode_payload(row["request_payload"]))
                    != row["plan_digest"]
                ):
                    continue
                if not await self._registration(row) or not await self._lifecycle_ready(
                    row
                ):
                    continue
                policy = await self._policy(row["org_id"])
                if policy.adp_org_id != row["adp_org_id"]:
                    continue
                request = {
                    "domain": "superplane",
                    "mode": row["mode"],
                    **{
                        key: row[key]
                        for key in (
                            "operation_id",
                            "job_id",
                            "attempt_id",
                            "org_id",
                            "workspace_id",
                        )
                    },
                }
                if await self._dispatch(request, policy):
                    delivered.append(row["operation_id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Operation recovery dispatch deferred", exc_info=False)
        return tuple(delivered)

    async def drain_once(self):
        if not self.enabled or not dispatch_enabled():
            return ()
        async with self.connect() as connection:
            rows = await connection.fetch(
                "SELECT o.operation_id FROM harness_dispatch_outbox o "
                "JOIN workspaces w ON w.id::text=o.workspace_id AND w.org_id::text=o.org_id "
                "WHERE o.delivered_at IS NULL AND o.abandoned_at IS NULL "
                "AND (o.claimed_until IS NULL OR o.claimed_until<now()) "
                "AND " + _REGISTERED + " ORDER BY o.id LIMIT 100",
            )
            return await self.outbox.drain_once(
                connection,
                self,
                limit=10,
                operation_ids=tuple(row["operation_id"] for row in rows),
            )

    def start(self):
        if not self.enabled or not dispatch_enabled():
            return
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="superplane-operation-outbox"
            )

    async def _run(self):
        while True:
            for deliver in (self.drain_once, self.recover_once):
                try:
                    await deliver()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A transport error can contain authorization or connection data.
                    logger.warning("Operation task delivery deferred", exc_info=False)
            await asyncio.sleep(self.interval)

    async def aclose(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.transport.aclose()
