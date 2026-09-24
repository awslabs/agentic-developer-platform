"""GitHub-free task workload binding and current-attempt authorization."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.run_credential import mint_credential, verify_credential
from src.tasks.records import task_authority_partition, task_run_grant_sort_key


@dataclass(frozen=True)
class VerifiedTaskAttempt:
    task_id: str
    invocation_id: str
    generation: int
    runtime_attempt_id: str | None
    tenant: str
    canonical_principal: str
    pod_uid: str


class TaskRuntime:
    def __init__(self, repository, *, env=None, clock=None):
        self.repository = repository
        self.env = env
        self.clock = clock or (lambda: datetime.now(UTC))

    def _grant(self, tenant, invocation, generation):
        grant = self.repository._get_authority(task_authority_partition(tenant), task_run_grant_sort_key(
            invocation_id=invocation, generation=generation))
        if grant is None or grant.get("status") != "active":
            raise BootstrapRefusedError("task authority unavailable")
        return grant

    def _current(self, task_id):
        task = self.repository.read_task(task_id)
        if task is None:
            raise BootstrapRefusedError("task authority unavailable")
        self.repository.resolve_work(task["dispatch_id"], expected_kind="dispatch")
        deadline = datetime.fromisoformat(task["deadline_at"].replace("Z", "+00:00"))
        if self.clock() >= deadline:
            raise BootstrapRefusedError("task authority expired")
        return task

    def bootstrap(self, *, body, pod, delivery):
        # Workload identity comes from TokenReview; the body only corroborates it.
        workload = body["workload"]
        if workload["pod_uid"] != pod.uid or workload["namespace"] != pod.namespace:
            raise BootstrapRefusedError("workload mismatch")
        delivery.require_assignment(pod.uid, body["invocation_id"], body["envelope_digest"])
        assignment = delivery.read(pod.uid)
        import json
        envelope = json.loads(assignment["body"])
        if envelope.get("kind") != "adp.task" or envelope.get("task_id") != body["task_id"]:
            raise BootstrapRefusedError("task assignment mismatch")
        task = self._current(envelope["task_id"])
        work = self.repository.resolve_work(task["dispatch_id"], expected_kind="dispatch")
        if envelope != work["envelope"] or envelope_digest(envelope) != body["envelope_digest"]:
            raise BootstrapRefusedError("task envelope mismatch")
        tenant = task["scope"]["tenant"]
        grant = self._grant(tenant, task["invocation_id"], int(task["generation"]))
        key = {"pk": {"S": task_authority_partition(tenant)}, "sk": {"S": task_run_grant_sort_key(
            invocation_id=task["invocation_id"], generation=int(task["generation"]))}}
        # No automatic takeover: a second pod needs authoritative termination
        # evidence and a new generation before it can obtain this run's authority.
        try:
            self.repository._client.update_item(
                TableName=self.repository.authority_table_name, Key=key,
                UpdateExpression=("SET workload_uid = :pod, workload_namespace = :namespace, "
                                  "credential_epoch = if_not_exists(credential_epoch, :epoch)"),
                ConditionExpression="#status = :active AND task_id = :task AND (attribute_not_exists(workload_uid) OR workload_uid = :pod)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":pod": {"S": pod.uid}, ":namespace": {"S": pod.namespace}, ":epoch": {"N": "1"},
                                           ":active": {"S": "active"}, ":task": {"S": task["task_id"]}},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise BootstrapRefusedError("task already owned") from None
            raise
        self._current(task["task_id"])
        credential = mint_credential(invocation_id=task["invocation_id"], attempt=int(task["generation"]),
            tenant_id=tenant, credential_epoch=int(grant.get("credential_epoch", 1)), persona=task["persona"],
            ttl_seconds=min(900, int((datetime.fromisoformat(task["deadline_at"].replace("Z", "+00:00")) - self.clock()).total_seconds())),
            now=self.clock(), env=self.env)
        claims = verify_credential(credential, now=self.clock(), env=self.env)
        return {"schema_version": "1.0", "task_id": task["task_id"], "invocation_id": task["invocation_id"],
                "generation": int(task["generation"]), "persona": task["persona"], "run_credential": credential,
                "run_credential_expires_at": claims.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "deadline_at": task["deadline_at"], "input": grant["input"], "model_binding": grant["model_binding"],
                "limits": grant["limits"], "capabilities": grant["capabilities"]}

    def authenticate(self, *, credential, pod, require_attempt=True):
        claims = verify_credential(credential, now=self.clock(), env=self.env)
        grant = self._grant(claims.tenant_id, claims.invocation_id, claims.attempt)
        task = self._current(grant["task_id"])
        if (grant.get("workload_uid") != pod.uid or grant.get("workload_namespace") != pod.namespace
                or int(grant.get("credential_epoch", 0)) != claims.credential_epoch
                or task["invocation_id"] != claims.invocation_id or int(task["generation"]) != claims.attempt):
            raise BootstrapRefusedError("task workload mismatch")
        attempt = task.get("runtime_attempt_id")
        if (require_attempt and not attempt) or grant.get("runtime_attempt_id") != attempt:
            raise BootstrapRefusedError("task attempt mismatch")
        return VerifiedTaskAttempt(task["task_id"], claims.invocation_id, claims.attempt, attempt,
                                   claims.tenant_id, task["scope"]["canonical_principal"], pod.uid)

    def register_attempt(self, *, identity, body):
        if (body["task_id"], body["invocation_id"], body["generation"]) != (
                identity.task_id, identity.invocation_id, identity.generation):
            raise BootstrapRefusedError("task attempt mismatch")
        task = self._current(identity.task_id)
        if task.get("runtime_attempt_id") == body["runtime_attempt_id"]:
            return  # Response-loss retry of the same committed binding.
        self.repository.bind_runtime_attempt(task_id=identity.task_id, invocation_id=identity.invocation_id,
            generation=identity.generation, runtime_attempt_id=body["runtime_attempt_id"],
            expected_version=int(task["version"]), expected_runtime_attempt_id=identity.runtime_attempt_id)
