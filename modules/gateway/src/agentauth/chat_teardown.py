"""Supervisor-only, durable observations of an admitted sandbox's teardown."""

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunchStore
from src.agentauth.chat_history_write import ChatHistoryConflictError


class ChatSandboxRemovalUnconfirmedError(ChatHistoryConflictError):
    """Finalize/complete asked for removal evidence the API server does not yet give.

    Distinct from the generic history conflict so the supervisor can re-await the pod's removal
    and retry instead of giving up (#8047); the route maps it to 409 ``chat_sandbox_removal_unconfirmed``.
    """


def sandbox_removed(workloads, *, name, uid):
    """Absent from the API server, or still returned only while the kubelet reclaims an exited pod."""
    return workloads.is_absent(name=name) or workloads.is_terminating(name=name, uid=uid)


class ChatTeardown:
    def __init__(self, authority, launches, crew_store=None):
        self.authority = authority
        self.store = authority.store
        self.launches = launches
        self.crew_store = crew_store

    def workloads_for_cleanup(self, run_id):
        previous = self.store._read(f"CHAT-LAUNCH#{run_id}", "TEARDOWN")
        if previous is None:
            return self.authority.workloads_for_run(run_id)
        scope = previous.get("binding", {}).get("M", {}).get("observation_scope")
        if scope == {"M": _encoded(self.authority.workloads.observation_scope)}:
            return self.authority.workloads
        if self.authority.crew_workloads is not None:
            crew = self.authority.crew_workloads()
            if scope == {"M": _encoded(crew.observation_scope)}:
                return crew
        raise ChatAuthorizationRefusedError("chat cleanup observation scope changed")

    def observe(self, body, role, bindings, *, removed, now):
        pointer = self.store._read(f"INVOCATION#{body.run_id}", "DISPATCH") or {}
        tenant = pointer.get("tenant_id", {}).get("S", "")
        if not tenant or pointer.get("envelope_digest") != {"S": body.envelope_digest}:
            raise ChatAuthorizationRefusedError("chat teardown root refused")
        metadata = self.store._read(f"TENANT#{tenant}", f"EXEC#{body.run_id}") or {}
        launch = self.launches.load(body.run_id)
        workloads = self.workloads_for_cleanup(launch.run_id)
        if (
            not any(
                binding.chat_supervisor_role == role and binding.tenant_id == tenant and metadata.get("persona", {}).get("S") in binding.personas
                for binding in bindings
            )
            or launch.tenant_id != tenant
            or metadata.get("repo") != {"S": f"chat/{launch.session_id}"}
            or metadata.get("current_attempt") != {"N": str(launch.attempt)}
            or metadata.get("current_credential_epoch") != {"N": str(launch.credential_epoch)}
            or metadata.get("workload_binding") != {"S": launch.sandbox_uid}
            or launch.sandbox_uid != body.pod_uid
            or metadata.get("pod_name") != {"S": body.pod_name}
        ):
            raise ChatAuthorizationRefusedError("chat teardown scope refused")
        binding = _encoded(
            {
                "run_id": launch.run_id,
                "envelope_digest": body.envelope_digest,
                "tenant_id": tenant,
                "session_id": launch.session_id,
                "pod_name": body.pod_name,
                "pod_uid": launch.sandbox_uid,
                "image_digest": launch.image_digest,
                "attempt": launch.attempt,
                "credential_epoch": launch.credential_epoch,
                "lease_generation": launch.lease_generation,
                "observation_scope": workloads.observation_scope,
            }
        )
        key = _encoded({"pk": f"CHAT-LAUNCH#{launch.run_id}", "sk": "TEARDOWN"})
        previous = self.store._read(key["pk"]["S"], key["sk"]["S"])
        if previous is not None and (previous.get("binding") != {"M": binding} or "exited_at" not in previous):
            raise ChatAuthorizationRefusedError("chat teardown evidence mismatch")
        if removed and previous is None:
            raise ChatAuthorizationRefusedError("chat exit evidence required")
        field = "removed_at" if removed else "exited_at"
        if previous is not None and field in previous:
            self._persist(previous, previous, pointer, metadata, launch)
            if removed:
                self._revoke_crew(launch)
            return True
        observed = (
            sandbox_removed(workloads, name=body.pod_name, uid=body.pod_uid)
            if removed
            else workloads.has_exited(name=body.pod_name, uid=body.pod_uid, image_digest=launch.image_digest)
        )
        fenced_absence = False
        if (
            not observed
            and not removed
            and launch.session_run_id
            and metadata.get("chat_session_lost")
            == {"M": _encoded({"run_id": launch.run_id, "sandbox_uid": launch.sandbox_uid, "lease_generation": launch.lease_generation})}
        ):
            fenced_absence = workloads.is_absent(name=body.pod_name)
            observed = fenced_absence
        if not observed:
            return False
        receipt = {**(previous or {**key, "binding": {"M": binding}}), field: {"N": str(now)}}
        if fenced_absence:
            receipt.update(exit_evidence={"S": "lease_fenced_absence"}, removed_at={"N": str(now)})
        self._persist(receipt, previous, pointer, metadata, launch)
        if removed:
            self._revoke_crew(launch)
        return True

    def _revoke_crew(self, launch):
        if self.crew_store is not None and launch.session_run_id:
            self.crew_store.revoke_for_lease(tenant_id=launch.tenant_id, session_id=launch.session_id, lease_generation=launch.lease_generation)

    def _unchanged(self, item):
        fields = [field for field in item if field not in {"pk", "sk"}]
        return {
            "TableName": self.store.table,
            "Key": {field: item[field] for field in ("pk", "sk")},
            "ConditionExpression": " AND ".join(f"#field{index} = :value{index}" for index in range(len(fields))),
            "ExpressionAttributeNames": {f"#field{index}": field for index, field in enumerate(fields)},
            "ExpressionAttributeValues": {f":value{index}": item[field] for index, field in enumerate(fields)},
        }

    def _persist(self, receipt, previous, pointer, metadata, launch):
        put = {"TableName": self.store.table, "Item": receipt, "ConditionExpression": "attribute_not_exists(pk)"}
        if previous is not None:
            put.update({key: value for key, value in self._unchanged(previous).items() if key != "Key"})
            if "removed_at" not in previous:
                put["ConditionExpression"] += " AND attribute_not_exists(removed_at)"
        try:
            self.store.client.transact_write_items(
                TransactItems=[
                    {"ConditionCheck": self._unchanged(pointer)},
                    {"ConditionCheck": self._unchanged(metadata)},
                    {"ConditionCheck": self._unchanged(ChatLaunchStore.item(launch))},
                    {"Put": put},
                ]
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise ChatAuthorizationRefusedError("chat teardown changed; retry observation") from None
            raise ChatAuthorizationUnavailableError("chat teardown persistence unavailable") from None
        except BotoCoreError:
            raise ChatAuthorizationUnavailableError("chat teardown persistence unavailable") from None
