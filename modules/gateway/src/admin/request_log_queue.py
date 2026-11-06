"""Bounded, replayable request analytics on the existing Redis connection.

Administrative AuditLog writes keep their existing transactional path. RequestLog
records are acknowledged only after their PostgreSQL batch commits. A crashed
consumer's pending entries are reclaimed; stable row IDs make replay idempotent.
Redis failure/fullness falls back to the existing synchronous writer, never a
fire-and-forget task or a silently trimmed stream.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from contextlib import suppress
from datetime import datetime

from prometheus_client import Counter, Histogram
from redis.exceptions import ResponseError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import make_url

from src.admin.models import RequestLog
from src.shared.database import get_session_factory
from src.shared.redis_client import create_redis_client

logger = logging.getLogger(__name__)
GROUP = "request-log-writers-v1"
CAPACITY = 10_000
BATCH_SIZE = 100
MAX_RECORD_BYTES = 16_384
ENQUEUE_SECONDS = 0.25
WRITE_SECONDS = 5
RECLAIM_MS = 30_000
EVENTS = Counter("bedrock_gateway_request_log_queue_total", "Request analytics queue outcomes", ["outcome"])
BATCH_RECORDS = Histogram("bedrock_gateway_request_log_batch_records", "Committed request analytics batch size", buckets=(1, 10, 25, 50, 100))
_ENQUEUE = """
if redis.call('XLEN', KEYS[1]) >= tonumber(ARGV[1]) then return false end
return redis.call('XADD', KEYS[1], '*', 'record', ARGV[2])
"""


class RequestLogQueue:
    def __init__(self, client, namespace: str, *, factory=None):
        self.client = client
        identity = hashlib.sha256(namespace.encode()).hexdigest()[:24]
        self.key = f"request-logs:{{{identity}}}:v1"
        self.consumer = uuid.uuid4().hex
        self.factory = factory or get_session_factory
        self.task = None
        self.closing = False
        self.next_reclaim = 0.0
        self.reclaim_cursor = "0-0"
        self.pending = []

    async def enqueue(self, record: dict) -> bool:
        if self.closing:
            EVENTS.labels("closing").inc()
            return False
        row = {**record, "timestamp": record["timestamp"].isoformat()}
        raw = json.dumps(row, separators=(",", ":"))
        if len(raw.encode()) > MAX_RECORD_BYTES:
            EVENTS.labels("oversize").inc()
            return False
        try:
            async with asyncio.timeout(ENQUEUE_SECONDS):
                accepted = bool(await self.client.eval(_ENQUEUE, 1, self.key, CAPACITY, raw))
                EVENTS.labels("enqueued" if accepted else "full").inc()
                return accepted
        except Exception:
            EVENTS.labels("unavailable").inc()
            logger.warning("Request log queue unavailable; using synchronous persistence")
            return False

    async def _group(self):
        try:
            await self.client.xgroup_create(self.key, GROUP, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def flush(self) -> int:
        """Commit one bounded batch. Failed or cancelled batches remain pending."""
        await self._group()
        # Retry our own pending batch before taking new work.
        entries = self.pending
        if not entries and time.monotonic() >= self.next_reclaim:
            claimed = await self.client.xautoclaim(self.key, GROUP, self.consumer, RECLAIM_MS, self.reclaim_cursor, count=BATCH_SIZE)
            self.reclaim_cursor, entries = claimed[:2]
            self.next_reclaim = time.monotonic() + 5
        if not entries:
            result = await self.client.xreadgroup(GROUP, self.consumer, {self.key: ">"}, count=BATCH_SIZE)
            entries = result[0][1] if result else []
        if not entries:
            return 0
        self.pending = entries
        records = []
        ids = []
        for entry_id, fields in entries:
            record = json.loads(fields.get("record", fields.get(b"record")))
            record["timestamp"] = datetime.fromisoformat(record["timestamp"])
            records.append(record)
            ids.append(entry_id)
        async with asyncio.timeout(WRITE_SECONDS), self.factory()() as session:
            insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
            await session.execute(insert(RequestLog).values(records).on_conflict_do_nothing(index_elements=[RequestLog.id]))
            await session.commit()
        # Both commands target the same key, including on Redis Cluster. An
        # ambiguous commit/ack is safe to retry because the row IDs are stable.
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.xack(self.key, GROUP, *ids)
            pipe.xdel(self.key, *ids)
            await pipe.execute()
        self.pending = []
        EVENTS.labels("acknowledged").inc(len(records))
        BATCH_RECORDS.observe(len(records))
        return len(records)

    async def run(self):
        while not self.closing:
            try:
                count = await self.flush()
            except Exception:
                EVENTS.labels("batch_deferred").inc()
                logger.warning("Request log batch deferred; records remain queued", exc_info=True)
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(0 if count == BATCH_SIZE else 0.25)

    def start(self):
        self.task = asyncio.create_task(self.run(), name="request_log_writer")

    async def close(self):
        self.closing = True
        if self.task is not None:
            # Cancellation rolls back an unfinished DB transaction. Redis keeps
            # the pending records for another worker; no shutdown data discard.
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        await self.client.aclose()


def create_request_log_queue(settings) -> RequestLogQueue | None:
    if not settings.request_log_queue_enabled or not settings.redis_url:
        return None
    url = make_url(settings.database_url)
    namespace = f"{settings.rds_host}:{settings.rds_port}/{settings.rds_dbname}" if settings.rds_host else f"{url.host}:{url.port}/{url.database}"
    client = create_redis_client(settings.redis_url, decode_responses=True, socket_timeout=0.2, socket_connect_timeout=0.2, max_connections=32)
    return RequestLogQueue(client, namespace)
