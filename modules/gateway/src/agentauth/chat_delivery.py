"""Immutable owner routing and trusted publication of provisional chat output."""

import hashlib
import json
import os
import re
import time
from functools import lru_cache

import boto3
from botocore.config import Config
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, Identifier
from src.orchestration.intake_wiring import _get_sessions_table


class ChatDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: Identifier
    session_id: Identifier
    task_id: Identifier
    thread_id: Identifier
    tenant_id: Identifier
    user_id: Identifier
    team_id: str = Field(max_length=128)
    owner_principal: str = Field(min_length=1, max_length=1024)
    session_generation: int = Field(gt=0)


def protected_delivery(envelope, human_id):
    if envelope.get("channel") != "webchat":
        return None
    owner = json.dumps(
        [envelope["tenant_id"], envelope["tenant_id"], envelope.get("team_id", ""), envelope["user_id"], "webchat"], separators=(",", ":")
    )
    if envelope.get("org_id") != envelope["tenant_id"] or envelope.get("owner_principal") != owner:
        raise ValueError("chat delivery owner mismatch")
    delivery = ChatDelivery(
        run_id=envelope["message_id"],
        session_id=envelope["session_id"],
        task_id=envelope["task_id"],
        thread_id=envelope["thread_id"],
        tenant_id=envelope["tenant_id"],
        user_id=human_id,
        team_id=envelope.get("team_id", ""),
        owner_principal=owner,
        session_generation=envelope["session_generation"],
    )
    return {"S": delivery.model_dump_json()}


def delivery_lookup_key(tenant_id, session_id, task_id):
    digest = envelope_digest({"tenant_id": tenant_id, "session_id": session_id, "task_id": task_id})
    return {"pk": {"S": f"CHAT-TURN#{digest}"}, "sk": {"S": "ROUTING"}}


def delivery_lookup_item(metadata, envelope, human_id):
    delivery = ChatDelivery.model_validate_json(metadata["chat_delivery"]["S"])
    if metadata["chat_delivery"] != protected_delivery(envelope, human_id):
        raise ValueError("chat delivery registration mismatch")
    return {**delivery_lookup_key(delivery.tenant_id, delivery.session_id, delivery.task_id), "run_id": {"S": delivery.run_id}}


def load_registered_delivery(authority, run_id, tenant_id):
    try:
        metadata = authority.store._read(f"TENANT#{tenant_id}", f"EXEC#{run_id}") or {}
        dispatch = authority.store._read(f"INVOCATION#{run_id}", "DISPATCH") or {}
        protected = {key: metadata[key] for key in ("chat_user_turn", "chat_delivery") if key in metadata}
        if dispatch.get("tenant_id") != {"S": tenant_id} or dispatch.get("execution_metadata_digest") != {"S": envelope_digest(protected)}:
            raise ValueError
        delivery = ChatDelivery.model_validate_json(metadata["chat_delivery"]["S"])
        if delivery.run_id != run_id or delivery.tenant_id != tenant_id:
            raise ValueError
        return delivery
    except (KeyError, ValueError, TypeError):
        raise ChatAuthorizationRefusedError("chat owner delivery unavailable") from None


def load_delivery(authority, launch):
    delivery = load_registered_delivery(authority, launch.run_id, launch.tenant_id)
    if any(getattr(delivery, key) != getattr(launch, key) for key in ("user_id", "team_id", "session_id")):
        raise ChatAuthorizationRefusedError("chat owner delivery unavailable")
    return delivery


@lru_cache(maxsize=1)
def response_transport():
    queue = os.environ.get("ADP_CHAT_RESPONSE_QUEUE_URL", "")
    match = re.fullmatch(r"https://sqs\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/[0-9]{12}/[A-Za-z0-9_-]+\.fifo", queue)
    sessions = _get_sessions_table()
    if not match or sessions is None:
        raise ChatAuthorizationUnavailableError("chat response transport unconfigured")
    client = boto3.client("sqs", region_name=match[1], config=Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1}))
    return client, queue, sessions


