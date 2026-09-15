"""Authenticated own-run status and control registration writes (#5028, AC4).

## The permission this exists to remove

``lib/invocation_status.py`` has three writers — ``update_status``,
``register_control_endpoint``, ``clear_control_endpoint`` — and each one calls
``dynamodb:UpdateItem`` straight at ``adp-*-webhook-events`` under the worker
role's ``DynamoDBWebhookEventsUpdate`` statement. That statement carries **no key
condition and no attribute condition**, and the row key ``(event_id,
arrived_at)`` is supplied by the caller from its own environment.

Every agent worker on the platform assumes that one role. So nothing in AWS
distinguishes worker A writing its own row from worker A writing worker B's —
including B's ``control_address`` and ``control_token``, which together decide
where B's control commands are delivered and what authenticates them. A worker
following malicious repository instructions can therefore redirect another
tenant's control channel at an address it chooses. That is AC4.

The fix is not "condition the IAM statement". It is to stop the worker writing
this table at all and route the writes through a service that derives the key
from state the worker cannot write. This module is that service.

## What the caller may and may not supply

The caller supplies **field values only**. Everything that decides *which row is
written* comes from elsewhere:

| Fact | Where it comes from | Why not the request |
|---|---|---|
| invocation / attempt | the HMAC-verified run credential | a body field is a self-assertion; ``ADP_MESSAGE_ID`` is worker-writable |
| workload identity | Kubernetes TokenReview (pod UID) | the pod cannot mint another pod's projected token |
| tenant | the protected execution record | a request tenant would let a caller write across tenants |
| ``arrived_at`` (sort key) | the protected execution record | see "the exact-row problem" below |
| control address | the *verified pod's* IP | a caller-chosen address is the redirect above |
| control port | pinned gateway configuration | a row-supplied port can aim the gateway at the kubelet |

Unknown and identity-shaped fields are **rejected, not dropped**. A silently
ignored ``tenant_id`` teaches a caller that the field was accepted, and the next
reader of this code may wire it through.

## The exact-row problem

``event_id`` alone does not identify a row; the table's key is ``(event_id,
arrived_at)``. Deriving the sort key by querying for the newest row means a
second row under the same ``event_id`` decides which row is written or read. The
protected execution record holds the authoritative ``arrived_at`` captured at
trusted dispatch, so every access here is a point operation on a key the caller
did not influence.

## Registration idempotency, and why it is not just a retry

``register_control_endpoint`` assigns the control generation with an atomic
``ADD``. A lost HTTP response naively retried would therefore increment it a
second time — and the listener would still be running the *first* generation, so
it would refuse every command the gateway subsequently sent. That failure is
indistinguishable from an attack, and it is caused purely by a dropped response.

The counter and a registration ledger under ``REG#<invocation>#<attempt>`` are
committed in one transaction with current execution, grant and authority checks.
A retry presenting the same token and expiry returns the
**same** generation without touching the counter. A retry presenting a
*different* token on the same attempt is refused: it would be a second listener
identity for one attempt, and issuing it a higher generation would silently
orphan the listener already running.

## What this module does not do

These writes address only the verified caller's own execution. Registration and
nonterminal updates require its live grant and human authority. Completion and
cleanup require the same current attempt, credential epoch and pod binding, but
may remove access after delegation has ended. Completion atomically records the
terminal outcome and releases the exact child reservation once.

It also does not renew a listener control token. A registration is not a renewal:
the listener caches its token and expiry in process, so writing a later expiry to
DynamoDB changes nothing the listener reads. That remaining AC6 fragment is
tracked in the issue, not quietly approximated here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.bootstrap import BootstrapRefusedError, BootstrapStore, _key
from src.agentauth.execution import ExecutionRecord, ExecutionStateError, ExecutionStatus, evaluate_execution_state
from src.agentauth.policy import AgentAuthorizationService, PolicyError
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import VerifiedPod

logger = logging.getLogger("bedrockgateway.agentauth.registration")

# Status values a worker may report for its own run. An allowlist rather than a
# free string: ``status`` drives what Activity displays and what the gateway
# treats as terminal, so an arbitrary value could make a live run look finished
# (and therefore uncontrollable) or a finished one look live.
ALLOWED_STATUSES: frozenset[str] = frozenset(
    {
        "in_progress",
        "complete",
        "failed",
        "skipped",
        "budget_stopped",
    }
)

# Free-text fields a worker may set on its own row, with the bound each is
# truncated to. Mirrors ``_MAX_ERROR_MESSAGE_CHARS`` in the worker helper: an
# unbounded string could push the item at the 400KB item limit and fail the whole
# update, losing the status transition too.
ALLOWED_STATUS_FIELDS: dict[str, int] = {
    "run_id": 256,
    "summary": 4096,
    "transcript_key": 1024,
    "session_id": 256,
    "token_mode": 32,
    "error_message": 1024,
    "skip_reason": 1024,
    "stop_reason": 1024,
}

# Fields that are never writable through this path even though they live on the
# same row. Named explicitly so a request carrying one is *refused* rather than
# filtered: these are the authority and routing fields the whole module exists to
# protect, and a caller that can send them without error will eventually be
# believed by some future reader of the request model.
REJECTED_FIELDS: frozenset[str] = frozenset(
    {
        "event_id",
        "arrived_at",
        "tenant_id",
        "org_id",
        "owner",
        "user_id",
        "parent_invocation_id",
        "parent_principal",
        "correlation_id",
        "root_human_id",
        "is_human_rooted",
        "persona",
        "repo",
        "control_address",
        "control_port",
        "control_generation",
        "control_token",
        "control_version",
    }
)

# The one port the control listener is dialled on, pinned here exactly as
# ``activity/control_service`` pins it. Registering a caller-supplied port would
# reintroduce the redirect this module removes, one indirection further along.
CONTROL_PORT_ENV = "AGENT_CONTROL_PORT"
DEFAULT_CONTROL_PORT = 8770

# Schema version written on the control record, matching the worker helper's
# ``CONTROL_RECORD_VERSION``. Asserted by a parity test rather than shared,
# because no build step spans this service and the worker image.
CONTROL_RECORD_VERSION = 1

# Removed together at teardown. Retain the generation counter so a later attempt
# cannot reuse an old generation. Tokens and addresses must disappear together:
# pod IPs are reused.
CONTROL_ATTRIBUTES = (
    "control_version",
    "control_address",
    "control_port",
    "control_token",
    "control_token_expires_at",
    "control_registered_at",
    "control_credential_epoch",
)

_REGISTRATION_PREFIX = "REG#"
_TENANT_PREFIX = "TENANT#"

# A control token is CSPRNG material minted in the pod (``secrets.token_urlsafe``).
# Bounded and charset-checked before storage so it cannot carry a payload into the
# row, and so an oversized value fails here rather than at DynamoDB.
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}\Z")
_ISO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


class RegistrationRefusedError(Exception):
    """The write was refused.

    One type for every refusal, and the message is deliberately coarse. A caller
    able to distinguish "no such run" from "your attempt is superseded" learns
    whether a run it named exists and how many times it has been retried.
    """


@dataclass(frozen=True)
class ControlRegistration:
    """The generation and destination assigned to one attempt's listener."""

    generation: int
    address: str
    port: int


