"""Owner-scoped legacy memory inventory and explicitly opted-in migration."""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from src.agentauth.agentcore_authority import shared_owner_header
from src.agentauth.agentcore_governance import advance_owner_state, owner_state
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_memory import ChatMemoryStore
from src.orchestration.chat_data_migration import _memory_owner, _owns_context_row


class LegacyMemoryMigration:
    def __init__(self, memory_table, context_table, provider, capabilities):
        self.memory = memory_table
        self.context = context_table
        self.provider = provider
        self.capabilities = capabilities

    def _authorize(self, owner):
        self.capabilities._member(owner.tenant_id, owner.user_id, owner.team_id)
        shared_owner_header(self.context, owner)

    def _source(self, row, owner, now):
        session_id = row.get("source", {}).get("sessionId") if isinstance(row.get("source"), dict) else None
        if not isinstance(session_id, str) or not session_id:
            return "source_unknown"
        source = SimpleNamespace(tenant_id=owner.tenant_id, team_id=owner.team_id, user_id=owner.user_id, session_id=session_id)
        try:
            shared_owner_header(self.context, source)
        except ChatAuthorizationRefusedError:
            return "source_not_shared"
        if _memory_owner(row) != (owner.tenant_id, owner.team_id, owner.user_id):
            return "owner_conflict"
        if row.get("aclUserIds", []) not in ([], [owner.user_id]):
            return "sharing_ambiguous"
        if row.get("kind") == "draft-learning" or not isinstance(row.get("content"), str) or len(row["content"].encode()) > 8192:
            return "record_not_publishable"
        try:
            store = ChatMemoryStore(self.memory, None, None)
            store._record(row)
            if store._expired(row, now):
                return "expired"
            if datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00")).tzinfo is None:
                return "record_invalid"
        except (ValueError, KeyError, TypeError, ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError):
            return "record_invalid"
        return None

    def inventory(self, owner, *, now, limit=25, cursor=None):
        self._authorize(owner)
        if not 1 <= limit <= 25:
            raise ValueError("migration page limit invalid")
        partition = f"memory-owner#{owner.tenant_id}#{owner.user_id}"
        if cursor is not None and (not isinstance(cursor, str) or not cursor.startswith("mem#") or len(cursor) > 256):
            raise ChatAuthorizationRefusedError("migration cursor refused")
        query = {"KeyConditionExpression": Key("PK").eq(partition) & Key("SK").begins_with("mem#"), "ConsistentRead": True, "Limit": limit}
        if cursor:
            query["ExclusiveStartKey"] = {"PK": partition, "SK": cursor}
        page = self.memory.query(**query)
        counts = {"eligible": 0, "quarantined": 0, "expired": 0}
        candidates = []
        for pointer in page.get("Items", []):
            reason = "index_invalid"
            reference = pointer.get("id")
            if (
                pointer.get("PK") == partition
                and isinstance(reference, str)
                and pointer.get("SK", "").endswith(f"#{reference}")
                and _owns_context_row(pointer, (owner.tenant_id, owner.team_id, owner.user_id))
            ):
                row = self.memory.get_item(Key={"PK": f"memory#{reference}", "SK": "record"}, ConsistentRead=True).get("Item")
                reason = self._source(row, owner, now) if row else "record_missing"
                if reason is None and pointer["SK"] != f"mem#{row['createdAt']}#{reference}":
                    reason = "index_invalid"
                if reason is None and owner_state(self.context, owner)["epoch"]:
                    reason = "governance_fenced"
            if reason is None:
                counts["eligible"] += 1
                candidates.append(row)
            elif reason == "expired":
                counts["expired"] += 1
            else:
                counts["quarantined"] += 1
        continuation = page.get("LastEvaluatedKey")
        if continuation and (continuation.get("PK") != partition or not continuation.get("SK", "").startswith("mem#")):
            raise ChatAuthorizationUnavailableError("migration page unavailable")
        return {"counts": counts, "next_cursor": continuation["SK"] if continuation else None, "candidates": candidates}

    def migrate(self, owner, *, now, limit=25, cursor=None):
        page = self.inventory(owner, now=now, limit=limit, cursor=cursor)
        state = owner_state(self.context, owner)
        namespace = self.provider.owner_namespace(owner)
        receipts = []
        for record in page["candidates"]:
            self._authorize(owner)
            current = self.memory.get_item(Key={"PK": record["PK"], "SK": "record"}, ConsistentRead=True).get("Item")
            if current != record or self._source(record, owner, now) is not None or owner_state(self.context, owner) != state:
                raise ChatAuthorizationRefusedError("migration authority changed")
            digest = hashlib.sha256(
                json.dumps([owner.tenant_id, owner.user_id, record["id"], int(record["version"])], separators=(",", ":")).encode()
            ).hexdigest()
            key = {"PK": f"memory-migration#{digest}", "SK": "journal"}
            entry = self.context.get_item(Key=key, ConsistentRead=True).get("Item")
            if entry is None:
                try:
                    self.context.put_item(
                        Item={
                            **key,
                            "tenantId": owner.tenant_id,
                            "ownerUserId": owner.user_id,
                            "sourceId": record["id"],
                            "sourceVersion": record["version"],
                            "memoryId": self.provider.memory_id,
                            "strategyId": self.provider.strategy_id,
                            "ownerEpoch": state["epoch"],
                            "status": "pending",
                        },
                        ConditionExpression="attribute_not_exists(PK)",
                    )
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise
                entry = self.context.get_item(Key=key, ConsistentRead=True).get("Item")
            if (
                not entry
                or entry.get("ownerEpoch") != state["epoch"]
                or entry.get("memoryId") != self.provider.memory_id
                or entry.get("strategyId") != self.provider.strategy_id
                or entry.get("status") not in ("pending", "accepted")
            ):
                raise ChatAuthorizationRefusedError("migration journal fenced")
            if entry["status"] == "pending":
                event = self.provider.client.create_event(
                    memoryId=self.provider.memory_id,
                    actorId=namespace.rsplit("/", 1)[-1],
                    sessionId=record["source"]["sessionId"],
                    eventTimestamp=datetime.fromisoformat(record["createdAt"].replace("Z", "+00:00")).astimezone(UTC),
                    payload=[{"conversational": {"content": {"text": record["content"]}, "role": "USER"}}],
                    metadata={
                        "adp_receipt": {"stringValue": digest},
                        "adp_epoch": {"stringValue": str(state["epoch"])},
                        "adp_legacy_source": {"stringValue": f"{record['id']}:{record['version']}"},
                    },
                    clientToken=digest,
                )
                service_event = (event.get("event") or {}) if isinstance(event, dict) else {}
                if (
                    service_event.get("memoryId") != self.provider.memory_id
                    or service_event.get("actorId") != namespace.rsplit("/", 1)[-1]
                    or not service_event.get("eventId")
                ):
                    raise ChatAuthorizationUnavailableError("migration receipt unavailable")
                self._authorize(owner)
                if owner_state(self.context, owner) != state:
                    raise ChatAuthorizationRefusedError("migration authority changed")
                self.context.update_item(
                    Key=key,
                    UpdateExpression="SET #status = :accepted, serviceEventId = :event",
                    ConditionExpression="#status = :pending AND ownerEpoch = :epoch",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":accepted": "accepted",
                        ":pending": "pending",
                        ":epoch": state["epoch"],
                        ":event": service_event["eventId"],
                    },
                )
            receipts.append({"receipt_id": digest, "status": "pending"})
        return {"counts": page["counts"], "next_cursor": page["next_cursor"], "receipts": receipts}

    def rollback(self, owner, *, receipt_id):
        self._authorize(owner)
        key = {"PK": f"memory-migration#{receipt_id}", "SK": "journal"}
        entry = self.context.get_item(Key=key, ConsistentRead=True).get("Item")
        if not entry or (entry.get("tenantId"), entry.get("ownerUserId")) != (owner.tenant_id, owner.user_id):
            raise ChatAuthorizationRefusedError("migration receipt refused")
        if entry["status"] != "rolled_back":
            advance_owner_state(self.context, owner, receipt_id, receipt_id, "forget")
            self.context.update_item(
                Key=key,
                UpdateExpression="SET #status = :rolled",
                ConditionExpression="#status = :accepted OR #status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":rolled": "rolled_back", ":accepted": "accepted", ":pending": "pending"},
            )
        return {"status": "rollback_pending_service_cleanup", "receipt_id": receipt_id}