def verify_delivery_session(delivery, sessions):
    row = sessions.get_item(Key={"session_id": delivery.session_id}, ConsistentRead=True).get("Item", {})
    if (
        row.get("owner_principal") != delivery.owner_principal
        or row.get("created_at") != delivery.session_generation
        or row.get("channel") != "webchat"
        or row.get("expires_at", 0) <= int(time.time())
        or row.get("threads", {}).get(delivery.thread_id, {}).get("processing_task_id") != delivery.task_id
    ):
        raise ChatAuthorizationRefusedError("chat delivery session changed")


class ChatDeliveryRelay:
    def __init__(self, delivery, operation_id, transport, authorize):
        self.delivery = delivery
        self.client, self.queue, self.sessions = transport
        self.authorize = authorize
        self.stream_id = hashlib.sha256(f"{delivery.run_id}\0{operation_id}".encode()).hexdigest()
        self.sequence = 0
        self.started = False
        self.text_bytes = 0

    @classmethod
    async def create(cls, authority, launch, operation_id, authorize):
        delivery = await run_in_threadpool(load_delivery, authority, launch)
        transport = await run_in_threadpool(response_transport)
        relay = cls(delivery, operation_id, transport, authorize)
        await authorize()
        try:
            await run_in_threadpool(relay._session)
        except ChatAuthorizationRefusedError:
            raise
        except Exception:
            raise ChatAuthorizationUnavailableError("chat delivery session unavailable") from None
        return relay

    def _session(self):
        verify_delivery_session(self.delivery, self.sessions)

    def _send(self, event):
        self._session()
        owner = self.delivery
        result = self.client.send_message(
            QueueUrl=self.queue,
            MessageGroupId=owner.session_id,
            MessageDeduplicationId=hashlib.sha256(f"{self.stream_id}:{self.sequence}".encode()).hexdigest(),
            MessageBody=json.dumps(
                {
                    "task_id": owner.task_id,
                    "session_id": owner.session_id,
                    "thread_id": owner.thread_id,
                    "channel": "webchat",
                    "owner_principal": owner.owner_principal,
                    "session_generation": owner.session_generation,
                    "strict_delivery": True,
                    "status": "ag_ui",
                    "ag_ui_event": True,
                    "event": {**event, "stream_id": self.stream_id, "stream_sequence": self.sequence},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        if not result.get("MessageId"):
            raise ChatAuthorizationUnavailableError("chat response handoff uncertain")
        self.sequence += 1

    async def publish(self, event):
        if self.sequence >= 16_384:
            raise ChatAuthorizationUnavailableError("chat response exceeds bound")
        await self.authorize()
        try:
            await run_in_threadpool(self._send, event)
        except ChatAuthorizationRefusedError:
            raise
        except Exception:
            raise ChatAuthorizationUnavailableError("chat response handoff unavailable") from None

    async def model_event(self, event):
        if (
            set(event) != {"type", "index", "text"}
            or event["type"] != "text_delta"
            or type(event["index"]) is not int
            or not 0 <= event["index"] < 64
            or not isinstance(event["text"], str)
            or not event["text"]
        ):
            raise ChatAuthorizationUnavailableError("invalid chat response event")
        self.text_bytes += len(event["text"].encode())
        if self.text_bytes > 65_536 or self.sequence >= 16_380:
            raise ChatAuthorizationUnavailableError("chat response exceeds bound")
        message_id = "chat_" + self.stream_id
        if not self.started:
            await self.publish({"event_type": "RUN_STARTED", "threadId": self.delivery.thread_id, "runId": self.delivery.task_id})
            await self.publish({"event_type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"})
            self.started = True
        for offset in range(0, len(event["text"]), 1024):
            await self.publish({"event_type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": event["text"][offset : offset + 1024]})
