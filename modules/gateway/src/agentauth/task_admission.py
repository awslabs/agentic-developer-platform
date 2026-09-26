"""Compose authenticated Task admission into the single T1 transaction."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError
from starlette.concurrency import run_in_threadpool

from src.agentauth.task_budget import task_budget
from src.agentauth.task_model_binding import resolve_task_model
from src.agentauth.task_service_policy import TaskServicePolicyStore
from src.tasks.records import idempotency_partition, payload_digest, task_artifact_partition
from src.tasks.store import AcceptanceConditionError, AcceptanceRequest, IdempotencyConflictError, TaskStoreError


class TaskAdmissionError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status
        super().__init__(code)


class TaskAdmission:
    def __init__(self, repository, *, policies=None, budget=None, model_resolver=resolve_task_model, clock=None):
        self.repository = repository
        self.policies = policies or TaskServicePolicyStore(table_name=repository.authority_table_name, client=repository._client)
        self.budget = budget or task_budget(repository)
        self.model_resolver = model_resolver
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def receipt(task, *, replayed):
        return {
            "schema_version": "1.0",
            "task_id": task["task_id"],
            "invocation_id": task["invocation_id"],
            "status": "accepted",
            "created_at": task["created_at"],
            "deadline_at": task["deadline_at"],
            "status_url": "/v1/tasks/" + task["task_id"],
            "events_url": "/v1/tasks/" + task["task_id"] + "/events",
            "request_id": task["dispatch_id"],
            "idempotent_replay": replayed,
        }

    async def admit(self, *, caller, submit, idempotency_key, db):
        caller.require("adp-tasks/submit")
        policy = await run_in_threadpool(self.policies.get, tenant_id=caller.tenant_id, canonical_principal_id=caller.principal_id)
        if not policy or policy["status"] != "active" or "submit" not in policy["task_scopes"]:
            raise TaskAdmissionError("disallowed_scope", 403)
        if submit["persona"] not in policy["allowed_personas"]:
            raise TaskAdmissionError("disallowed_persona", 403)
        from src.agentauth.task_tool_policy import TaskToolPolicyError, freeze_tools

        try:
            tool_grants = freeze_tools(submit["persona"], policy)
        except TaskToolPolicyError:
            raise TaskAdmissionError("prerequisite_unavailable", 503) from None
        from src.agentauth.task_repository_policy import TaskRepositoryPolicyError, freeze_repository

        try:
            repository_binding = freeze_repository(submit.get("inputs", {}), policy)
        except (TaskRepositoryPolicyError, ValueError, TypeError):
            raise TaskAdmissionError("disallowed_repository", 403) from None

        from src.tasks.repository_authority import CODING_PERSONAS, require_coding_snapshot

        digest = payload_digest(submit)
        idem_key = idempotency_partition(tenant=caller.tenant_id, canonical_principal=caller.principal_id, idempotency_key=idempotency_key)
        existing = await run_in_threadpool(self.repository._read_idempotency, idem_key)
        if existing:
            if existing["request_digest"] != digest:
                raise TaskAdmissionError("idempotency_conflict", 409)
            task = await run_in_threadpool(self.repository.read_task, existing["task_id"])
            if task is None:
                raise TaskStoreError("accepted task unavailable")
            return self.receipt(task, replayed=True)
        if submit["persona"] in CODING_PERSONAS:
            await require_coding_snapshot(caller=caller, submit=submit, policy=policy, db=db)
        now = self.clock()
        deadline = now + timedelta(minutes=int(policy["limits"]["max_duration_minutes"]))
        from src.admin.persona_models.catalogue import persona_compatibility_class

        tool_profile = bool(tool_grants) and persona_compatibility_class(submit["persona"]) == "codex-sdk"

        human_owner = caller.principal_id.startswith("human:")
        binding = await self.model_resolver(
            db,
            tenant=caller.tenant_id,
            principal=caller.principal_id,
            deadline=deadline,
            expected_policy_version=policy["model_policy_version"],
            persona=submit["persona"],
            **({"responses_tools": True} if tool_profile else {}),
            **({"include_context": True} if human_owner else {}),
        )
        if human_owner:
            binding, owner_policy, _ = binding
            from src.tasks.human_authority import require_admission_headroom

            await require_admission_headroom(owner_policy.context, policy["limits"]["max_usd_per_task"])
        refs = []
        total_bytes = 0
        for artifact_id in submit.get("artifact_ids", []):
            artifact = await run_in_threadpool(self.repository._get, task_artifact_partition(artifact_id), "META")
            if (
                artifact is None
                or artifact.get("scope") != {"tenant": caller.tenant_id, "canonical_principal": caller.principal_id}
                or artifact.get("binding_state") != "unclaimed"
                or int(artifact.get("expires_at", 0)) <= int(now.timestamp())
            ):
                raise TaskAdmissionError("not_found", 404)
            total_bytes += int(artifact["size_bytes"])
            refs.append({key: artifact[key] for key in ("artifact_id", "version", "content_sha256", "content_type")})
        if total_bytes > 1048576:
            raise TaskAdmissionError("payload_too_large", 413)
        task_id, invocation_id, dispatch_id = "tsk_" + str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        envelope_refs = [{key: ref[key] for key in ("artifact_id", "version", "content_sha256")} for ref in refs]
        input_ref = {"record_type": "TASK", "input_digest": digest, **({"artifact_refs": envelope_refs} if refs else {})}
        assignment = {"grant_pk": "TENANT#" + caller.tenant_id, "grant_sk": f"TASK_RUN#{invocation_id}#GEN#0000000001", "generation": 1}
        envelope = {
            "schema_version": "1.0",
            "kind": "adp.task",
            "task_id": task_id,
            "invocation_id": invocation_id,
            "message_id": invocation_id,
            "dispatch_id": dispatch_id,
            "persona": submit["persona"],
            "request_digest": digest,
            "input_ref": input_ref,
            "assignment_ref": assignment,
        }
        immutable_input = {key: submit[key] for key in ("instructions", "inputs", "acceptance_criteria") if key in submit}
        immutable_input.update(input_digest=digest)
        if refs:
            immutable_input["artifacts"] = refs
        turn_limit = int(policy["limits"]["max_turns"])
        if binding["transport"] == "openai_responses":
            turn_limit = int(policy["limits"].get("codex_max_turns", turn_limit))
        limits = {
            "max_turns": turn_limit,
            "max_output_tokens_per_turn": int(policy["limits"]["max_output_tokens_per_turn"]),
            "max_usd": float(policy["limits"]["max_usd_per_task"]),
            "deadline_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        from src.agentauth.task_harness import TaskHarnessError, assert_bootstrap_size, freeze_harness

        try:
            harness = freeze_harness(persona=submit["persona"], model_binding=binding, limits=limits, service_policy=policy, tool_grants=tool_grants)
            if harness is not None:
                if json.loads(harness["snapshot"]["definition"])["completionPolicy"] == "validated-change":
                    from src.agentauth.task_completion_service import required_acceptance

                    try:
                        required_acceptance((repository_binding or {}).get("binding", {}), submit.get("acceptance_criteria", []))
                        required = {"repository.read", "repository.write", "repository.commit", "validation.run", "change.create"}
                        if not required.issubset(tool_grants):
                            raise TaskStoreError("Developer tools unavailable")
                    except TaskStoreError:
                        raise TaskHarnessError("Developer completion prerequisites unavailable") from None
                assert_bootstrap_size(harness, immutable_input=immutable_input, model_binding=binding, limits=limits)
        except TaskHarnessError:
            raise TaskAdmissionError("prerequisite_unavailable", 503) from None
        scope = hashlib.sha256(("task-nonterminal:tenant:" + caller.tenant_id).encode()).hexdigest()
        try:
            await run_in_threadpool(
                self.repository._client.put_item,
                TableName=self.repository.authority_table_name,
                Item={
                    "pk": {"S": "TASK_CAPACITY#" + scope},
                    "sk": {"S": "ACTIVE"},
                    "active_count": {"N": "0"},
                    "capacity_limit": {"N": "20"},
                    "reservations": {"M": {}},
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        reservation = await self.budget.reserve_admission(
            tenant=caller.tenant_id, principal=caller.principal_id, idempotency_key=idempotency_key, max_usd=limits["max_usd"], request_digest=digest
        )
        rate_scope = hashlib.sha256(
            ("task-rate:" + caller.tenant_id + ":" + caller.principal_id + ":" + now.strftime("%Y%m%d%H%M")).encode()
        ).hexdigest()
        request = AcceptanceRequest(
            task_id=task_id,
            invocation_id=invocation_id,
            dispatch_id=dispatch_id,
            tenant=caller.tenant_id,
            canonical_principal=caller.principal_id,
            idempotency_key=idempotency_key,
            persona=submit["persona"],
            tool_grants=tool_grants,
            repository_binding=repository_binding,
            harness=harness,
            request_payload=submit,
            deadline_at=deadline,
            grant_reference=assignment["grant_sk"],
            envelope=envelope,
            immutable_input=immutable_input,
            model_binding=binding,
            run_limits=limits,
            policy_version=int(policy["version"]),
            capacity_scope_hash=scope,
            capacity_limit=20,
            capacity_reservation_id=str(uuid.uuid4()),
            input_reference=input_ref,
            artifact_ids=tuple(submit.get("artifact_ids", [])),
            budget_reservation=reservation,
            submit_rate_scope_hash=rate_scope,
            submit_rate_window_end=int(now.timestamp()) + 120,
        )
        try:
            accepted = await run_in_threadpool(self.repository.accept, request)
        except AcceptanceConditionError as exc:
            # A definitive cancelled transaction and absent idempotency record
            # proves no provider could have spent this reservation.
            await self.budget.abort_admission(reservation)
            if "rate" in str(exc):
                raise TaskAdmissionError("rate_limited", 429) from None
            if "capacity" in str(exc):
                raise TaskAdmissionError("queue_full", 429) from None
            raise TaskAdmissionError("prerequisite_unavailable", 503) from None
        except IdempotencyConflictError:
            # A concurrent successful acceptance owns the same reservation ID.
            # Never release it from the losing conflicting request.
            raise TaskAdmissionError("idempotency_conflict", 409) from None
        # Unknown outcomes retain the upper-bound hold. A retry reads the same
        # idempotency row; it must never release a possibly committed task's cap.
        task = await run_in_threadpool(self.repository.read_task, accepted.task_id)
        return self.receipt(task, replayed=accepted.replayed)
