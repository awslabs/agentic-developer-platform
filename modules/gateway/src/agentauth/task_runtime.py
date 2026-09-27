"""GitHub-free task workload binding and current-attempt authorization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError

from src.agentauth.bootstrap import BootstrapRefusedError, envelope_digest
from src.agentauth.run_credential import mint_credential, verify_credential, verify_credential_for_task_settlement
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

    def _grant(self, tenant, invocation, generation, *, stop_only=False):
        grant = self.repository._get_authority(
            task_authority_partition(tenant), task_run_grant_sort_key(invocation_id=invocation, generation=generation)
        )
        if grant is None or (not stop_only and grant.get("status") != "active"):
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
        if task["state"] in {"completed", "cancelled", "failed", "cancel_requested"}:
            raise BootstrapRefusedError("task cannot bootstrap")
        work = self.repository.resolve_work(task["dispatch_id"], expected_kind="dispatch")
        if envelope != work["envelope"] or envelope_digest(envelope) != body["envelope_digest"]:
            raise BootstrapRefusedError("task envelope mismatch")
        tenant = task["scope"]["tenant"]
        grant = self._grant(tenant, task["invocation_id"], int(task["generation"]))
        key = {
            "pk": {"S": task_authority_partition(tenant)},
            "sk": {"S": task_run_grant_sort_key(invocation_id=task["invocation_id"], generation=int(task["generation"]))},
        }
        # Claiming ownership and all three execution slots is one transaction.
        # Repeat bootstrap by the same pod consumes no additional capacity.
        if grant.get("workload_uid") not in (None, pod.uid):
            raise BootstrapRefusedError("task already owned")
        if grant.get("workload_uid") is None:
            scopes = [("pilot", 4), ("tenant:" + tenant, 4), ("principal:" + tenant + ":" + task["scope"]["canonical_principal"], 2)]
            capacity_keys = []
            transactions = []
            for scope, limit in scopes:
                partition = "TASK_CAPACITY#" + hashlib.sha256(("task-execution:" + scope).encode()).hexdigest()
                capacity_keys.append(partition)
                capacity_key = {"pk": {"S": partition}, "sk": {"S": "ACTIVE"}}
                try:
                    self.repository._client.put_item(
                        TableName=self.repository.authority_table_name,
                        Item={**capacity_key, "active_count": {"N": "0"}, "capacity_limit": {"N": str(limit)}, "reservations": {"M": {}}},
                        ConditionExpression="attribute_not_exists(pk)",
                    )
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
                transactions.append(
                    {
                        "Update": {
                            "TableName": self.repository.authority_table_name,
                            "Key": capacity_key,
                            "UpdateExpression": "SET reservations.#task = :invocation ADD active_count :one",
                            "ConditionExpression": "capacity_limit = :limit AND active_count < :limit AND attribute_not_exists(reservations.#task)",
                            "ExpressionAttributeNames": {"#task": task["task_id"]},
                            "ExpressionAttributeValues": {
                                ":invocation": {"S": task["invocation_id"]},
                                ":one": {"N": "1"},
                                ":limit": {"N": str(limit)},
                            },
                        }
                    }
                )
            transactions.append(
                {
                    "Update": {
                        "TableName": self.repository.authority_table_name,
                        "Key": key,
                        "UpdateExpression": (
                            "SET workload_uid = :pod, workload_namespace = :namespace, workload_name = :pod_name, credential_epoch = :epoch, "
                            "execution_capacity_keys = :keys, execution_capacity_released = :false"
                        ),
                        "ConditionExpression": (
                            "#status = :active AND task_id = :task AND attribute_not_exists(workload_uid) "
                            "AND attribute_not_exists(runtime_start_cancelled)"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":pod": {"S": pod.uid},
                            ":namespace": {"S": pod.namespace},
                            ":pod_name": {"S": getattr(pod, "name", "")},
                            ":epoch": {"N": "1"},
                            ":active": {"S": "active"},
                            ":task": {"S": task["task_id"]},
                            ":false": {"BOOL": False},
                            ":keys": {"L": [{"S": value} for value in capacity_keys]},
                        },
                    }
                }
            )
            try:
                self.repository._client.transact_write_items(TransactItems=transactions)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "TransactionCanceledException":
                    raise BootstrapRefusedError("task execution capacity or ownership unavailable") from None
                raise
        self._current(task["task_id"])
        credential = mint_credential(
            invocation_id=task["invocation_id"],
            attempt=int(task["generation"]),
            tenant_id=tenant,
            credential_epoch=int(grant.get("credential_epoch", 1)),
            persona=task["persona"],
            ttl_seconds=min(900, int((datetime.fromisoformat(task["deadline_at"].replace("Z", "+00:00")) - self.clock()).total_seconds())),
            now=self.clock(),
            env=self.env,
        )
        claims = verify_credential(credential, now=self.clock(), env=self.env)
        return {
            "schema_version": "1.0",
            "task_id": task["task_id"],
            "invocation_id": task["invocation_id"],
            "generation": int(task["generation"]),
            "persona": task["persona"],
            "run_credential": credential,
            "run_credential_expires_at": claims.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "deadline_at": task["deadline_at"],
            "input": grant["input"],
            "model_binding": grant["model_binding"],
            **({"harness": grant["harness"]} if "harness" in grant else {}),
            "limits": grant["limits"],
            "capabilities": grant["capabilities"],
        }

    def authenticate(self, *, credential, pod, require_attempt=True, stop_only=False):
        verifier = verify_credential_for_task_settlement if stop_only else verify_credential
        claims = verifier(credential, now=self.clock(), env=self.env)
        grant = self._grant(claims.tenant_id, claims.invocation_id, claims.attempt, stop_only=stop_only)
        task = self.repository.read_task(grant["task_id"]) if stop_only else self._current(grant["task_id"])
        if task is None or task["scope"]["tenant"] != claims.tenant_id:
            raise BootstrapRefusedError("task binding unavailable")
        if (
            grant.get("workload_uid") != pod.uid
            or grant.get("workload_namespace") != pod.namespace
            or int(grant.get("credential_epoch", 0)) != claims.credential_epoch
            or task["invocation_id"] != claims.invocation_id
            or int(task["generation"]) != claims.attempt
        ):
            raise BootstrapRefusedError("task workload mismatch")
        attempt = task.get("runtime_attempt_id")
        if (require_attempt and not attempt) or grant.get("runtime_attempt_id") != attempt:
            raise BootstrapRefusedError("task attempt mismatch")
        return VerifiedTaskAttempt(
            task["task_id"], claims.invocation_id, claims.attempt, attempt, claims.tenant_id, task["scope"]["canonical_principal"], pod.uid
        )

    def authenticate_settlement(self, *, pod):
        """Resolve stop-only authority from the server-retained pod assignment."""
        delivery = self.repository._get_authority(f"PODTASK#{pod.uid}", "DELIVERY")
        try:
            envelope = json.loads(delivery["body"]) if delivery else None
            if not envelope or envelope.get("kind") != "adp.task":
                raise ValueError("not a Task assignment")
            task = self.repository.read_task(envelope["task_id"])
            assignment = envelope["assignment_ref"]
        except (KeyError, TypeError, ValueError):
            raise BootstrapRefusedError("task settlement assignment unavailable") from None
        if task is None or task["invocation_id"] != envelope.get("invocation_id") or task["request_digest"] != envelope.get("request_digest"):
            raise BootstrapRefusedError("task settlement binding unavailable")
        tenant, generation = task["scope"]["tenant"], int(task["generation"])
        if assignment != {
            "grant_pk": task_authority_partition(tenant),
            "grant_sk": task_run_grant_sort_key(invocation_id=task["invocation_id"], generation=generation),
            "generation": generation,
        }:
            raise BootstrapRefusedError("task settlement generation mismatch")
        grant = self._grant(tenant, task["invocation_id"], generation, stop_only=True)
        attempt = task.get("runtime_attempt_id")
        if (
            grant.get("task_id") != task["task_id"]
            or grant.get("workload_uid") != pod.uid
            or grant.get("workload_namespace") != pod.namespace
            or not attempt
            or grant.get("runtime_attempt_id") != attempt
        ):
            raise BootstrapRefusedError("task settlement workload or attempt mismatch")
        return VerifiedTaskAttempt(task["task_id"], task["invocation_id"], generation, attempt, tenant, task["scope"]["canonical_principal"], pod.uid)

    def register_attempt(self, *, identity, body):
        if (body["task_id"], body["invocation_id"], body["generation"]) != (identity.task_id, identity.invocation_id, identity.generation):
            raise BootstrapRefusedError("task attempt mismatch")
        task = self._current(identity.task_id)
        if task.get("runtime_attempt_id") == body["runtime_attempt_id"]:
            self.ensure_execution_recovery(task["task_id"])
            return  # Response-loss retry of the same committed binding.
        self.repository.bind_runtime_attempt(
            task_id=identity.task_id,
            invocation_id=identity.invocation_id,
            generation=identity.generation,
            runtime_attempt_id=body["runtime_attempt_id"],
            expected_version=int(task["version"]),
            expected_runtime_attempt_id=identity.runtime_attempt_id,
        )
        self.ensure_execution_recovery(task["task_id"])

    def ensure_execution_recovery(self, task_id):
        from src.tasks.records import task_work_partition

        if self.repository._get(task_work_partition(task_id), "RECONCILE") is not None:
            return
        snapshot = self.repository.read_task(task_id)
        self.repository.create_recovery_work(
            task_id=task_id, kind="execution", due_at=self.clock() + timedelta(seconds=60), expected_task_version=int(snapshot["version"])
        )
