"""Verify buffered cancellation against retained input and its protected registration."""

import hashlib
import json
import time

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.agentauth.chat_delivery import load_registered_delivery
from src.agentauth.chat_teardown import ChatTeardown
from src.agentauth.model_policy import canonical_json


def pending_cancellation_fence(service, delivery):
    try:
        row = service.sessions.get_item(Key={"session_id": delivery.session_id}, ConsistentRead=True).get("Item", {})
        thread = row.get("threads", {}).get(delivery.thread_id, {})
        now = int(time.time())
        if (
            row.get("owner_principal") != delivery.owner_principal
            or row.get("created_at") != delivery.session_generation
            or row.get("channel") != "webchat"
            or row.get("expires_at", 0) <= now
        ):
            raise ValueError
        if thread.get("processing_task_id") == delivery.task_id:
            return None
        selected = hashlib.sha256(delivery.run_id.encode()).hexdigest()
        record = thread["pending_turns"][selected]
        request = json.loads(record["request_json"])
        envelope = {
            **request,
            "persona": request["agent_type"],
            "source_ref": {**request.get("source_ref", {}), "repo": f"chat/{delivery.session_id}"},
            "correlation": {"correlation_id": delivery.run_id, "root_human_id": delivery.user_id, "is_human_rooted": True},
        }
        envelope.pop("parent_principal", None)
        encoded = canonical_json(envelope).decode()
        markers = [message for message in thread["messages"] if message.get("pending_id") == selected]
        fields = ("session_id", "thread_id", "task_id", "tenant_id", "team_id", "owner_principal", "session_generation")
        if (
            not isinstance(thread.get("processing_task_id"), str)
            or not thread["processing_task_id"]
            or selected in thread.get("scheduled_turns", {})
            or record["status"] not in {"registering", "registered"}
            or (record["status"] == "registered" and record.get("envelope_json") != encoded)
            or request["message_id"] != delivery.run_id
            or request.get("channel") != "webchat"
            or any(request.get(field) != getattr(delivery, field) for field in fields)
            or len(markers) != 1
            or markers[0].get("role") != "user"
            or markers[0].get("content") != request["message"][:10000]
            or load_registered_delivery(service, delivery.run_id, delivery.tenant_id) != delivery
        ):
            raise ValueError
        pointer = service.store._read(f"INVOCATION#{delivery.run_id}", "DISPATCH") or {}
        execution = service.store._read(f"TENANT#{delivery.tenant_id}", f"EXEC#{delivery.run_id}") or {}
        digest = {"S": envelope_digest(envelope)}
        if pointer.get("envelope_digest") != digest or execution.get("envelope_digest") != digest:
            raise ValueError
        evidence = ChatTeardown(service, None)
        checks = [
            {
                "ConditionCheck": {
                    "TableName": service.sessions.name,
                    "Key": _encoded({"session_id": delivery.session_id}),
                    "ConditionExpression": (
                        "owner_principal = :owner AND created_at = :generation AND #channel = :channel "
                        "AND expires_at > :now AND threads.#thread = :thread"
                    ),
                    "ExpressionAttributeNames": {"#channel": "channel", "#thread": delivery.thread_id},
                    "ExpressionAttributeValues": _encoded(
                        {
                            ":owner": delivery.owner_principal,
                            ":generation": delivery.session_generation,
                            ":channel": "webchat",
                            ":now": now,
                            ":thread": thread,
                        }
                    ),
                }
            },
            {"ConditionCheck": evidence._unchanged(pointer)},
            *[
                {
                    "ConditionCheck": {
                        "TableName": service.store.table,
                        "Key": _encoded({"pk": f"CHAT-LAUNCH#{delivery.run_id}", "sk": sort_key}),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
                for sort_key in ("LAUNCH", "CREATION")
            ],
        ]
        return {"checks": checks, "execution": evidence._unchanged(execution)}
    except (BotoCoreError, ClientError):
        raise ChatAuthorizationUnavailableError("chat buffered cancellation unavailable") from None
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ChatAuthorizationRefusedError("chat buffered cancellation binding changed") from None
