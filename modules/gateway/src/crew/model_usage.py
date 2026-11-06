"""Reserve and record EC2 Crew model usage under a verified owner run."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import HTTPException
from sqlalchemy import select

from src.crew.model_token import verify_model_token
from src.shared.models.onboarding import TenantMembership

logger = logging.getLogger(__name__)


def _well_formed_usage(usage) -> bool:
    return isinstance(usage, dict) and all(type(usage.get(field)) is int and usage[field] >= 0 for field in ("input_tokens", "output_tokens"))


def _collect_stream_usage(chunk: bytes, usage: dict) -> None:
    """Fold the usage carried by Anthropic SSE events into ``usage``.

    ``message_start`` carries ``input_tokens`` (and the cache counters); ``message_delta``
    carries the final ``output_tokens``. Anything unparseable is ignored: the stream is
    the client's, accounting only reads it.
    """
    try:
        text = chunk.decode("utf-8", errors="ignore")
    except AttributeError:
        return
    for line in text.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "message_start":
            start = event.get("message", {}).get("usage", {}) if isinstance(event.get("message"), dict) else {}
            if isinstance(start, dict):
                usage.update({name: value for name, value in start.items() if name != "output_tokens"})
        elif event.get("type") == "message_delta" and isinstance(event.get("usage"), dict):
            usage.update(event["usage"])


def _denied():
    return HTTPException(403, detail={"error": "crew_model_usage_denied"})


class CrewModelUsage:
    def __init__(self, run_store):
        self.run_store = run_store
        self.table = run_store.table

    async def _reserve(self, token, target, model_id, *, now):
        """Verify the host's model token and reserve a pending receipt under its run.

        Returns the receipt key; ``_confirm``/``_fail`` close it. Shared by the
        buffered and the streaming call so both account identically.
        """
        claims = await verify_model_token(token, target, now=now)
        if not isinstance(model_id, str) or not model_id or len(model_id) > 256:
            raise HTTPException(422, detail={"error": "crew_model_invalid"})
        receipt_id = str(uuid4())
        row = await asyncio.to_thread(self.run_store.read_run, claims["run"])
        if (
            row is None
            or row.get("kind") != "crew"
            or row.get("status") != "active"
            or row.get("tenant_id") != claims["tenant"]
            or row.get("user_id") != claims["owner"]
            or row.get("session_id") != claims["box"]
            or row.get("target_ref") != claims["instance"]
            or row.get("lease_generation") != claims["generation"]
            or row.get("lease_expires_at", 0) <= now
        ):
            raise HTTPException(401, detail={"error": "crew_model_credential_denied"})
        key = {"event_id": f"crew-model#{claims['run']}", "arrived_at": receipt_id}
        receipt = {
            **key,
            "run_id": claims["run"],
            "tenant_id": claims["tenant"],
            "user_id": claims["owner"],
            "machine_generation": claims["generation"],
            "model_id": model_id,
            "status": "pending",
            "created_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "expires_at": now + 30 * 86400,
        }
        try:
            await asyncio.to_thread(
                self.table.meta.client.transact_write_items,
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self.table.name,
                            "Key": {"event_id": claims["run"], "arrived_at": row["arrived_at"]},
                            "ConditionExpression": "#status = :active AND lease_expires_at > :now AND lease_generation = :generation",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {":active": "active", ":now": now, ":generation": claims["generation"]},
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": self.table.name,
                            "Key": {"event_id": f"crew-lease#{claims['tenant']}#{claims['box']}#{claims['generation']}", "arrived_at": "crew"},
                            "ConditionExpression": "#status = :active AND run_id = :run",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {":active": "active", ":run": claims["run"]},
                        }
                    },
                    {"Put": {"TableName": self.table.name, "Item": receipt, "ConditionExpression": "attribute_not_exists(event_id)"}},
                ],
            )
        except ClientError as error:
            # The refusal stays opaque to the host, but the operator needs the code: on
            # dev 2026-10-20 this was AccessDeniedException for ConditionCheckItem and
            # every Crew model call read as a credential problem (#8223 cause 32).
            logger.warning(
                "Crew model usage reservation refused run=%s code=%s",
                claims["run"],
                error.response.get("Error", {}).get("Code"),
            )
            raise HTTPException(401, detail={"error": "crew_model_credential_denied"}) from None
        return key

    async def _fail(self, key):
        await asyncio.to_thread(
            self.table.update_item,
            Key=key,
            UpdateExpression="SET #status = :failed",
            ConditionExpression="#status = :pending",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":failed": "failed", ":pending": "pending"},
        )

    async def _confirm(self, key, usage):
        await asyncio.to_thread(
            self.table.update_item,
            Key=key,
            UpdateExpression="SET #status = :confirmed, #usage = :usage",
            ConditionExpression="#status = :pending",
            ExpressionAttributeNames={"#status": "status", "#usage": "usage"},
            ExpressionAttributeValues={
                ":confirmed": "confirmed",
                ":pending": "pending",
                ":usage": {field: usage[field] for field in ("input_tokens", "output_tokens")},
            },
        )

    async def invoke(self, token, target, model_id, call, *, now):
        key = await self._reserve(token, target, model_id, now=now)
        try:
            result = await call()
            usage = result.get("usage") if isinstance(result, dict) else None
            if not _well_formed_usage(usage):
                raise ValueError("model response lacks usage")
        except BaseException:
            await self._fail(key)
            raise
        await self._confirm(key, usage)
        return result

    async def invoke_stream(self, token, target, model_id, open_stream, *, now):
        """The streaming twin of :meth:`invoke` (Dev Box latency, #8223).

        Reserves the receipt before the provider is opened, then yields the SSE bytes
        through untouched. The receipt is confirmed only when the stream has ended with
        the usage Bedrock reports in ``message_start`` (input) and ``message_delta``
        (output); a stream that errors, is cut by the client, or ends without usage is
        recorded ``failed`` — exactly what the buffered call does for a bad reply.
        """
        key = await self._reserve(token, target, model_id, now=now)
        try:
            stream = await open_stream()
        except BaseException:
            await self._fail(key)
            raise
        return self._account_stream(stream, key)

    async def _account_stream(self, stream, key):
        usage: dict = {}
        completed = False
        try:
            async for chunk in stream:
                if chunk:
                    _collect_stream_usage(chunk, usage)
                yield chunk
            completed = True
        finally:
            if completed and _well_formed_usage(usage):
                await self._confirm(key, usage)
            else:
                await self._fail(key)

    async def receipts(self, run_id, identity, db):
        if identity.account_type != "human" or not isinstance(run_id, str) or not run_id:
            raise _denied()
        member = await db.scalar(
            select(TenantMembership).where(
                TenantMembership.user_id == identity.user_id,
                TenantMembership.tenant_id == identity.org_id,
                TenantMembership.revoked_at.is_(None),
            )
        )
        if member is None:
            raise _denied()
        items = []
        last_key = None
        while True:
            options = {"KeyConditionExpression": Key("event_id").eq(f"crew-model#{run_id}"), "ConsistentRead": True}
            if last_key:
                options["ExclusiveStartKey"] = last_key
            page = await asyncio.to_thread(self.table.query, **options)
            for row in page.get("Items", []):
                if row.get("tenant_id") != identity.org_id or (row.get("user_id") != identity.user_id and member.role not in ("owner", "admin")):
                    raise _denied()
                items.append(
                    {
                        "run_id": row["run_id"],
                        "model_id": row["model_id"],
                        "generation": row["machine_generation"],
                        "status": row["status"],
                        "usage": row.get("usage"),
                        "created_at": row["created_at"],
                    }
                )
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return items
