"""The protected authority store: DynamoDB backing for grants and execution state (#5028).

## The property that makes this a boundary

Everything in this table must be unwritable by the workers it constrains. That is
the one requirement `adp-*-webhook-events` fails: the shared worker role holds
`dynamodb:UpdateItem` on it with no key or attribute condition, so a record
stored there can be edited by the run it is supposed to describe.

The mechanism is a **separate table the worker role cannot address at all** —
`adp-<env>-agent-authority`, granted to the gateway/dispatch roles and named in no
worker statement. Chosen over per-attribute conditions on `webhook-events`
because attribute-level protection would rest on condition-key correctness on a
table the worker must keep writing to; absence of any grant does not. Decision 3
in ``docs/design/agent-delegated-authority.md`` records this, including why it
diverges from #3142.

## One table, three item kinds

Single-table because every authorization read wants the execution record and the
grant for the same tenant, and two tables would make that two round trips with no
isolation benefit — the protection is the *table-level* absence of worker access,
which one table provides as well as three.

| Item | PK | SK | Holds |
|---|---|---|---|
| Execution | ``TENANT#<tenant>`` | ``EXEC#<invocation_id>`` | attempt, status, credential epoch + overlap, workload binding, flow, repo |
| Grant | ``TENANT#<tenant>`` | ``GRANT#<principal>`` | delegated actions, targets, authority reference, revocation epoch |
| Reservation counter | ``TENANT#<tenant>`` | ``RESV#<grant_id>`` | in-flight dispatch count |

Partitioning by tenant keeps one tenant's authority reads off another's
partition, and makes every key this module builds contain a tenant that came from
a verified credential rather than from a request.

## Consistent reads are mandatory, not an optimization

Every read on the authorization path passes ``ConsistentRead=True``. DynamoDB's
default eventually-consistent read may serve a value from before a revocation —
and an authorization check that can read a stale revocation is an authorization
check that honours revoked authority for an unbounded window. The cost is
latency on a single-item read; the alternative is a revocation bound that cannot
be stated.

## Concurrency is a reservation, not a count

A dispatch limit implemented as "read the count, compare, dispatch" is a race:
two concurrent requests both read 2-of-3 and both dispatch, yielding 4. So
:meth:`AgentAuthorityStore.reserve_dispatch` performs a conditional atomic ``ADD``
that fails when the ceiling is already reached, and returns the reserved slot
number. The check and the increment are one operation, so the later spawn wiring
inherits a primitive that cannot be misused into a race.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.grants import (
    AgentAction,
    AuthorityReference,
    DelegatedGrant,
    GrantRefusedError,
    TargetRelationship,
)

logger = logging.getLogger("bedrockgateway.agentauth.store")

# Table name via env indirection so dev and prod cannot share an authority table
# by accident. The default is dev-shaped and deliberately not a production name.
AUTHORITY_TABLE_ENV = "AGENT_AUTHORITY_TABLE"
_DEFAULT_TABLE_NAME = "adp-dev-agent-authority"

# Key prefixes. Constants rather than inline f-strings so a reader can see that
# no key is ever built from unprefixed caller input, and so a typo in one call
# site cannot silently address a different item kind.
_TENANT_PREFIX = "TENANT#"
_EXEC_PREFIX = "EXEC#"
_GRANT_PREFIX = "GRANT#"
_RESERVATION_PREFIX = "RESV#"

MAX_EPOCH_OVERLAP_SECONDS = 30

# Reservation lifecycle. A released reservation is retained rather than deleted:
# the record is what makes a duplicate release a no-op, so deleting it on release
# would restore the double-decrement it exists to prevent.
_RESERVATION_HELD = "held"
_RESERVATION_RELEASED = "released"


class AuthorityStoreError(Exception):
    """The authority store could not answer.

    Raised instead of returning ``None`` on infrastructure failure, because the
    two must not be conflated: ``None`` means "no such grant" and is a normal
    refusal, while this means "the authority is unavailable". The policy fails
    closed on both, but only this one is an operational alarm.
    """


class AgentAuthorityStore:
    """Reads and writes the protected authority table.

    Implements the :class:`~src.agentauth.policy.GrantStore` protocol plus the
    execution-state and reservation operations. Injected into the policy, so the
    policy still names no table.

    Uses the low-level client with explicit type descriptors, matching
    ``src/admin/identity/user_identity_index.py``: the resource-level ``Table``
    abstraction hides the request shape, and the request shape (condition
    expressions, ``ConsistentRead``) is precisely what the tests here assert.
    """

    def __init__(self, *, table_name: str | None = None, dynamodb_client=None) -> None:
        self._table_name = table_name or os.environ.get(AUTHORITY_TABLE_ENV, _DEFAULT_TABLE_NAME)
        self._client = dynamodb_client or boto3.client(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )

    @property
    def table_name(self) -> str:
        return self._table_name

    # -- reads ------------------------------------------------------------

    def load_execution(self, *, invocation_id: str, tenant_id: str) -> ExecutionRecord | None:
        """Read authoritative execution state, or None if there is no such record.

        None is a real answer here and the policy fails closed on it — see
        :func:`~src.agentauth.execution.evaluate_execution_state`. This method
        does not itself decide; it only reports what the store holds.
        """
        if not invocation_id or not tenant_id:
            return None
        item = self._get(sort_key=f"{_EXEC_PREFIX}{invocation_id}", tenant_id=tenant_id)
        if item is None:
            return None
        return _deserialize_execution(item)

    def load_grant(self, *, principal: str, tenant_id: str) -> DelegatedGrant | None:
        """Read the grant issued to one execution identity (``<invocation>#<attempt>``).

        The principal comes from the verified credential. It is part of the sort
        key, so a caller cannot be served another principal's grant by a lookup
        it influenced — there is no query here that could match more than one
        item.
        """
        if not principal or not tenant_id:
            return None
        item = self._get(sort_key=f"{_GRANT_PREFIX}{principal}", tenant_id=tenant_id)
        if item is None:
            return None
        try:
            return _deserialize_grant(item)
        except (GrantRefusedError, ValueError, KeyError) as exc:
            # A stored grant that will not deserialize is treated as no grant.
            # Logged as a warning because it is a provisioning defect, but it must
            # not raise: a malformed record must refuse the request, not turn into
            # a 500 that tells the caller its target exists.
            logger.warning(
                "Discarding malformed delegated grant",
                extra={"principal": principal, "tenant_id": tenant_id, "reason": str(exc)},
            )
            return None

    def active_dispatch_count(self, *, grant_id: str, tenant_id: str) -> int:
        """In-flight dispatch count for a grant.

        Present because the :class:`GrantStore` protocol requires it. Prefer
        :meth:`reserve_dispatch`: this is a read, and a limit enforced by reading
        a count and then acting on it can be raced by a concurrent caller.

        Raises rather than returning 0 on missing arguments. 0 is the value that
        means "budget fully available", so returning it for "I could not build a
        key" would turn a bug into an unlimited dispatch allowance.
        """
        if not grant_id or not tenant_id:
            raise AuthorityStoreError("grant_id and tenant_id are required to read a dispatch count")
        item = self._get(sort_key=f"{_RESERVATION_PREFIX}{grant_id}", tenant_id=tenant_id)
        if item is None:
            return 0
        return int(item.get("in_flight", {}).get("N", "0"))

    def _get(self, *, sort_key: str, tenant_id: str) -> dict | None:
        """Single strongly-consistent item read."""
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key={
                    "pk": {"S": f"{_TENANT_PREFIX}{tenant_id}"},
                    "sk": {"S": sort_key},
                },
                # See the module docstring: an eventually-consistent authorization
                # read can serve a pre-revocation value.
                ConsistentRead=True,
            )
        except ClientError as exc:
            logger.error(
                "Authority store read failed",
                extra={"table": self._table_name, "sk": sort_key, "code": exc.response.get("Error", {}).get("Code")},
            )
            raise AuthorityStoreError("authority store unavailable") from exc
        return response.get("Item")

    # -- reservations -----------------------------------------------------

    def reserve_dispatch(
        self,
        *,
        grant_id: str,
        tenant_id: str,
        reservation_id: str,
        max_concurrency: int,
    ) -> int:
        """Atomically claim one *identified* dispatch slot. Returns the in-flight count.

        Raises :class:`DispatchLimitReachedError` when the ceiling is already taken,
        and :class:`DispatchReservationConflictError` when ``reservation_id`` has
        already claimed a slot.

        ``reservation_id`` is required, and identity is what makes the accounting
        correct rather than merely atomic. An earlier version of this method kept
        only a counter, which is atomic and still wrong: with no record of *who*
        holds each slot, a duplicate release decrements the shared counter and
        frees a slot another child is still occupying, so the ceiling silently
        admits an extra dispatch. Atomicity prevents two callers from both reading
        2-of-3; it does nothing about a release that returns a slot its caller
        never held.

        So a reservation is two items written in one transaction:

        - ``RESV#<grant_id>#<reservation_id>`` — the slot itself, guarded by
          ``attribute_not_exists(sk)``. A retried dispatch of the same unit of work
          cannot consume a second slot.
        - ``RESV#<grant_id>`` — the counter, guarded by the ceiling.

        Both conditions must hold or neither item is written, which is why this is
        a transaction rather than two updates: a counter incremented without its
        reservation record is a leaked slot that nothing can ever release, and a
        reservation record without the increment is an unbounded ceiling.

        The caller must derive ``reservation_id`` from the unit of work being
        dispatched (a message ID, or the child invocation ID), never generate a
        fresh random value per attempt — a new ID per retry is indistinguishable
        from new work and reintroduces exactly the over-dispatch this prevents.
        """
        if max_concurrency <= 0:
            raise DispatchLimitReachedError("grant conveys no dispatch budget")
        if not grant_id or not tenant_id or not reservation_id:
            raise AuthorityStoreError("grant_id, tenant_id and reservation_id are required to reserve dispatch")

        now = _iso(datetime.now(UTC))
        partition = {"S": f"{_TENANT_PREFIX}{tenant_id}"}
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": {
                                "pk": partition,
                                "sk": {"S": f"{_RESERVATION_PREFIX}{grant_id}#{reservation_id}"},
                                "grant_id": {"S": grant_id},
                                "reservation_id": {"S": reservation_id},
                                "state": {"S": _RESERVATION_HELD},
                                "created_at": {"S": now},
                                "updated_at": {"S": now},
                            },
                            # Idempotency: this reservation must not already exist,
                            # released or otherwise. A released record still blocks,
                            # because re-claiming a retired slot under the same ID
                            # would make the release/re-reserve pair unaccountable.
                            "ConditionExpression": "attribute_not_exists(sk)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": {"pk": partition, "sk": {"S": f"{_RESERVATION_PREFIX}{grant_id}"}},
                            "UpdateExpression": "ADD in_flight :one SET updated_at = :now",
                            # attribute_not_exists covers the first reservation for
                            # a grant, so no separate initialization write is needed.
                            "ConditionExpression": "attribute_not_exists(in_flight) OR in_flight < :ceiling",
                            "ExpressionAttributeValues": {
                                ":one": {"N": "1"},
                                ":ceiling": {"N": str(int(max_concurrency))},
                                ":now": {"S": now},
                            },
                        }
                    },
                ]
            )
        except ClientError as exc:
            self._raise_reservation_failure(exc, grant_id=grant_id, reservation_id=reservation_id)

        # Read back rather than using ReturnValues: TransactWriteItems does not
        # return updated attributes. The count is for observability and for the
        # fast-path check in the policy — it is NOT the enforcement point, which is
        # the ceiling condition above, so a value that has moved by the time it is
        # read costs nothing.
        return self.active_dispatch_count(grant_id=grant_id, tenant_id=tenant_id)

    def release_dispatch(self, *, grant_id: str, tenant_id: str, reservation_id: str) -> None:
        """Release the slot held by one identified reservation.

        Idempotent by state rather than by swallowing an error: the reservation
        record must currently be ``held`` for the counter to be decremented, and
        both happen in one transaction. A duplicate release therefore cancels the
        whole transaction and leaves the counter alone — which is the bug this
        replaces, where a second release for child A silently freed child B's slot
        and let the ceiling admit an extra dispatch.

        A release naming a reservation that was never made is also a no-op for the
        same reason, so a confused caller cannot manufacture budget.
        """
        if not grant_id or not tenant_id or not reservation_id:
            raise AuthorityStoreError("grant_id, tenant_id and reservation_id are required to release dispatch")

        now = _iso(datetime.now(UTC))
        partition = {"S": f"{_TENANT_PREFIX}{tenant_id}"}
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": {
                                "pk": partition,
                                "sk": {"S": f"{_RESERVATION_PREFIX}{grant_id}#{reservation_id}"},
                            },
                            "UpdateExpression": "SET #st = :released, updated_at = :now",
                            # Must exist AND still be held. The state check is what
                            # makes a duplicate release a no-op instead of a
                            # double decrement.
                            "ConditionExpression": "attribute_exists(sk) AND #st = :held",
                            "ExpressionAttributeNames": {"#st": "state"},
                            "ExpressionAttributeValues": {
                                ":released": {"S": _RESERVATION_RELEASED},
                                ":held": {"S": _RESERVATION_HELD},
                                ":now": {"S": now},
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": {"pk": partition, "sk": {"S": f"{_RESERVATION_PREFIX}{grant_id}"}},
                            "UpdateExpression": "ADD in_flight :minus SET updated_at = :now",
                            # Belt and braces: the held-state check above should
                            # already make this unreachable, but a counter driven
                            # negative would silently raise the effective ceiling,
                            # so it is guarded independently.
                            "ConditionExpression": "in_flight > :zero",
                            "ExpressionAttributeValues": {
                                ":minus": {"N": "-1"},
                                ":zero": {"N": "0"},
                                ":now": {"S": now},
                            },
                        }
                    },
                ]
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "TransactionCanceledException":
                # Cancellation also covers conflicts, throttling and validation
                # failures. Confirm persisted state before acknowledging cleanup:
                # a failed transaction does not establish that this slot is free.
                reservation = self._get(
                    sort_key=f"{_RESERVATION_PREFIX}{grant_id}#{reservation_id}",
                    tenant_id=tenant_id,
                )
                if reservation is None or (
                    reservation.get("state", {}).get("S") == _RESERVATION_RELEASED
                    and reservation.get("grant_id", {}).get("S") == grant_id
                    and reservation.get("reservation_id", {}).get("S") == reservation_id
                ):
                    logger.info(
                        "Confirmed dispatch reservation already released or absent",
                        extra={"grant_id": grant_id, "reservation_id": reservation_id},
                    )
                    return
                raise AuthorityStoreError("dispatch release not confirmed; reservation remains held or inconsistent") from exc
            raise AuthorityStoreError("authority store unavailable") from exc

    def _raise_reservation_failure(self, exc: ClientError, *, grant_id: str, reservation_id: str) -> None:
        """Translate a cancelled reservation transaction into the specific refusal.

        ``CancellationReasons`` is positional, matching the ``TransactItems`` order:
        index 0 is the reservation record, index 1 is the counter. Distinguishing
        them matters because they mean opposite things to a caller — a duplicate
        reservation is already-satisfied work, while a full ceiling is genuine
        backpressure that should be retried later.
        """
        code = exc.response.get("Error", {}).get("Code")
        if code == "TransactionCanceledException":
            reasons = _cancellation_reasons(exc)
            if reasons and reasons[0] == "ConditionalCheckFailed":
                raise DispatchReservationConflictError(f"reservation {reservation_id} has already claimed a dispatch slot") from exc
            if len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed":
                raise DispatchLimitReachedError("dispatch concurrency limit reached") from exc
            # Cancelled for something else entirely (throttling, capacity). Not a
            # limit: reporting it as one would tell the caller its budget is spent
            # when it is not, and silently shrink the effective ceiling.
            logger.error(
                "Dispatch reservation cancelled unexpectedly",
                extra={"grant_id": grant_id, "reservation_id": reservation_id, "reasons": reasons},
            )
            raise AuthorityStoreError("authority store unavailable") from exc

        logger.error(
            "Dispatch reservation failed",
            extra={"grant_id": grant_id, "reservation_id": reservation_id, "code": code},
        )
        raise AuthorityStoreError("authority store unavailable") from exc

    # -- trusted writes ---------------------------------------------------
    #
    # Called by trusted dispatch, never from the request path. They live here
    # rather than in a separate module because they must agree with the readers
    # above about the item shape, and a second module is how a writer and a reader
    # start disagreeing about a field name.

    def put_execution(self, *, record: ExecutionRecord, expect_absent: bool = True) -> None:
        """Write a new execution record at dispatch.

        ``expect_absent`` guards one-invocation-per-record: a second dispatch
        naming an existing invocation is refused rather than overwriting its
        attempt, status or workload binding. That overwrite is precisely how a
        fresh pod could supersede a live authorized attempt by naming its run.
        """
        item: dict[str, dict] = {
            "pk": {"S": f"{_TENANT_PREFIX}{record.tenant_id}"},
            "sk": {"S": f"{_EXEC_PREFIX}{record.invocation_id}"},
            "invocation_id": {"S": record.invocation_id},
            "tenant_id": {"S": record.tenant_id},
            "current_attempt": {"N": str(record.current_attempt)},
            "status": {"S": record.status.value},
            "current_credential_epoch": {"N": str(record.current_credential_epoch)},
            "min_acceptable_credential_epoch": {"N": str(record.min_acceptable_credential_epoch)},
        }
        if record.epoch_overlap_expires_at is not None:
            item["epoch_overlap_expires_at"] = {"S": _iso(record.epoch_overlap_expires_at)}
        if record.workload_binding:
            item["workload_binding"] = {"S": record.workload_binding}
        if record.flow_id:
            item["flow_id"] = {"S": record.flow_id}
        if record.repo:
            item["repo"] = {"S": record.repo}
        if record.arrived_at:
            item["arrived_at"] = {"S": record.arrived_at}
        if record.parent_principal:
            item["parent_principal"] = {"S": record.parent_principal}

        kwargs: dict = {"TableName": self._table_name, "Item": item}
        if expect_absent:
            kwargs["ConditionExpression"] = "attribute_not_exists(sk)"
        try:
            self._client.put_item(**kwargs)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ExecutionAlreadyExistsError(record.invocation_id) from exc
            raise AuthorityStoreError("authority store unavailable") from exc

    def record_abort_intent(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        attempt: int,
        command_id: str,
        body_digest: str,
        now: datetime | None = None,
    ) -> dict[str, str]:
        """Durably record that an authorized abort was accepted for this attempt (#3963).

        This is the fact the whole graceful-abort story rests on, so it is worth
        being precise about what it is and what it deliberately is *not*.

        **It is intent, not a terminal outcome.** The run is still executing when
        this lands — the operator's abort has been authorized and accepted, but
        cancellation and quiescence have not happened yet. Reporting ``aborted``
        here would claim an outcome that has not occurred. The terminal status is
        written later, by the finalizing supervisor, after the run has actually
        stopped.

        **It must not disturb ``status``.** The obvious implementation — flip the
        execution to ``CANCELLED`` — is wrong and is the reason this is a separate
        marker rather than a lifecycle transition. ``evaluate_execution_state``
        authorizes only an ``ACTIVE`` record, so cancelling here would immediately
        invalidate the very run credential the worker needs to deliver the abort to
        its listener, apply it, and finalize the outcome. The operator would be left
        with an accepted command that can never reach the still-running task and a
        run that reports nothing. So the marker sits *beside* the status: the
        execution stays ``ACTIVE`` and keeps its channel until it genuinely ends.

        **Why this table.** The marker has to be trustworthy against the worker it
        constrains, and this table is the only one the worker role cannot address
        (see the module docstring). A copy on ``webhook-events`` would be editable
        by the run it describes, which is exactly the property an abort record
        cannot have.

        Idempotent by ``command_id``: a retried acceptance of the same command
        re-reads and returns the stored marker rather than overwriting its
        timestamp, so the recorded moment stays the first acceptance. A *different*
        command_id does not overwrite either — the first accepted abort is the one
        that stopped the run, and a later one cannot rewrite which.

        Returns the effective marker (``command_id``, ``body_digest``,
        ``requested_at``), which is what the caller signs into a receipt. It is
        returned from the store rather than echoed from the arguments so the receipt
        attests what is actually durable, not what the caller hoped to write.
        """
        if (
            type(attempt) is not int
            or attempt < 1
            or not all(isinstance(value, str) and value for value in (invocation_id, tenant_id, command_id, body_digest))
        ):
            raise AuthorityStoreError("abort intent requires a complete accepted command")
        requested_at = _iso(now or datetime.now(UTC))
        key = {
            "pk": {"S": f"{_TENANT_PREFIX}{tenant_id}"},
            "sk": {"S": f"{_EXEC_PREFIX}{invocation_id}"},
        }
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key=key,
                UpdateExpression=(
                    "SET abort_command_id = :command, abort_body_digest = :digest, "
                    "abort_requested_at = :now, abort_requested_attempt = :attempt"
                ),
                # Only for the attempt that is actually running, and only once. The
                # attempt check stops a stale acceptance from marking a newer attempt
                # it never authorized; `attribute_not_exists` makes the first accepted
                # abort the durable one.
                ConditionExpression=(
                    "current_attempt = :attempt AND attribute_not_exists(abort_command_id)"
                ),
                ExpressionAttributeValues={
                    ":command": {"S": command_id},
                    ":digest": {"S": body_digest},
                    ":now": {"S": requested_at},
                    ":attempt": {"N": str(attempt)},
                },
            )
            return {"command_id": command_id, "body_digest": body_digest, "requested_at": requested_at}
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise AuthorityStoreError("authority store unavailable") from exc
        except BotoCoreError as exc:
            raise AuthorityStoreError("authority store unavailable") from exc

        # The write was refused. Either this attempt already has an abort marker
        # (the retry case, which must return the stored one) or the attempt moved
        # on (which must refuse rather than report an abort of something else).
        existing = self._get(sort_key=f"{_EXEC_PREFIX}{invocation_id}", tenant_id=tenant_id)
        if (
            not existing
            or existing.get("current_attempt") != {"N": str(attempt)}
            or "abort_command_id" not in existing
            or existing.get("abort_command_id") != {"S": command_id}
            or existing.get("abort_body_digest") != {"S": body_digest}
        ):
            raise AbortIntentConflictError(invocation_id)
        stored_at = existing.get("abort_requested_at", {}).get("S")
        if not stored_at:
            raise AbortIntentConflictError(invocation_id)
        return {"command_id": command_id, "body_digest": body_digest, "requested_at": stored_at}

    def abort_intent(self, *, invocation_id: str, tenant_id: str) -> dict[str, str] | None:
        """Read this run's durable abort marker, if it has one (#3963).

        Used by admission: a run an operator stopped must not be started again, and
        this is the fact that says so independently of whether the aborted run
        managed to write a terminal status or acknowledge its queue message. Those
        are exactly the writes that can fail, which is why admission cannot depend
        on them.
        """
        record = self._get(sort_key=f"{_EXEC_PREFIX}{invocation_id}", tenant_id=tenant_id)
        if not record:
            return None
        command_id = record.get("abort_command_id", {}).get("S")
        requested_at = record.get("abort_requested_at", {}).get("S")
        if not command_id or not requested_at:
            return None
        return {
            "command_id": command_id,
            "body_digest": record.get("abort_body_digest", {}).get("S", ""),
            "requested_at": requested_at,
            "attempt": record.get("abort_requested_attempt", {}).get("N", ""),
        }

    def rotate_credential_epoch(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        expected_epoch: int,
        expected_attempt: int,
        workload_binding: str,
        overlap_seconds: int,
        now: datetime | None = None,
    ) -> int:
        """Advance the credential epoch, opening a bounded overlap window.

        The building block for renewal (AC6). It deliberately touches **only**
        epoch fields: attempt, status and workload binding are untouched, so a
        renewal cannot reset the attempt the command journal and control
        generation are keyed to. The `ADP` control generation lives in a different
        table entirely and is not addressed here at all.

        Conditioned on the verified attempt and workload as well as the epoch:
        a replacement attempt may reuse an epoch number. An old renewal racing
        replacement must not rotate that replacement's credential.

        Conditioned on ``expected_epoch`` so two concurrent renewals cannot both
        advance the epoch — the loser is refused and retries against the new
        value rather than skipping an epoch. Also conditioned on the execution
        still being ACTIVE, so a cancelled or revoked execution cannot renew.
        Live grant/flow validation and lost-response recovery must be integrated
        by the renewal service before this primitive is exposed. Overlap is
        capped at 30 seconds; it never changes attempt or control generation.

        Renewal *plumbing* — a caller that mints and delivers the new credential —
        is not implemented; this is the state transition it will need.
        """
        if type(expected_attempt) is not int or expected_attempt < 1 or type(expected_epoch) is not int or expected_epoch < 1 or not workload_binding:
            raise EpochRotationConflictError("verified attempt and workload are required")
        current = now or datetime.now(UTC)
        overlap_until = current + timedelta(seconds=max(0, min(MAX_EPOCH_OVERLAP_SECONDS, int(overlap_seconds))))
        try:
            response = self._client.update_item(
                TableName=self._table_name,
                Key={
                    "pk": {"S": f"{_TENANT_PREFIX}{tenant_id}"},
                    "sk": {"S": f"{_EXEC_PREFIX}{invocation_id}"},
                },
                UpdateExpression=(
                    "SET current_credential_epoch = :next, "
                    "min_acceptable_credential_epoch = :floor, "
                    "epoch_overlap_expires_at = :overlap, "
                    "updated_at = :now"
                ),
                ConditionExpression=(
                    "current_credential_epoch = :expected AND #st = :active AND current_attempt = :attempt AND workload_binding = :binding"
                ),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":next": {"N": str(expected_epoch + 1)},
                    # The floor stays at the outgoing epoch for the overlap window,
                    # which is what lets an in-flight request with the old
                    # credential finish instead of failing mid-task.
                    ":floor": {"N": str(expected_epoch)},
                    ":overlap": {"S": _iso(overlap_until)},
                    ":expected": {"N": str(expected_epoch)},
                    ":attempt": {"N": str(expected_attempt)},
                    ":binding": {"S": workload_binding},
                    ":active": {"S": ExecutionStatus.ACTIVE.value},
                    ":now": {"S": _iso(current)},
                },
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise EpochRotationConflictError(invocation_id) from exc
            raise AuthorityStoreError("authority store unavailable") from exc
        return int(response.get("Attributes", {}).get("current_credential_epoch", {}).get("N", "0"))

    def set_execution_status(
        self,
        *,
        invocation_id: str,
        tenant_id: str,
        status: ExecutionStatus,
        expected_attempt: int,
        expected_status: ExecutionStatus,
    ) -> None:
        """Record a lifecycle transition (activation, completion, cancellation).

        Compare the attempt and source state at the write. A delayed activation
        cannot resurrect cancellation, and an old worker cannot complete a new
        attempt. Repeating the same transition on the same attempt is harmless.
        Activation also requires a workload binding established by bootstrap.
        """
        transitions = {
            ExecutionStatus.PENDING: {ExecutionStatus.ACTIVE, ExecutionStatus.CANCELLED, ExecutionStatus.REVOKED},
            ExecutionStatus.ACTIVE: {ExecutionStatus.COMPLETED, ExecutionStatus.CANCELLED, ExecutionStatus.REVOKED},
        }
        if (
            type(expected_attempt) is not int
            or expected_attempt < 1
            or not isinstance(status, ExecutionStatus)
            or not isinstance(expected_status, ExecutionStatus)
            or (status != expected_status and status not in transitions.get(expected_status, set()))
            or status is ExecutionStatus.PENDING
        ):
            raise ExecutionTransitionConflictError("invalid execution transition")
        condition = "current_attempt = :attempt AND (#st = :expected OR #st = :status)"
        values = {
            ":status": {"S": status.value},
            ":expected": {"S": expected_status.value},
            ":attempt": {"N": str(expected_attempt)},
            ":now": {"S": _iso(datetime.now(UTC))},
        }
        if status is ExecutionStatus.ACTIVE:
            condition += " AND attribute_exists(workload_binding) AND workload_binding <> :empty"
            values[":empty"] = {"S": ""}
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={
                    "pk": {"S": f"{_TENANT_PREFIX}{tenant_id}"},
                    "sk": {"S": f"{_EXEC_PREFIX}{invocation_id}"},
                },
                UpdateExpression="SET #st = :status, updated_at = :now",
                ConditionExpression=condition,
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ExecutionTransitionConflictError(invocation_id) from exc
            raise AuthorityStoreError("authority store unavailable") from exc

    def revoke_grant(self, *, principal: str, tenant_id: str) -> None:
        """Revoke a grant and bump its epoch so queued actions revalidate false.

        Both in one update: setting ``revoked`` without advancing the epoch would
        leave an already-queued action's epoch check passing, and advancing the
        epoch without ``revoked`` would let a fresh request still authorize.
        """
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={
                    "pk": {"S": f"{_TENANT_PREFIX}{tenant_id}"},
                    "sk": {"S": f"{_GRANT_PREFIX}{principal}"},
                },
                UpdateExpression="SET revoked = :true, updated_at = :now ADD revocation_epoch :one",
                ConditionExpression="attribute_exists(sk)",
                ExpressionAttributeValues={
                    ":true": {"BOOL": True},
                    ":one": {"N": "1"},
                    ":now": {"S": _iso(datetime.now(UTC))},
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise GrantNotFoundError(principal) from exc
            raise AuthorityStoreError("authority store unavailable") from exc


class DispatchLimitReachedError(Exception):
    """The grant's dispatch concurrency ceiling is already taken."""


class DispatchReservationConflictError(Exception):
    """This reservation ID has already claimed a dispatch slot.

    Distinct from :class:`DispatchLimitReachedError` because the two call for
    opposite responses: a limit is backpressure to retry later, while a duplicate
    reservation means the work was already admitted and retrying is wrong.
    """


class ExecutionAlreadyExistsError(Exception):
    """An execution record already exists for this invocation."""


class ExecutionTransitionConflictError(Exception):
    """Execution is missing, superseded or no longer in the expected state."""


class EpochRotationConflictError(Exception):
    """The credential epoch moved, or the execution is no longer active."""


class AbortIntentConflictError(Exception):
    """Abort intent could not be established for the attempt it was accepted for.

    Raised rather than returning ``None`` because the caller must not report an
    accepted abort it could not make durable (#3963): a silent failure here would
    let a run be told its abort was applied while nothing prevents it running
    again.
    """


class GrantNotFoundError(Exception):
    """No grant exists for this principal."""


# -- deserialization ------------------------------------------------------
#
# Explicit per-field, not a generic descriptor walk. A generic deserializer would
# happily turn an attacker-shaped item into an object with surprising types; here
# a field that is not the expected type is a refusal.


def _deserialize_execution(item: dict) -> ExecutionRecord:
    raw_status = item.get("status", {}).get("S", "")
    try:
        status = ExecutionStatus(raw_status)
    except ValueError:
        # An unrecognized status is not treated as active. Refusing an unknown
        # value is the fail-closed reading; mapping it to ACTIVE would let a
        # future status (or a corrupted field) authorize work.
        logger.warning("Unknown execution status in authority store", extra={"status": raw_status})
        status = ExecutionStatus.CANCELLED

    overlap_raw = item.get("epoch_overlap_expires_at", {}).get("S")
    current_epoch = _required_positive_int(item, "current_credential_epoch")
    # Defaulting the floor to the current epoch, not to 1: a record missing the
    # floor must not accept every historical epoch.
    floor = _int_field(item, "min_acceptable_credential_epoch", default=current_epoch)
    if not 1 <= floor <= current_epoch:
        raise AuthorityStoreError("invalid execution credential epoch floor")

    overlap_expires = _parse_iso(overlap_raw) if overlap_raw else None
    if overlap_expires is None:
        # The record claims an overlap window but its deadline is unreadable, so
        # there is no deadline to enforce. An unbounded overlap is exactly what
        # the explicit-state design exists to prevent, so the window is treated as
        # already closed: only the current epoch is accepted.
        if floor != current_epoch:
            logger.warning(
                "Closing credential epoch overlap without a valid deadline",
                extra={"invocation_id": item.get("invocation_id", {}).get("S", "")},
            )
        floor = current_epoch

    return ExecutionRecord(
        invocation_id=item.get("invocation_id", {}).get("S", ""),
        tenant_id=item.get("tenant_id", {}).get("S", ""),
        current_attempt=_required_positive_int(item, "current_attempt"),
        status=status,
        current_credential_epoch=current_epoch,
        min_acceptable_credential_epoch=floor,
        epoch_overlap_expires_at=overlap_expires,
        workload_binding=item.get("workload_binding", {}).get("S") or None,
        flow_id=item.get("flow_id", {}).get("S") or None,
        repo=item.get("repo", {}).get("S") or None,
        arrived_at=item.get("arrived_at", {}).get("S") or None,
        parent_principal=item.get("parent_principal", {}).get("S") or None,
    )


def _deserialize_grant(item: dict) -> DelegatedGrant:
    authority = AuthorityReference(
        kind=item.get("authority_kind", {}).get("S", ""),
        reference_id=item.get("authority_reference_id", {}).get("S", ""),
        human_id=item.get("authority_human_id", {}).get("S", ""),
        org_id=item.get("authority_org_id", {}).get("S", ""),
    )
    expires_raw = item.get("expires_at", {}).get("S")
    expires_at = _parse_iso(expires_raw) if expires_raw else None
    if expires_raw and expires_at is None:
        # A stored grant with an unparseable expiry would otherwise deserialize to
        # ``expires_at=None``, which ``DelegatedGrant.is_live`` reads as "never
        # expires" — a corrupted field would *widen* the grant. Raising sends it
        # down ``load_grant``'s malformed-record path, which refuses.
        raise ValueError(f"unparseable grant expiry: {expires_raw!r}")

    return DelegatedGrant(
        grant_id=item.get("grant_id", {}).get("S", ""),
        tenant_id=item.get("tenant_id", {}).get("S", ""),
        principal=item.get("principal", {}).get("S", ""),
        authority=authority,
        allowed_actions=_action_set(item, "allowed_actions"),
        target_run_ids=frozenset(_string_set(item, "target_run_ids")),
        target_relationships=_relationship_set(item, "target_relationships"),
        flow_id=item.get("flow_id", {}).get("S") or None,
        repo_scope=frozenset(_string_set(item, "repo_scope")),
        expires_at=expires_at,
        revocation_epoch=_int_field(item, "revocation_epoch", default=1),
        revoked=bool(item.get("revoked", {}).get("BOOL", False)),
        max_dispatch_concurrency=_int_field(item, "max_dispatch_concurrency", default=0),
        max_chain_depth=_int_field(item, "max_chain_depth", default=0),
        delegable_actions=_action_set(item, "delegable_actions"),
    )


def _int_field(item: dict, name: str, *, default: int) -> int:
    raw = item.get(name, {}).get("N")
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _required_positive_int(item: dict, name: str) -> int:
    """Missing or corrupt versions must not restore first-attempt authority."""
    value = _int_field(item, name, default=0)
    if value < 1:
        raise AuthorityStoreError("missing or invalid execution version")
    return value


def _string_set(item: dict, name: str) -> set[str]:
    """Read a DynamoDB string set, tolerating its absence.

    DynamoDB cannot store an empty set, so an absent attribute is the encoding of
    "empty" and must not be an error.
    """
    attribute = item.get(name, {})
    if "SS" in attribute:
        return {value for value in attribute["SS"] if isinstance(value, str)}
    if "L" in attribute:
        return {entry.get("S", "") for entry in attribute["L"] if entry.get("S")}
    return set()


def _action_set(item: dict, name: str) -> frozenset[AgentAction]:
    """Parse actions, dropping values this build does not recognize.

    Dropping rather than raising: an unknown action name in a stored grant is
    authority this deployment cannot evaluate, and the safe reading of
    "permission I do not understand" is "permission I do not have". Raising would
    turn a forward-compatible grant into a total refusal for actions that *are*
    understood.
    """
    parsed = set()
    for value in _string_set(item, name):
        try:
            parsed.add(AgentAction(value))
        except ValueError:
            logger.warning("Ignoring unknown action in stored grant", extra={"action": value, "field": name})
    return frozenset(parsed)


def _relationship_set(item: dict, name: str) -> frozenset[TargetRelationship]:
    parsed = set()
    for value in _string_set(item, name):
        try:
            parsed.add(TargetRelationship(value))
        except ValueError:
            logger.warning("Ignoring unknown target relationship in stored grant", extra={"relationship": value})
    return frozenset(parsed)


def _cancellation_reasons(exc: ClientError) -> list[str]:
    """Per-item cancellation codes from a TransactWriteItems failure, in item order.

    Positional and parallel to ``TransactItems``: ``["None", "ConditionalCheckFailed"]``
    means the second item's condition is what cancelled the transaction. Tolerant
    of a missing or malformed key, because a caller that cannot read the reasons
    must still fail closed rather than raise a second error while handling the
    first.
    """
    reasons = exc.response.get("CancellationReasons")
    if not isinstance(reasons, list):
        return []
    return [str(reason.get("Code", "")) if isinstance(reason, dict) else "" for reason in reasons]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        # Returns None, which means "no timestamp" — and "no timestamp" is the
        # permissive reading for both callers (no expiry, no overlap deadline). So
        # each caller must handle None explicitly rather than pass it through:
        # `_deserialize_grant` raises, `_deserialize_execution` closes the overlap.
        logger.warning("Unparseable timestamp in authority store", extra={"value": value})
        return None
