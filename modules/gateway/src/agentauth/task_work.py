"""Durable publication/recovery work records: where recoverability comes from.

## The problem

Accepting a task and publishing it to the queue cannot be one atomic operation:
one is a DynamoDB transaction, the other is an SQS send. Whatever order they run
in, the process can die in between. So the design does not try to make them
atomic -- it makes the *intent* durable first, and gives a scheduled sweep the
job of finishing whatever was left half-done.

A work record is that durable intent. It is committed inside the acceptance
transaction (T1 owns the transaction; this module owns the record's shape and its
lifecycle), which means: if a client ever received a 202, a work record exists,
and therefore the task will eventually be published or will fail visibly. There
is no third outcome where it silently disappears.

## Why a lease rather than a lock

Two publishers can reach the same record -- the scheduled sweep racing the
original request path, or two sweep invocations overlapping. A lock held across
the SQS send would have to survive a crash, which is the thing we cannot assume.
Instead a claim takes a short lease with a compare-and-swap on the record's
version: a second claimant is refused while the lease is live, and once the lease
expires the record becomes claimable again. Publishing under a lease that expired
mid-send is still safe, because the dispatch ID makes the send idempotent at the
queue and the conditional bootstrap makes execution single-owner regardless.

## Why "unknown" is a stored state and not an error

Publication outcome and `queue_ack_status` both carry `unknown`. If an SQS call
times out we genuinely do not know whether it took effect. Recording `failed`
would authorize a fresh dispatch of a message that may already be on the queue;
recording `confirmed` would strand a task whose message never arrived. Keeping
`unknown` means the next sweep re-sends the SAME dispatch envelope and ID, which
FIFO deduplication collapses if the first one did land.

## Two different refusals that look alike

A claim can be refused because the try budget for the current window is spent
(`throttled`) or because the task's deadline has passed (`exhausted`). They are
deliberately distinct. Throttled work is still going to be retried -- the design
allows 5 tries per 10-minute window and bounds the whole thing by the task
deadline, not by a total try count. Exhausted work never will be, so it is the
one that has to become a visible failure or recovery-required state for an
operator. Collapsing them would either give up on a task that had 9 minutes left
or retry a dead task forever.

## Layout

Records live in the existing request table alongside task data, per the design's
storage section:

    event_id   = TASK_WORK#<task_id>
    arrived_at = DISPATCH#<dispatch_id>   (one publication attempt series)
                 RECONCILE                (the per-task reconciliation record)

They carry the two sparse index attributes `task_work_shard` and `task_due` and
nothing else in the table does, so the recovery index holds only outstanding
work rather than 30 days of every delivery. The index can lag, which delays
discovery; it never authorizes a mutation, because a claim re-reads the primary
record consistently before touching it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import UTC, datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("bedrockgateway.agentauth.task_work")

WEBHOOK_EVENTS_TABLE_ENV = "WEBHOOK_EVENTS_TABLE"
_DEFAULT_WEBHOOK_EVENTS_TABLE = "adp-dev-webhook-events"

TASK_WORK_PREFIX = "TASK_WORK#"
DISPATCH_SORT_PREFIX = "DISPATCH#"
RECONCILE_SORT_KEY = "RECONCILE"

# The sparse recovery index (design section 6).
TASK_WORK_INDEX = "task-work-index"
SHARD_ATTRIBUTE = "task_work_shard"
DUE_ATTRIBUTE = "task_due"
SHARD_COUNT = 16
SHARD_VERSION = "v1"

# Fixed limits from contracts/v1/limits.json, restated as constants so a change
# is a visible diff against the accepted contract rather than a tuning knob.
WORK_LEASE_SECONDS = 45
MAX_WORK_RECORDS_PER_INVOCATION = 100
MAX_INVOCATION_SECONDS = 30
MAX_PUBLICATION_TRIES = 5
PUBLICATION_TRY_WINDOW_MINUTES = 10

DISPATCH_KIND = "dispatch"
QUEUE_ACK_KIND = "queue_ack"
WORK_KINDS = (DISPATCH_KIND, "execution", QUEUE_ACK_KIND, "cleanup")

PUBLICATION_OUTCOMES = ("confirmed", "unknown", "failed")
# `pending` is the initial stored value; it is not a settled outcome.
PENDING_OUTCOME = "pending"
SETTLING_OUTCOMES = ("confirmed", "failed")


class TaskWorkError(Exception):
    """A work-record operation was refused.

    Distinct from the store being unavailable: this is a normal, expected
    refusal (someone else holds the lease, the record is already settled, the
    try budget for this window is spent) and not an operational alarm.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TaskWorkUnavailableError(Exception):
    """The work store could not answer. Operational, not a refusal."""


