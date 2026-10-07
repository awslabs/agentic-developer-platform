"""Supervisor-only, durable observations of an admitted sandbox's teardown."""

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunchStore


class ChatTeardown:
    def __init__(self, authority, launches):
        self.authority = authority
        self.store = authority.store
        self.launches = launches

    def observe(self, body, role, bindings, *, removed, now):
        pointer = self.store._read(f"INVOCATION#{body.run_id}", "DISPATCH") or {}
        tenant = pointer.get("tenant_id", {}).get("S", "")
        if not tenant or pointer.get("envelope_digest") != {"S": body.envelope_digest}:
            raise ChatAuthorizationRefusedError("chat teardown root refused")
        metadata = self.store._read(f"TENANT#{tenant}", f"EXEC#{body.run_id}") or {}
        launch = self.launches.load(body.run_id)
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
                "observation_scope": self.authority.workloads.observation_scope,
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
            return True
        observed = (
            self.authority.workloads.is_absent(name=body.pod_name)
            if removed
            else self.authority.workloads.has_exited(name=body.pod_name, uid=body.pod_uid, image_digest=launch.image_digest)
        )
        fenced_absence = False
        if (
            not observed
            and not removed
            and launch.session_run_id
            and metadata.get("chat_session_lost")
            == {"M": _encoded({"run_id": launch.run_id, "sandbox_uid": launch.sandbox_uid, "lease_generation": launch.lease_generation})}
        ):
            fenced_absence = self.authority.workloads.is_absent(name=body.pod_name)
            observed = fenced_absence
        if not observed:
            return False
        receipt = {**(previous or {**key, "binding": {"M": binding}}), field: {"N": str(now)}}
        if fenced_absence:
            receipt.update(exit_evidence={"S": "lease_fenced_absence"}, removed_at={"N": str(now)})
        self._persist(receipt, previous, pointer, metadata, launch)
        return True

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