class AgentRegistrationService:
    """Writes a run's own status and control registration on its behalf.

    Takes the shared policy (for caller authentication and liveness), the
    protected authority store (for the authoritative row key and registration
    ledger) and a DynamoDB client for the events table. It holds no per-request
    state, so one instance serves the process.
    """

    def __init__(
        self,
        *,
        policy: AgentAuthorizationService,
        authority_table: str,
        events_table: str,
        dynamodb_client,
        authority_client=None,
        now: Callable[[], datetime] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if not authority_table or not events_table:
            raise AuthorityStoreError("authority and events table names are required")
        self._policy = policy
        self._authority_table = authority_table
        self._events_table = events_table
        self._client = dynamodb_client
        # Defaults to the same client. Separate parameter because the two tables
        # are different trust domains and a deployment may reach them through
        # different clients; defaulting keeps the common case one argument.
        self._authority_client = authority_client or dynamodb_client
        self._now = now or (lambda: datetime.now(UTC))
        self._env = env
        self._bootstrap = BootstrapStore(table_name=authority_table, dynamodb_client=self._authority_client)

    # -- caller resolution ------------------------------------------------

    def _resolve(self, *, credential_token: str, pod: VerifiedPod, terminal: bool = False, clearing: bool = False) -> ExecutionRecord:
        try:
            caller = self._policy.resolve_caller(credential_token)
            record = self._bootstrap.authority.load_execution(invocation_id=caller.invocation_id, tenant_id=caller.tenant_id)
            accepted = {ExecutionStatus.COMPLETED} if terminal else set()
            if clearing:
                accepted |= {ExecutionStatus.COMPLETED, ExecutionStatus.CANCELLED, ExecutionStatus.REVOKED}
            checked = replace(record, status=ExecutionStatus.ACTIVE) if record and record.status in accepted else record
            evaluate_execution_state(
                record=checked,
                invocation_id=caller.invocation_id,
                attempt=caller.attempt,
                tenant_id=caller.tenant_id,
                credential_epoch=caller.credential_epoch,
                now=self._now(),
                presented_workload_binding=pod.uid,
            )
            if record is None or not record.workload_binding or record.workload_binding != pod.uid or not record.arrived_at:
                raise RegistrationRefusedError("not found")
            if not clearing and not terminal and record.status == ExecutionStatus.ACTIVE:
                self._bootstrap.live_grant(
                    invocation_id=record.invocation_id, tenant_id=record.tenant_id, attempt=record.current_attempt, now=self._now()
                )
            return record
        except (PolicyError, ExecutionStateError, BootstrapRefusedError):
            raise RegistrationRefusedError("not found") from None

    # -- status -----------------------------------------------------------

    def record_status(
        self,
        *,
        credential_token: str,
        pod: VerifiedPod,
        status: str,
        fields: dict[str, str] | None = None,
    ) -> None:
        """Write an allowlisted status transition to the caller's own row."""
        record = self._resolve(credential_token=credential_token, pod=pod, terminal=status != "in_progress")
        if status not in ALLOWED_STATUSES:
            raise RegistrationRefusedError("unsupported status")

        supplied = dict(fields or {})
        rejected = sorted(set(supplied) & REJECTED_FIELDS)
        if rejected:
            # Logged with names but not values: a rejected field's value may be
            # the identity a caller was attempting to claim.
            logger.warning(
                "Refusing status write carrying protected fields",
                extra={"invocation_id": record.invocation_id, "fields": rejected},
            )
            raise RegistrationRefusedError("unsupported field")
        unknown = sorted(set(supplied) - set(ALLOWED_STATUS_FIELDS))
        if unknown:
            raise RegistrationRefusedError("unsupported field")

        names = {"#st": "status", "#su": "status_updated_at"}
        values = {
            ":status": {"S": status},
            ":status_updated_at": {"S": _iso(self._now())},
        }
        sets = ["#st = :status", "#su = :status_updated_at"]
        for index, (field, bound) in enumerate(sorted(ALLOWED_STATUS_FIELDS.items())):
            raw = supplied.get(field)
            if raw is None or raw == "":
                continue
            if not isinstance(raw, str):
                raise RegistrationRefusedError("unsupported field")
            names[f"#f{index}"] = field
            values[f":f{index}"] = {"S": raw[:bound]}
            sets.append(f"#f{index} = :f{index}")

        update = "SET " + ", ".join(sets)
        if status == "in_progress":
            self._update_row(record, update=update, names=names, values=values)
        else:
            self._commit_terminal(record, status=status, supplied=supplied, update=update, names=names, values=values)

    def _commit_terminal(self, record, *, status, supplied, update, names, values):
        digest = hashlib.sha256(json.dumps({"status": status, "fields": supplied}, sort_keys=True).encode()).hexdigest()
        raw = self._bootstrap._read(f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}") or {}
        if record.status == ExecutionStatus.COMPLETED:
            if raw.get("terminal_report_digest") != {"S": digest}:
                raise RegistrationRefusedError("terminal report conflicts with recorded outcome")
            return
        execution_update = self._execution_check(record)["ConditionCheck"]
        execution_update["UpdateExpression"] = "SET #st = :complete, terminal_report_digest = :digest, terminal_outcome = :outcome"
        execution_update["ExpressionAttributeValues"].update({":complete": {"S": "completed"}, ":digest": {"S": digest}, ":outcome": {"S": status}})
        transaction = [
            {
                "Update": {
                    "TableName": self._events_table,
                    "Key": self._event_key(record),
                    "UpdateExpression": update + " REMOVE " + ", ".join(CONTROL_ATTRIBUTES),
                    "ConditionExpression": "attribute_exists(event_id) AND tenant_id = :tenant",
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": {**values, ":tenant": {"S": record.tenant_id}},
                }
            },
            {"Update": execution_update},
        ]
        parent_grant = raw.get("parent_grant_id", {}).get("S")
        reservation = raw.get("dispatch_reservation_id", {}).get("S")
        if bool(parent_grant) != bool(reservation):
            raise AuthorityStoreError("dispatch reservation metadata is incomplete")
        if parent_grant and reservation:
            now = _iso(self._now())
            transaction += [
                {
                    "Update": {
                        "TableName": self._authority_table,
                        "Key": _key(f"TENANT#{record.tenant_id}", f"RESV#{parent_grant}#{reservation}"),
                        "UpdateExpression": "SET #s = :released, updated_at = :now",
                        "ConditionExpression": "#s = :held",
                        "ExpressionAttributeNames": {"#s": "state"},
                        "ExpressionAttributeValues": {":held": {"S": "held"}, ":released": {"S": "released"}, ":now": {"S": now}},
                    }
                },
                {
                    "Update": {
                        "TableName": self._authority_table,
                        "Key": _key(f"TENANT#{record.tenant_id}", f"RESV#{parent_grant}"),
                        "UpdateExpression": "ADD in_flight :minus SET updated_at = :now",
                        "ConditionExpression": "in_flight >= :one",
                        "ExpressionAttributeValues": {":minus": {"N": "-1"}, ":one": {"N": "1"}, ":now": {"S": now}},
                    }
                },
            ]
        try:
            self._authority_client.transact_write_items(TransactItems=transaction)
        except (ClientError, BotoCoreError) as exc:
            committed = self._bootstrap._read(f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}") or {}
            if committed.get("status") == {"S": "completed"} and committed.get("terminal_report_digest") == {"S": digest}:
                return
            if _conditional_refusal(exc):
                raise RegistrationRefusedError("not found") from None
            raise AuthorityStoreError("terminal report was not committed") from None

    # -- control registration ---------------------------------------------

    def register_control(
        self,
        *,
        credential_token: str,
        pod: VerifiedPod,
        token: str,
        token_expires_at: str,
    ) -> ControlRegistration:
        """Register this pod's control listener, assigning its generation once.

        The address is the **verified pod's** IP and the port is pinned
        configuration; only the token and its expiry come from the caller, because
        only the pod can mint the token it will honour.
        """
        record = self._resolve(credential_token=credential_token, pod=pod)
        if not _TOKEN_PATTERN.fullmatch(token or ""):
            raise RegistrationRefusedError("invalid control token")
        if not _ISO_PATTERN.fullmatch(token_expires_at or ""):
            raise RegistrationRefusedError("invalid control token expiry")
        expires = _parse_iso(token_expires_at)
        if expires is None or expires <= self._now():
            # An already-expired registration produces a run the UI reports as
            # controllable and the gateway refuses at click time.
            raise RegistrationRefusedError("invalid control token expiry")
        if not pod.ip:
            raise RegistrationRefusedError("not found")

        port = _configured_port(self._env)
        digest = _token_digest(token)

        if (expires - self._now()).total_seconds() > 6 * 60 * 60:
            raise RegistrationRefusedError("invalid control token expiry")
        for _ in range(3):
            existing = self._read_registration(record)
            if existing is not None:
                return self._recover_registration(record, existing, digest=digest, pod=pod, port=port, expiry=token_expires_at)
            row = self._read_event(record)
            if row is None or row.get("tenant_id") != {"S": record.tenant_id}:
                raise RegistrationRefusedError("not found")
            previous = _positive_int(row.get("control_generation", {}).get("N"))
            if "control_generation" in row and previous is None:
                raise RegistrationRefusedError("invalid control generation")
            generation = (previous or 0) + 1
            now_iso = _iso(self._now())
            values = {
                ":v": {"N": str(CONTROL_RECORD_VERSION)},
                ":a": {"S": pod.ip},
                ":p": {"N": str(port)},
                ":t": {"S": token},
                ":e": {"S": token_expires_at},
                ":r": {"S": now_iso},
                ":one": {"N": "1"},
                ":tid": {"S": record.tenant_id},
            }
            condition = "attribute_exists(event_id) AND tenant_id = :tid AND "
            if previous is None:
                condition += "attribute_not_exists(control_generation)"
            else:
                condition += "control_generation = :previous"
                values[":previous"] = {"N": str(previous)}
            ledger = {
                **self._registration_key(record),
                "invocation_id": {"S": record.invocation_id},
                "tenant_id": {"S": record.tenant_id},
                "attempt": {"N": str(record.current_attempt)},
                "generation": {"N": str(generation)},
                "token_digest": {"S": digest},
                "token_expires_at": {"S": token_expires_at},
                "address": {"S": pod.ip},
                "port": {"N": str(port)},
                "workload_binding": {"S": pod.uid},
                "registered_at": {"S": now_iso},
                "credential_epoch": {"N": "1"},
            }
            transactions = [
                {
                    "Update": {
                        "TableName": self._events_table,
                        "Key": self._event_key(record),
                        "UpdateExpression": (
                            "SET control_version = :v, control_address = :a, control_port = :p, "
                            "control_token = :t, control_token_expires_at = :e, control_registered_at = :r, "
                            "control_credential_epoch = :one ADD control_generation :one"
                        ),
                        "ConditionExpression": condition,
                        "ExpressionAttributeValues": values,
                    }
                },
                {"Put": {"TableName": self._authority_table, "Item": ledger, "ConditionExpression": "attribute_not_exists(sk)"}},
                *self._live_checks(record),
            ]
            try:
                self._authority_client.transact_write_items(TransactItems=transactions)
                return ControlRegistration(generation=generation, address=pod.ip, port=port)
            except (ClientError, BotoCoreError):
                # A response may be lost after commit. Both records commit or
                # neither does, so no retry can advance just the counter.
                existing = self._read_registration(record)
                if existing is not None:
                    return self._recover_registration(record, existing, digest=digest, pod=pod, port=port, expiry=token_expires_at)
                self._resolve(credential_token=credential_token, pod=pod)
        raise AuthorityStoreError("control registration unavailable")

    def control_registration_state(self, *, credential_token, pod, generation) -> dict:
        record = self._resolve(credential_token=credential_token, pod=pod)
        ledger = self._read_registration(record) or {}
        row = self._read_event(record) or {}
        if (
            ledger.get("generation") != {"N": str(generation)}
            or ledger.get("workload_binding") != {"S": pod.uid}
            or row.get("control_generation") != {"N": str(generation)}
            or "control_token" not in row
        ):
            raise RegistrationRefusedError("not found")
        return {
            "control_generation": generation,
            "control_credential_epoch": int(ledger.get("credential_epoch", {"N": "1"})["N"]),
            "rotation_id": ledger.get("rotation_id", {}).get("S"),
        }

    def renew_control(self, *, credential_token, pod, generation, expected_epoch, rotation_id, token, token_expires_at) -> dict:
        """Rotate one listener credential without changing its generation.

        The worker stages the next credential locally before this commit. A lost
        response retries the same rotation ID, token and expiry; it never starts
        another rotation while the previous outcome is unknown.
        """
        record = self._resolve(credential_token=credential_token, pod=pod)
        expires = _parse_iso(token_expires_at)
        if (
            not isinstance(token, str)
            or not _TOKEN_PATTERN.fullmatch(token)
            or expires is None
            or not 0 < (expires - self._now()).total_seconds() <= 3600
            or type(generation) is not int
            or generation < 1
            or type(expected_epoch) is not int
            or expected_epoch < 1
            or not isinstance(rotation_id, str)
            or not re.fullmatch(r"[0-9a-f-]{36}", rotation_id)
        ):
            raise RegistrationRefusedError("invalid control renewal")
        digest = _token_digest(token)
        ledger = self._read_registration(record) or {}
        if ledger.get("generation") != {"N": str(generation)} or ledger.get("workload_binding") != {"S": pod.uid}:
            raise RegistrationRefusedError("not found")
        current_epoch = int(ledger.get("credential_epoch", {"N": "1"})["N"])
        next_epoch = expected_epoch + 1

        def recovered(item):
            return (
                item.get("credential_epoch") == {"N": str(next_epoch)}
                and item.get("rotation_id") == {"S": rotation_id}
                and item.get("token_digest") == {"S": digest}
                and item.get("token_expires_at") == {"S": token_expires_at}
            )

        result = {"control_generation": generation, "control_credential_epoch": next_epoch, "rotation_id": rotation_id}
        if recovered(ledger):
            self._recover_registration(record, ledger, digest=digest, pod=pod, port=_configured_port(self._env), expiry=token_expires_at)
            return result
        if current_epoch != expected_epoch or ledger.get("token_digest") == {"S": digest}:
            raise RegistrationRefusedError("control renewal conflicts with recorded outcome")
        values = {":generation": {"N": str(generation)}, ":old": {"N": str(expected_epoch)}, ":next": {"N": str(next_epoch)}}
        transaction = [
            {
                "Update": {
                    "TableName": self._events_table,
                    "Key": self._event_key(record),
                    "UpdateExpression": "SET control_token = :token, control_token_expires_at = :expiry, control_credential_epoch = :next",
                    "ConditionExpression": (
                        "tenant_id = :tenant AND control_generation = :generation "
                        "AND control_credential_epoch = :old AND attribute_exists(control_token)"
                    ),
                    "ExpressionAttributeValues": {
                        **values,
                        ":tenant": {"S": record.tenant_id},
                        ":token": {"S": token},
                        ":expiry": {"S": token_expires_at},
                    },
                }
            },
            {
                "Update": {
                    "TableName": self._authority_table,
                    "Key": self._registration_key(record),
                    "UpdateExpression": "SET token_digest = :digest, token_expires_at = :expiry, credential_epoch = :next, rotation_id = :rotation",
                    "ConditionExpression": "generation = :generation AND credential_epoch = :old AND workload_binding = :pod",
                    "ExpressionAttributeValues": {
                        **values,
                        ":pod": {"S": pod.uid},
                        ":digest": {"S": digest},
                        ":expiry": {"S": token_expires_at},
                        ":rotation": {"S": rotation_id},
                    },
                }
            },
            *self._live_checks(record),
        ]
        try:
            self._authority_client.transact_write_items(TransactItems=transaction)
        except (ClientError, BotoCoreError) as exc:
            committed = self._read_registration(record) or {}
            if recovered(committed):
                self._recover_registration(record, committed, digest=digest, pod=pod, port=_configured_port(self._env), expiry=token_expires_at)
                return result
            if _conditional_refusal(exc):
                raise RegistrationRefusedError("not found") from None
            raise AuthorityStoreError("control renewal unavailable") from None
        return result

    def _recover_registration(self, record, existing, *, digest, pod, port, expiry):
        generation = _positive_int(existing.get("generation", {}).get("N"))
        if (
            not hmac.compare_digest(existing.get("token_digest", {}).get("S", ""), digest)
            or generation is None
            or existing.get("workload_binding") != {"S": pod.uid}
            or existing.get("token_expires_at") != {"S": expiry}
            or existing.get("address") != {"S": pod.ip}
            or existing.get("port") != {"N": str(port)}
        ):
            raise RegistrationRefusedError("control already registered")
        row = self._read_event(record) or {}
        if (
            row.get("control_generation") != {"N": str(generation)}
            or _token_digest(row.get("control_token", {}).get("S", "")) != digest
            or row.get("control_token_expires_at") != {"S": expiry}
        ):
            raise RegistrationRefusedError("registration is no longer current")
        return ControlRegistration(generation=generation, address=pod.ip, port=port)

    def _event_key(self, record):
        return {"event_id": {"S": record.invocation_id}, "arrived_at": {"S": record.arrived_at}}

    def _read_event(self, record):
        try:
            return self._client.get_item(TableName=self._events_table, Key=self._event_key(record), ConsistentRead=True).get("Item")
        except (ClientError, BotoCoreError):
            raise AuthorityStoreError("events table unavailable") from None

    def _live_checks(self, record):
        try:
            grant = self._bootstrap.live_grant(
                invocation_id=record.invocation_id, tenant_id=record.tenant_id, attempt=record.current_attempt, now=self._now()
            )
        except BootstrapRefusedError:
            raise RegistrationRefusedError("not found") from None
        return [self._execution_check(record), self._bootstrap._grant_check(grant, self._now()), self._bootstrap._authority_check(grant)]

    def _execution_check(self, record):
        return {
            "ConditionCheck": {
                "TableName": self._authority_table,
                "Key": _key(f"TENANT#{record.tenant_id}", f"EXEC#{record.invocation_id}"),
                "ConditionExpression": (
                    "#st = :status AND current_attempt = :attempt AND current_credential_epoch = :epoch AND workload_binding = :pod"
                ),
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {
                    ":status": {"S": str(record.status)},
                    ":attempt": {"N": str(record.current_attempt)},
                    ":epoch": {"N": str(record.current_credential_epoch)},
                    ":pod": {"S": record.workload_binding},
                },
            }
        }

    def clear_control(self, *, credential_token: str, pod: VerifiedPod, generation: int) -> None:
        """Remove the control fields at terminal teardown.

        Scoped to the caller's own attempt *and* to the generation it registered,
        so a stale attempt cannot strip a live listener's registration. Terminal
        cleanup can always clear its own, because the generation it presents is
        the one it was assigned.
        """
        record = self._resolve(credential_token=credential_token, pod=pod, clearing=True)
        if type(generation) is not int or generation < 1:
            raise RegistrationRefusedError("invalid generation")
        ledger = self._read_registration(record)
        if not ledger or ledger.get("generation") != {"N": str(generation)} or ledger.get("workload_binding") != {"S": pod.uid}:
            raise RegistrationRefusedError("not found")
        self._update_row(
            record,
            update="REMOVE " + ", ".join(CONTROL_ATTRIBUTES),
            names={"#cg": "control_generation"},
            values={":cg": {"N": str(generation)}},
            extra_condition="#cg = :cg",
            cleanup=True,
        )

    # -- storage ----------------------------------------------------------

    def _update_row(
        self,
        record: ExecutionRecord,
        *,
        update: str,
        names: dict[str, str],
        values: dict[str, dict],
        extra_condition: str | None = None,
        cleanup: bool = False,
    ) -> None:
        """Point-update the caller's own events row.

        The key comes entirely from ``record``. The tenant is conditioned rather
        than assumed: this is not the protected table, so its rows must not be
        trusted to be in the tenant the protected record names — a disagreement
        means the two tables describe different things and the write must not land.
        """
        names = {**names, "#tid": "tenant_id"}
        values = {**values, ":tid": {"S": record.tenant_id}}
        condition = "attribute_exists(event_id) AND #tid = :tid"
        if extra_condition:
            condition += f" AND {extra_condition}"
        try:
            self._authority_client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._events_table,
                            "Key": self._event_key(record),
                            "UpdateExpression": update,
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": values,
                            "ConditionExpression": condition,
                        }
                    },
                    *([self._execution_check(record)] if cleanup else self._live_checks(record)),
                ]
            )
        except ClientError as exc:
            if _conditional_refusal(exc):
                raise RegistrationRefusedError("not found") from exc
            logger.error(
                "Events row write failed",
                extra={"invocation_id": record.invocation_id, "code": exc.response.get("Error", {}).get("Code")},
            )
            raise AuthorityStoreError("events table unavailable") from exc
        except BotoCoreError as exc:
            raise AuthorityStoreError("events table unavailable") from exc

    def _registration_key(self, record: ExecutionRecord) -> dict:
        return {
            "pk": {"S": f"{_TENANT_PREFIX}{record.tenant_id}"},
            "sk": {"S": f"{_REGISTRATION_PREFIX}{record.invocation_id}#{record.current_attempt}"},
        }

    def _read_registration(self, record: ExecutionRecord) -> dict | None:
        try:
            response = self._authority_client.get_item(
                TableName=self._authority_table,
                Key=self._registration_key(record),
                # Strongly consistent: an eventually-consistent read here would
                # miss a just-committed registration and increment the generation
                # a second time, which is the exact failure this ledger prevents.
                ConsistentRead=True,
            )
        except (ClientError, BotoCoreError) as exc:
            raise AuthorityStoreError("authority store unavailable") from exc
        return response.get("Item")


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "strict")).hexdigest()


def _conditional_refusal(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
        return True
    return any(reason.get("Code") == "ConditionalCheckFailed" for reason in exc.response.get("CancellationReasons", []))


def _configured_port(env: dict[str, str] | None) -> int:
    source = os.environ if env is None else env
    raw = (source.get(CONTROL_PORT_ENV) or "").strip()
    if not raw:
        return DEFAULT_CONTROL_PORT
    try:
        port = int(raw)
    except ValueError:
        logger.warning("Malformed %s — using the default control port", CONTROL_PORT_ENV)
        return DEFAULT_CONTROL_PORT
    if not 1024 < port < 65536:
        logger.warning("Out-of-range %s — using the default control port", CONTROL_PORT_ENV)
        return DEFAULT_CONTROL_PORT
    return port


def _positive_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None
