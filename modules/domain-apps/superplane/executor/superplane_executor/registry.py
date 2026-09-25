"""Publish revocable assignments for one pod, never manufacture execution authority."""

import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from harness_jobs.execution_rpc import admitted_steps
from harness_jobs.identity import OperationRefused
from harness_jobs.store import OperationStore

from .authority import read_token
from .handoff import bound_operation, live_handoff


class AssignmentRegistry:
    def __init__(
        self,
        *,
        domain_pool,
        execution_pool,
        authority,
        instance_file,
        token_dir,
        submitter_id,
        validate_plan,
        handoffs=None,
        handoff_reader=None,
    ):
        # Grants delivered for this run, by operation. Empty means this pod holds no
        # per-operation authority, so every verification refuses and the pod idles.
        self.handoffs = dict(handoffs or {})
        self.handoff_reader = handoff_reader
        self.domain_pool = domain_pool
        self.execution_pool = execution_pool
        self.authority = authority
        self.instance_file = Path(instance_file)
        self.token_dir = Path(token_dir)
        self.submitter_id = submitter_id
        self.validate_plan = validate_plan
        self.tokens = {}
        self.observations = {}

    def handoff(self, operation_id):
        # Production reads the producer-owned file at every authority boundary.
        # The in-memory carrier remains an explicit test/composition port only.
        grants = self.handoff_reader() if self.handoff_reader else self.handoffs
        return live_handoff(grants, operation_id)

    def check_handoff(self, operation, expected):
        current = self.handoff(operation.grant.lease.operation_id)
        if current != expected:
            raise OperationRefused("run handoff replaced during authorization")
        bound_operation(current, operation)
        return current

    def controller_holder(self):
        instance = read_token(self.instance_file)
        if str(UUID(instance)) != instance:
            raise OperationRefused("controller instance unavailable")
        return f"{self.submitter_id}:{instance}"

    async def target(self, operation, holder):
        lease = operation.grant.lease
        # Shared admission retains the domain UUID; ADP run tenancy is mapped by Gateway.
        # Never reinterpret the original admitted tenant as a Gateway run tenant.
        async with self.domain_pool.acquire() as connection:
            from .deployment_registry import require_deployment_registration

            await require_deployment_registration(connection, operation)
            row = await connection.fetchrow(
                """
                SELECT w.id::text AS workspace_id, w.org_id::text AS domain_org_id,
                       w.namespace_name AS namespace, c.id::text AS cluster_id,
                       c.eks_cluster_arn AS cluster_arn, c.endpoint,
                       l.expires_at AS controller_expires_at,
                       w.shared_cluster_id::text AS shared_cluster_id
                  FROM workspaces w
                  JOIN organizations o ON o.id=w.org_id
                  JOIN clusters c ON c.id=w.cluster_id AND c.org_id=w.org_id
                  JOIN observation_leases l ON l.scope='controller_management/' || o.id::text
                 WHERE w.id::text=$1 AND o.id::text=$2
                   AND (w.status IN ('Ready','active') OR ($4='teardown' AND w.status IN ('Teardown','retired')))
                   AND c.status IN ('Ready','Active')
                   AND l.holder=$3 AND l.expires_at > clock_timestamp()
                """,
                lease.workspace_id,
                lease.org_id,
                holder,
                operation.request.action,
            )
            if row is not None:
                row = dict(row)
                shared = row.pop("shared_cluster_id")
                if shared is not None:
                    from .member_target import credential_target

                    if shared != row["cluster_id"]:
                        raise OperationRefused(
                            "shared workspace cluster binding changed"
                        )
                    metadata, eligible = await credential_target(
                        connection,
                        workspace_id=lease.workspace_id,
                        org_id=lease.org_id,
                        scope="mutator",
                    )
                    row["membership_credential"] = metadata
                    row["platform_eligible"] = eligible
        if row is None:
            raise OperationRefused(
                "workspace registration or controller ownership lost"
            )
        return dict(row)

    async def verify(self, operation_id, expected=None, *, require_active=False):
        if require_active:
            entries = [
                (name, value)
                for name, value in self.tokens.items()
                if value[1]["operation_id"] == operation_id
            ]
            if len(entries) != 1:
                raise OperationRefused("execution assignment withdrawn")
            name, (issued, bound, expires) = entries[0]
            if expires <= datetime.now(UTC) or not hmac.compare_digest(
                read_token(self.token_dir / name), issued
            ):
                raise OperationRefused("execution assignment revoked")
            expected = bound
        # A live per-operation grant is required before Gateway is asked anything.
        # Without it this pod has only a static projection, which is not admission.
        handoff = self.handoff(operation_id)
        operation = await self.authority.resolve(operation_id)
        # Gateway resolved the attempt/job independently; the grant must name the
        # same ones. Neither value is ever derived from the operation ID.
        bound_operation(handoff, operation)
        if expected is not None:
            actual = operation.grant.lease
            # Renewal may extend time; it may never exchange tenant/attempt/fence.
            if (
                any(
                    getattr(actual, field) != expected[field]
                    for field in (
                        "operation_id",
                        "org_id",
                        "workspace_id",
                        "holder",
                        "attempt_id",
                        "fence_token",
                    )
                )
                or operation.plan_digest != expected["plan_digest"]
                or operation.job_id != expected["job_id"]
            ):
                raise OperationRefused("execution binding changed")
        holder = self.controller_holder()
        if expected is not None and holder != expected["controller_holder"]:
            raise OperationRefused("controller replica changed")
        target = await self.target(operation, holder)
        self.observations[operation_id] = await self.validate_plan(operation, target)
        await self.authority.preflight(operation)
        # The grant read above is now stale. `resolve`, `target`, `validate_plan` and
        # `preflight` each await I/O, and a grant can expire or be withdrawn while they
        # run -- so the earlier read establishes only that authority existed *then*.
        # Re-read from the producer-owned source at the boundary where authority is
        # actually conferred, and require the same grant: a replacement naming a
        # different attempt/job is a different authorization, not a renewal of this one.
        current = self.check_handoff(operation, handoff)
        return operation, target, holder, current

    async def publish(self, operation_id):
        operation, target, holder, handoff = await self.verify(operation_id)
        lease = operation.grant.lease
        async with self.execution_pool.acquire() as connection:
            record = await OperationStore().get(
                connection, operation.grant.principal, operation_id
            )
        if record is None or record.plan_digest != operation.plan_digest:
            raise OperationRefused("admitted operation unavailable")
        self.check_handoff(operation, handoff)
        steps = admitted_steps(record)
        allocation = operation.request.parameters.get("allocation_id")
        if not allocation:
            raise OperationRefused("approved allocation required")
        binding = {
            name: getattr(lease, name)
            for name in (
                "operation_id",
                "org_id",
                "workspace_id",
                "holder",
                "attempt_id",
                "fence_token",
            )
        }
        binding.update(
            job_id=operation.job_id,
            plan_digest=operation.plan_digest,
            controller_holder=holder,
        )
        name = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        previous = self.tokens.get(name)
        token = previous[0] if previous else secrets.token_urlsafe(48)
        assignment = {
            key: value for key, value in binding.items() if key != "controller_holder"
        }
        assignment.update(
            action=operation.request.action,
            allocation_id=allocation,
            credential_name=name,
            step_ids=[step.step_id for step in steps],
            provider_observation=self.observations.get(operation_id),
        )
        # Commit metadata only after a private token is durably written. A crash
        # between these writes can leave an unused token, never an authority grant.
        self.token_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        temporary = self.token_dir / ("." + secrets.token_hex(16))
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
            with os.fdopen(fd, "w") as output:
                output.write(token)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.token_dir / name)
        finally:
            temporary.unlink(missing_ok=True)
        expires_at = min(
            lease.expires_at,
            lease.runtime_deadline,
            target["controller_expires_at"],
            # An assignment may not outlive the grant that justified it. Without this
            # bound a 30-second credential issued just before the grant lapsed would
            # stay usable after the producer's authority had ended.
            handoff.not_after,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        async with self.domain_pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO controller_executions
                    (operation_id, org_id, workspace_id, controller_holder, assignment, expires_at)
                VALUES ($1,$2::text::uuid,$3::text::uuid,$4,$5::json,$6)
                ON CONFLICT (operation_id) DO UPDATE SET
                    org_id=EXCLUDED.org_id, workspace_id=EXCLUDED.workspace_id,
                    controller_holder=EXCLUDED.controller_holder,
                    assignment=EXCLUDED.assignment, expires_at=EXCLUDED.expires_at
                """,
                operation_id,
                target["domain_org_id"],
                lease.workspace_id,
                holder,
                json.dumps(assignment),
                expires_at,
            )
        self.check_handoff(operation, handoff)
        self.tokens[name] = (token, binding, expires_at)
        return name

    async def authenticate(self, token):
        # Never decode caller claims from the token. It selects one locally issued
        # binding, which is reverified through ADP and both databases on every RPC.
        for name, (issued, binding, expires) in tuple(self.tokens.items()):
            if hmac.compare_digest(token, issued):
                if datetime.now(UTC) >= expires or not hmac.compare_digest(
                    read_token(self.token_dir / name), issued
                ):
                    raise OperationRefused("execution credential expired or revoked")
                operation, _, _, _ = await self.verify(binding["operation_id"], binding)
                return operation.grant
        raise OperationRefused("execution credential refused")

    def revoke_except(self, active):
        for name in set(self.tokens) - set(active):
            del self.tokens[name]
            (self.token_dir / name).unlink(missing_ok=True)

    async def refresh(self, operation_ids):
        active = set()
        try:
            for operation_id in operation_ids:
                try:
                    active.add(await self.publish(operation_id))
                except Exception:
                    # Provider/run/database errors may contain secrets. Revocation
                    # is the only consequence; no raw exception crosses this boundary.
                    continue
        finally:
            self.revoke_except(active)