def work_shard(task_id: str) -> str:
    """Stable shard for a task, so recovery can fan out without a table scan.

    Derived from the task ID rather than assigned round-robin or by arrival
    order: a record must land in the same shard on every write, or a sweep of
    shard N would miss work that moved to shard M and that work would never be
    discovered by anyone.
    """
    if not task_id:
        raise TaskWorkError("invalid_task_id")
    digest = hashlib.sha256(task_id.encode("utf-8")).digest()
    return f"{SHARD_VERSION}#{digest[0] % SHARD_COUNT:02d}"


def due_key(due_at: datetime, work_id: str) -> str:
    """Fixed-width epoch-millisecond sort key, suffixed with the work ID.

    Fixed width because DynamoDB sorts strings lexicographically and an unpadded
    number would order "9" after "10" -- putting later work at the head of the
    queue and starving earlier work. 13 digits covers epoch milliseconds past
    the year 2286. The work ID suffix keeps two records due in the same
    millisecond distinct instead of colliding.
    """
    millis = int(due_at.astimezone(UTC).timestamp() * 1000)
    return f"{millis:013d}#{work_id}"


def _due_upper_bound(now: datetime) -> str:
    """Inclusive upper bound for "due at or before now".

    The sort key is `<millis>#<work_id>`, so comparing against a bare
    `<millis>` would exclude every record due in that exact millisecond (their
    keys sort after it). The sentinel suffix is the highest code point, which
    sorts after any real work ID, making the bound inclusive of the whole
    millisecond.
    """
    millis = int(now.astimezone(UTC).timestamp() * 1000)
    return f"{millis:013d}#￿"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a stored timestamp, or None if it is absent or unreadable.

    Callers must treat None as "unknown", never as "zero": an unreadable
    deadline that defaulted to the epoch would make every record look expired.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def encode_cursor(key: dict) -> str:
    """Opaque continuation token for bounded pagination."""
    return base64.urlsafe_b64encode(json.dumps(key, sort_keys=True).encode()).decode()


def decode_cursor(cursor: str) -> dict:
    """Reject a malformed cursor rather than silently restarting the page.

    Treating an unreadable cursor as "start from the beginning" would make a
    corrupted token re-do work already done, which for publication means
    re-sending messages.
    """
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, TypeError, binascii.Error):
        raise TaskWorkError("invalid_cursor") from None
    if not isinstance(decoded, dict):
        raise TaskWorkError("invalid_cursor")
    return decoded


