"""Bounded references and cleanup for records created by fixed E45 operations."""

import hashlib
import os
import re

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatLaunchStore
from src.agentauth.chat_qualification import QualificationUnavailableError
from src.agentauth.chat_teardown import ChatTeardown


def operation_key(binding, operation):
    return f"e45-{binding['nonce']}-{operation}"


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


def removed(authority, binding):
    row = authority.store._read(f"CHAT-LAUNCH#{binding['run_id']}", "TEARDOWN") or {}
    observed = row.get("binding", {}).get("M", {})
    fields = {"run_id": "run_id", "session_id": "session_id", "pod_uid": "sandbox_uid", "image_digest": "image_digest", "tenant_id": "tenant_id"}
    return bool(
        row.get("removed_at")
        and all(observed.get(field) == {"S": binding[source]} for field, source in fields.items())
        and all(observed.get(field) == {"N": str(binding[field])} for field in ("lease_generation", "attempt", "credential_epoch"))
    )


class QualificationFixtures:
    def __init__(self, qualification, memory, artifacts, storage, bucket):
        self.qualification = qualification
        self.memory, self.artifacts, self.storage, self.bucket = memory, artifacts, storage, bucket

    def _owned(self, row, binding):
        if not row or any(
            row.get(field) != binding[source]
            for field, source in (
                ("tenantId", "tenant_id"),
                ("teamId", "team_id"),
                ("ownerUserId", "user_id"),
            )
        ):
            raise ChatAuthorizationRefusedError("qualification fixture ownership refused")
        return row

    def _receipt(self, binding, prefix, operation):
        row = self.qualification.history._get(binding["session_id"], prefix + hashed(operation_key(binding, operation)))
        self._owned(row, binding)
        if row.get("runId") != binding["run_id"] or row.get("leaseGeneration") != binding["lease_generation"]:
            raise ChatAuthorizationRefusedError("qualification fixture run refused")
        return row

    def _delete_object(self, path):
        self.storage.delete_object(Bucket=self.bucket, Key=path)
        try:
            self.storage.head_object(Bucket=self.bucket, Key=path)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
                raise
        else:
            raise QualificationUnavailableError("chat_qualification_object_cleanup_unverified")

    def capture(self, token, request, *, now):
        service = self.qualification
        configured = service._request(request, now)
        launch, header, record = service._bound_record(token, request, configured, now, "claimed")
        binding = record["binding"]
        if binding["nonce"] != request.nonce or binding["actor"] != "a1":
            raise ChatAuthorizationRefusedError("qualification fixture publisher refused")
        if record.get("fixtures"):
            return record["fixtures"]
        if not configured.secret_canary:
            raise QualificationUnavailableError("chat_qualification_secret_canary_unavailable")
        message = self._receipt(binding, "write#", "message_write")["result"]["message_id"]
        summary = self._receipt(binding, "summary-write#", "summary_write")["result"]["summary_id"]
        artifact = self._receipt(binding, "artifact-write#", "artifact_write")["artifactId"]
        self._receipt(binding, "draft-write#", "session_write")
        partition = f"memory-owner#{binding['tenant_id']}#{binding['user_id']}"
        memory_receipt = self.memory.get_item(
            Key={"PK": partition, "SK": "write#" + hashed(f"{binding['run_id']}#{operation_key(binding, 'memory_write')}")}, ConsistentRead=True
        ).get("Item")
        self._owned(memory_receipt, binding)
        memory_id = memory_receipt["result"]["memory_id"]
        memory_row = self._owned(self.memory.get_item(Key={"PK": f"memory#{memory_id}", "SK": "record"}, ConsistentRead=True).get("Item"), binding)
        if memory_row.get("source") != {"sessionId": binding["session_id"], "runId": binding["run_id"]}:
            raise ChatAuthorizationRefusedError("qualification memory provenance refused")
        pointer = self._owned(service.history._get(binding["session_id"], f"artifact-id#{artifact}"), binding)
        artifact_row = self._owned(
            self.artifacts.get_item(Key={"PK": header["PK"], "SK": pointer["catalogKey"]}, ConsistentRead=True).get("Item"), binding
        )
        if artifact_row.get("runId") != binding["run_id"]:
            raise ChatAuthorizationRefusedError("qualification artifact provenance refused")
        page = service.history.read_page(token, run_id=request.run_id, session_id=request.session_id, now=now, limit=1)
        if not page["next_cursor"]:
            raise QualificationUnavailableError("chat_qualification_cursor_unavailable")
        fixtures = {
            "owner": binding,
            "message_id": message,
            "summary_id": summary,
            "artifact_id": artifact,
            "memory_id": memory_id,
            "cursor": page["next_cursor"],
            "history_version": page["version"],
            "memory_version": int(memory_row["version"]),
            "cursor_expires_at": min(now + 300, launch.expires_at, int(header["ttl"])),
            "bucket": self.bucket,
            "object_key": artifact_row["s3Key"],
            "context_table": service.table.name,
            "secret_canary": configured.secret_canary,
            "region": os.environ.get("AWS_REGION", "us-east-1"),
        }
        update = service.writer._unchanged(record)["ConditionCheck"]
        update["UpdateExpression"] = "SET fixtures = :fixtures"
        update["ExpressionAttributeValues"].update(_encoded({":fixtures": fixtures}))
        service._transact(service._fences(launch, header, now) + [{"Update": update}])
        return fixtures

    def cleanup(self, user, session_id, request, *, now):
        service = self.qualification
        owned = service.result(user, session_id, request, now=now)
        binding = owned["binding"]
        launch = service.capabilities.launches.load(binding["run_id"])
        if any(
            getattr(launch, field) != binding[field]
            for field in (
                "tenant_id",
                "user_id",
                "team_id",
                "session_id",
                "sandbox_uid",
                "lease_generation",
                "attempt",
                "credential_epoch",
            )
        ):
            raise ChatAuthorizationRefusedError("qualification cleanup generation changed")
        if not removed(service.authority, binding):
            raise QualificationUnavailableError("chat_qualification_teardown_unavailable")
        execution = service.authority.store._read(f"TENANT#{binding['tenant_id']}", f"EXEC#{binding['run_id']}") or {}
        if (
            execution.get("workload_binding") != {"S": binding["sandbox_uid"]}
            or execution.get("current_attempt") != {"N": str(binding["attempt"])}
            or execution.get("current_credential_epoch") != {"N": str(binding["credential_epoch"])}
        ):
            raise ChatAuthorizationRefusedError("qualification cleanup execution changed")
        if execution.get("status") == {"S": "active"} and not execution.get("chat_turn_sealed") and not execution.get("abort_command_id"):
            raise QualificationUnavailableError("chat_qualification_run_not_sealed")
        record = service._read(service._key(session_id, request.evaluation_id))
        if record.get("cleanup_complete"):
            return {"cleanup_complete": True, "nonce": binding["nonce"]}
        if binding["actor"] == "a1":
            artifact_id = (
                "art_" + hashed(f"session#{session_id}#{binding['tenant_id']}#{binding['user_id']}#{operation_key(binding, 'artifact_write')}")[:32]
            )
            path = f"o/{binding['tenant_id']}/t/{binding['team_id'] or '~personal'}/u/{binding['user_id']}/s/{session_id}/gateway/out/{artifact_id}"
            self._delete_object(path)
        changes = []
        tables = {table.name: table for table in (service.table, self.artifacts)}
        for table in tables.values():
            page = table.query(KeyConditionExpression=Key("PK").eq(f"session#{session_id}"), Limit=64, ConsistentRead=True)
            if page.get("LastEvaluatedKey"):
                raise QualificationUnavailableError("chat_qualification_cleanup_bound_exceeded")
            for row in page.get("Items", []):
                if row.get("runId") != binding["run_id"]:
                    continue
                self._owned(row, binding)
                if row.get("leaseGeneration") != binding["lease_generation"]:
                    raise ChatAuthorizationRefusedError("qualification cleanup record generation changed")
                if "s3Key" in row:
                    expected = (
                        f"o/{binding['tenant_id']}/t/{binding['team_id'] or '~personal'}/u/{binding['user_id']}/"
                        f"s/{session_id}/gateway/out/{row['id']}"
                    )
                    if row["s3Key"] != expected or not re.fullmatch(r"art_[a-f0-9]{12,64}", row["id"]):
                        raise ChatAuthorizationRefusedError("qualification object cleanup scope refused")
                    self._delete_object(expected)
                check = service.writer._unchanged(row)["ConditionCheck"]
                check["TableName"] = table.name
                changes.append({"Delete": check})
        partition = f"memory-owner#{binding['tenant_id']}#{binding['user_id']}"
        key = {"PK": partition, "SK": "write#" + hashed(f"{binding['run_id']}#{operation_key(binding, 'memory_write')}")}
        receipt = self.memory.get_item(Key=key, ConsistentRead=True).get("Item")
        if receipt:
            self._owned(receipt, binding)
            memory_id = receipt["result"]["memory_id"]
            row = self.memory.get_item(Key={"PK": f"memory#{memory_id}", "SK": "record"}, ConsistentRead=True).get("Item")
            if not row:
                raise QualificationUnavailableError("chat_qualification_memory_cleanup_unverified")
            if row:
                self._owned(row, binding)
                if row.get("source") != {"sessionId": session_id, "runId": binding["run_id"]}:
                    raise ChatAuthorizationRefusedError("qualification memory cleanup scope refused")
                pointer = self.memory.get_item(Key={"PK": partition, "SK": f"mem#{row['createdAt']}#{memory_id}"}, ConsistentRead=True).get("Item")
                for item in (row, pointer, receipt):
                    if item:
                        self._owned(item, binding)
                        check = service.writer._unchanged(item)["ConditionCheck"]
                        check["TableName"] = self.memory.name
                        changes.append({"Delete": check})
        if len(changes) > 90:
            raise QualificationUnavailableError("chat_qualification_cleanup_bound_exceeded")
        update = service.writer._unchanged(record)["ConditionCheck"]
        update["UpdateExpression"] = "SET cleanup_complete = :complete"
        update["ExpressionAttributeValues"].update(_encoded({":complete": True}))
        fences = ChatTeardown(service.authority, None)
        service._transact(
            changes
            + [
                {"ConditionCheck": fences._unchanged(ChatLaunchStore.item(launch))},
                {"ConditionCheck": fences._unchanged(execution)},
                {"Update": update},
            ]
        )
        return {"cleanup_complete": True, "nonce": binding["nonce"]}
