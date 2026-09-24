"""Pod-local paid execution; canonical registration remains with the manager."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

from harness_jobs.execution_plan import admitted_steps
from harness_jobs.identity import OperationRefused
from harness_jobs.store import OperationStore

from .registry import AssignmentRegistry


class TaskRegistry(AssignmentRegistry):
    def __init__(self, *, original, write_private, assignment_file, **kwargs):
        super().__init__(**kwargs)
        self.original = original
        self.write_private = write_private
        self.assignment_file = assignment_file

    def controller_holder(self):
        # This pod owns execution, never the registration manager's lease.
        return self.original.grant.lease.holder

    async def target(self, operation, holder):
        lease = operation.grant.lease
        if lease != self.original.grant.lease:
            names = (
                "operation_id",
                "org_id",
                "workspace_id",
                "holder",
                "attempt_id",
                "fence_token",
            )
            if any(
                getattr(lease, key) != getattr(self.original.grant.lease, key)
                for key in names
            ):
                raise OperationRefused("paid task lease changed")
        async with self.domain_pool.acquire() as connection:
            from .deployment_registry import require_deployment_registration

            await require_deployment_registration(connection, operation)
            row = await connection.fetchrow(
                "SELECT w.id::text AS workspace_id,w.org_id::text AS domain_org_id,"
                "w.namespace_name AS namespace,c.id::text AS cluster_id,"
                "c.eks_cluster_arn AS cluster_arn,c.endpoint,l.expires_at AS controller_expires_at "
                "FROM workspaces w JOIN clusters c ON c.id=w.cluster_id AND c.org_id=w.org_id "
                "JOIN observation_leases l ON l.scope='controller_management/' || w.org_id::text "
                "WHERE w.id::text=$1 AND w.org_id::text=$2 AND l.expires_at>clock_timestamp() "
                "AND (w.status IN ('Ready','active') OR ($3='teardown' AND w.status IN ('Teardown','retired'))) "
                "AND c.status IN ('Ready','Active')",
                lease.workspace_id,
                lease.org_id,
                operation.request.action,
            )
        if row is None:
            raise OperationRefused("canonical workspace registration unavailable")
        return dict(row)

    async def publish(self, operation_id):
        operation, target, holder, handoff = await self.verify(operation_id)
        lease = operation.grant.lease
        async with self.execution_pool.acquire() as connection:
            record = await OperationStore().get(
                connection, operation.grant.principal, operation_id
            )
        if record is None or record.plan_digest != operation.plan_digest:
            raise OperationRefused("original task admission unavailable")
        self.check_handoff(operation, handoff)
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
        token = (
            self.tokens[name][0] if name in self.tokens else secrets.token_urlsafe(48)
        )
        self.token_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        self.write_private(self.token_dir / name, token, mode=0o640)
        self.check_handoff(operation, handoff)
        self.tokens[name] = (
            token,
            binding,
            min(
                lease.expires_at,
                lease.runtime_deadline,
                handoff.not_after,
                target["controller_expires_at"],
                datetime.now(UTC) + timedelta(seconds=30),
            ),
        )
        assignment = {
            key: value for key, value in binding.items() if key != "controller_holder"
        }
        assignment.update(
            credential_name=name,
            step_ids=[step.step_id for step in admitted_steps(record)],
        )
        self.write_private(self.assignment_file, json.dumps(assignment), mode=0o640)
        return name