class TaskWorkStore:
    """Work-record reads, claims and settlements on the existing request table.

    Uses the low-level DynamoDB client with explicit type descriptors, matching
    ``agentauth/store.py``: the request shape -- condition expressions,
    ``ConsistentRead`` -- is exactly what the correctness tests assert, and the
    resource-level abstraction hides it.
    """

    def __init__(
        self,
        *,
        dynamodb_client,
        table_name: str | None = None,
        clock=time.time,
    ):
        self.table = table_name or os.environ.get(WEBHOOK_EVENTS_TABLE_ENV, _DEFAULT_WEBHOOK_EVENTS_TABLE)
        if not self.table:
            raise TaskWorkUnavailableError("work table is required")
        self.client = dynamodb_client
        self.clock = clock

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=UTC)

    def _key(self, task_id: str, sort_key: str) -> dict:
        return {
            "event_id": {"S": f"{TASK_WORK_PREFIX}{task_id}"},
            "arrived_at": {"S": sort_key},
        }

    def read(self, task_id: str, sort_key: str) -> dict | None:
        """Consistent read of one work record.

        Always consistent: the recovery index may lag, so a claim that trusted
        an index projection could act on a record another writer has already
        settled -- publishing a message for a task that is finished.
        """
        try:
            response = self.client.get_item(
                TableName=self.table,
                Key=self._key(task_id, sort_key),
                ConsistentRead=True,
            )
        except (ClientError, BotoCoreError):
            raise TaskWorkUnavailableError("work store unavailable") from None
        return response.get("Item")

    def put_work(
        self,
        *,
        task_id: str,
        kind: str,
        tenant_id: str,
        deadline_at: datetime,
        dispatch_id: str | None = None,
        envelope_digest: str | None = None,
        due_at: datetime | None = None,
    ) -> dict:
        """Create the durable intent, before the thing it describes is attempted.

        For dispatch work this ordering is the entire mechanism: a crash after
        this write leaves discoverable work, and a crash before it means no 202
        was returned, so the client's original idempotency key is still free to
        produce the same task rather than a second one.

        Conditional on the item's absence, so replaying the acceptance
        transaction cannot create a second attempt series for one dispatch.
        """
        if kind not in WORK_KINDS:
            raise TaskWorkError("invalid_work_kind")
        if kind == DISPATCH_KIND and not dispatch_id:
            raise TaskWorkError("dispatch_id_required")
        if not tenant_id:
            raise TaskWorkError("invalid_scope")

        now = self._now()
        sort_key = f"{DISPATCH_SORT_PREFIX}{dispatch_id}" if kind == DISPATCH_KIND else RECONCILE_SORT_KEY
        # The dispatch series is identified by its dispatch ID, so recovery
        # republishes under the same work identity it was recorded under.
        work_id = dispatch_id if kind == DISPATCH_KIND else f"{kind}#{task_id}"
        item = {
            **self._key(task_id, sort_key),
            "record_type": {"S": "TASK_WORK"},
            "schema_version": {"S": "1.0"},
            "work_id": {"S": work_id},
            "task_id": {"S": task_id},
            "kind": {"S": kind},
            # Nested scope metadata: the design keeps tenant_id out of the
            # top-level attributes so these rows stay invisible to the legacy
            # tenant-index GSI and cannot appear in Activity queries.
            "scope": {"M": {"tenant_id": {"S": tenant_id}}},
            "publication_outcome": {"S": PENDING_OUTCOME},
            "queue_ack_status": {"S": "pending"},
            "tries": {"N": "0"},
            "deadline_at": {"S": _iso(deadline_at)},
            "created_at": {"S": _iso(now)},
            "version": {"S": secrets.token_hex(16)},
            # Sparse index attributes. Only work records carry them.
            SHARD_ATTRIBUTE: {"S": work_shard(task_id)},
            DUE_ATTRIBUTE: {"S": due_key(due_at or now, work_id)},
        }
        if dispatch_id:
            item["dispatch_id"] = {"S": dispatch_id}
        if envelope_digest:
            item["envelope_digest"] = {"S": envelope_digest}

        try:
            self.client.put_item(
                TableName=self.table,
                Item=item,
                ConditionExpression=("attribute_not_exists(event_id) AND attribute_not_exists(arrived_at)"),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise TaskWorkError("already_exists") from None
            raise TaskWorkUnavailableError("work store unavailable") from None
        except BotoCoreError:
            raise TaskWorkUnavailableError("work store unavailable") from None
        return item

    def claim(self, *, task_id: str, sort_key: str) -> dict:
        """Take a short lease on a work record, or refuse with a reason.

        Compare-and-swap on ``version`` rather than a held lock, because a lock
        across the SQS send would have to survive a crash. A refused claim is
        normal: the other claimant is doing the work.

        Returns the claimed record including its fresh lease token. Settlement
        must present that exact token, so a claimant whose lease expired and was
        re-claimed by someone else cannot settle the newer claim's work with an
        observation about an older attempt.
        """
        record = self.read(task_id, sort_key)
        if record is None:
            raise TaskWorkError("not_found")
        if record.get("settled_at", {}).get("S"):
            raise TaskWorkError("already_settled")

        now = self._now()
        self._refuse_if_unclaimable(record, now=now)

        token = secrets.token_hex(16)
        expires = now + timedelta(seconds=WORK_LEASE_SECONDS)
        tries = _int_attribute(record, "tries")
        try:
            response = self.client.update_item(
                TableName=self.table,
                Key=self._key(task_id, sort_key),
                UpdateExpression=(
                    "SET lease_token = :token, lease_expires_at = :expires, "
                    "#version = :new_version, tries = :tries, "
                    "try_window_started_at = if_not_exists(try_window_started_at, :now)"
                ),
                # Both halves matter: the version CAS loses the race to a
                # concurrent claimant, and the settled_at guard loses to a
                # settlement that landed between the read above and here.
                ConditionExpression=("#version = :current_version AND attribute_not_exists(settled_at)"),
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues={
                    ":token": {"S": token},
                    ":expires": {"S": _iso(expires)},
                    ":new_version": {"S": secrets.token_hex(16)},
                    ":current_version": record["version"],
                    ":tries": {"N": str(tries + 1)},
                    ":now": {"S": _iso(now)},
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise TaskWorkError("lost_claim_race") from None
            raise TaskWorkUnavailableError("work store unavailable") from None
        except BotoCoreError:
            raise TaskWorkUnavailableError("work store unavailable") from None
        return response.get("Attributes", {})

    def _refuse_if_unclaimable(self, record: dict, *, now: datetime) -> None:
        """Deadline, live lease and try-budget checks, in that order.

        Deadline first: a task past its deadline is exhausted no matter how many
        tries remain, and reporting `throttled` for it would imply a retry that
        is never coming.
        """
        deadline = _parse_iso(record.get("deadline_at", {}).get("S"))
        if deadline is not None and now >= deadline:
            raise TaskWorkError("exhausted")

        lease_expiry = _parse_iso(record.get("lease_expires_at", {}).get("S"))
        if lease_expiry is not None and lease_expiry > now:
            raise TaskWorkError("leased")

        if record.get("kind", {}).get("S") != DISPATCH_KIND:
            return
        tries = _int_attribute(record, "tries")
        if tries < MAX_PUBLICATION_TRIES:
            return
        window_started = _parse_iso(record.get("try_window_started_at", {}).get("S"))
        if window_started is None:
            # Tries were counted but the window start is missing or unreadable.
            # Refuse rather than guess: assuming the window already elapsed
            # would hand out an unbounded try budget.
            raise TaskWorkError("throttled")
        if now < window_started + timedelta(minutes=PUBLICATION_TRY_WINDOW_MINUTES):
            raise TaskWorkError("throttled")

    def reset_try_window(self, *, task_id: str, sort_key: str) -> None:
        """Open a new try window for work whose previous window has elapsed.

        Kept explicit rather than folded into ``claim``: the sweep decides when a
        window rolls over, and a claim that silently reset the counter would make
        the 5-tries-per-10-minutes bound unobservable.
        """
        try:
            self.client.update_item(
                TableName=self.table,
                Key=self._key(task_id, sort_key),
                UpdateExpression=("SET tries = :zero, try_window_started_at = :now, #version = :new_version"),
                ConditionExpression="attribute_not_exists(settled_at)",
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues={
                    ":zero": {"N": "0"},
                    ":now": {"S": _iso(self._now())},
                    ":new_version": {"S": secrets.token_hex(16)},
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise TaskWorkError("already_settled") from None
            raise TaskWorkUnavailableError("work store unavailable") from None
        except BotoCoreError:
            raise TaskWorkUnavailableError("work store unavailable") from None

    def settle_publication(
        self,
        *,
        task_id: str,
        dispatch_id: str,
        lease_token: str,
        publication_outcome: str,
        sqs_message_id: str | None = None,
    ) -> dict:
        """Record what the publisher observed, under its own lease.

        ``confirmed`` requires a transport message ID, because "the call
        returned" is not the same as "the message is on the queue".

        ``unknown`` deliberately does NOT settle the record: it releases the
        lease and makes the work due again so the next sweep re-sends the same
        dispatch envelope and ID. That is what keeps an ambiguous send from
        becoming either a lost task or a second execution.
        """
        if publication_outcome not in PUBLICATION_OUTCOMES:
            raise TaskWorkError("invalid_outcome")
        if publication_outcome == "confirmed" and not sqs_message_id:
            raise TaskWorkError("confirmed_without_message_id")
        if not lease_token:
            raise TaskWorkError("lease_token_required")

        now = self._now()
        settling = publication_outcome in SETTLING_OUTCOMES
        set_clauses = [
            "publication_outcome = :outcome",
            "#version = :new_version",
            "observed_at = :now",
        ]
        remove_clauses = ["lease_token", "lease_expires_at"]
        values = {
            ":outcome": {"S": publication_outcome},
            ":token": {"S": lease_token},
            ":new_version": {"S": secrets.token_hex(16)},
            ":now": {"S": _iso(now)},
        }
        if sqs_message_id:
            # Stored for settlement and diagnostics only. It never enters the
            # envelope and never identifies the run (T3-AC02).
            set_clauses.append("sqs_message_id = :message_id")
            values[":message_id"] = {"S": sqs_message_id}
        if settling:
            set_clauses.append("settled_at = :now")
            if publication_outcome == "confirmed":
                set_clauses.append("queue_ack_status = :ack")
                values[":ack"] = {"S": "confirmed"}
            # Settled work must leave the sparse index, or every future sweep
            # rediscovers it and the index stops being sparse.
            remove_clauses.extend([SHARD_ATTRIBUTE, DUE_ATTRIBUTE])
        else:
            set_clauses.append(f"{DUE_ATTRIBUTE} = :due")
            values[":due"] = {"S": due_key(now, dispatch_id)}

        expression = f"SET {', '.join(set_clauses)} REMOVE {', '.join(remove_clauses)}"
        try:
            response = self.client.update_item(
                TableName=self.table,
                Key=self._key(task_id, f"{DISPATCH_SORT_PREFIX}{dispatch_id}"),
                UpdateExpression=expression,
                # The exact lease token, and still unsettled. A stale claimant
                # whose lease was re-claimed must not overwrite the new claim.
                ConditionExpression=("lease_token = :token AND attribute_not_exists(settled_at)"),
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise TaskWorkError("stale_lease") from None
            raise TaskWorkUnavailableError("work store unavailable") from None
        except BotoCoreError:
            raise TaskWorkUnavailableError("work store unavailable") from None
        logger.info(
            "task work publication settled task_id=%s dispatch_id=%s outcome=%s",
            task_id,
            dispatch_id,
            publication_outcome,
        )
        return response.get("Attributes", {})

    def due_work(
        self,
        *,
        shard: str,
        cursor: str | None = None,
        limit: int = MAX_WORK_RECORDS_PER_INVOCATION,
    ) -> tuple[list[dict], str | None]:
        """Query the sparse index for due work, in bounded pages.

        Bounded and paginated with a retained continuation, never a table scan:
        an unbounded sweep would cost in proportion to total task history rather
        than outstanding work, and would eventually exceed its own invocation
        budget and stop making progress at all.
        """
        if not shard:
            raise TaskWorkError("invalid_shard")
        limit = max(1, min(int(limit), MAX_WORK_RECORDS_PER_INVOCATION))
        arguments = {
            "TableName": self.table,
            "IndexName": TASK_WORK_INDEX,
            "KeyConditionExpression": (f"{SHARD_ATTRIBUTE} = :shard AND {DUE_ATTRIBUTE} <= :now"),
            "ExpressionAttributeValues": {
                ":shard": {"S": shard},
                ":now": {"S": _due_upper_bound(self._now())},
            },
            "Limit": limit,
        }
        if cursor:
            arguments["ExclusiveStartKey"] = decode_cursor(cursor)
        try:
            response = self.client.query(**arguments)
        except (ClientError, BotoCoreError):
            raise TaskWorkUnavailableError("work store unavailable") from None
        last = response.get("LastEvaluatedKey")
        return response.get("Items", []), encode_cursor(last) if last else None


def _int_attribute(record: dict, name: str) -> int:
    """Read a numeric attribute, tolerating absence but not nonsense."""
    raw = record.get(name, {}).get("N")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise TaskWorkError("corrupt_work_record") from None
