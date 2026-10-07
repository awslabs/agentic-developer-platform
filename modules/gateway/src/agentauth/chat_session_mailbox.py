"""Durable, owner-scoped session mode and ordered turn acceptance."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agentauth.chat_admission import retention_seconds
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, Identifier
from src.chat_logging.scrubber import RegexScrubber
from src.orchestration.chat_data_migration import _owner_fields, _owns_context_row


class AcceptedTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    turn_id: Identifier
    message: str = Field(min_length=1, max_length=65_536)


class ChatSessionMailbox:
    def __init__(self, table):
        self.table = table

    def _header(self, session_id, owner, now):
        header = self.table.get_item(Key={"PK": f"session#{session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
        if header is not None:
            expiry = header.get("ttl")
            if (
                not _owns_context_row(header, owner)
                or header.get("status") != "active"
                or isinstance(expiry, bool)
                or not isinstance(expiry, int | Decimal)
                or expiry <= now
            ):
                raise ChatAuthorizationRefusedError("chat session unavailable")
        return header

    @staticmethod
    def _sequence(header):
        sequence = header.get("sessionTurnSequence", 0) if header else 0
        if isinstance(sequence, bool) or not isinstance(sequence, int | Decimal) or int(sequence) != sequence or not 0 <= sequence < 99_999_999:
            raise ChatAuthorizationUnavailableError("chat turn ordering unavailable")
        return int(sequence)

    @staticmethod
    def _mode(header):
        mode = header.get("sessionMode", "ephemeral") if header else "ephemeral"
        if mode not in ("ephemeral", "persistent"):
            raise ChatAuthorizationUnavailableError("chat session mode unavailable")
        return mode

    def state(self, *, session_id: Identifier, owner: tuple[str, str, str], now: int):
        header = self._header(session_id, owner, now)
        health = header.get("sessionState", "idle") if header else "idle"
        if health == "recovering" and header.get("sessionEndReason"):
            health = "ending"
        lease = header.get("chatLease") if header else None
        lease_expiry = lease.get("expires_at") if isinstance(lease, dict) else None
        if health == "active" and (isinstance(lease_expiry, bool) or not isinstance(lease_expiry, int | Decimal) or lease_expiry <= now):
            health = "recovering"
        state = {"mode": self._mode(header), "sequence": self._sequence(header), "health": health}
        started = header.get("sessionCleanupStartedAt") if header else None
        if health == "ending" and isinstance(started, int | Decimal) and not isinstance(started, bool):
            state["cleanup_elapsed_seconds"] = max(0, now - int(started))
            if state["cleanup_elapsed_seconds"] >= 120:
                state["health"] = "cleanup_delayed"
        if header and header.get("sessionPendingMode") in ("ephemeral", "persistent"):
            state["pending_mode"] = header["sessionPendingMode"]
        return state

    def next_turn(
        self,
        *,
        session_id: Identifier,
        owner: tuple[str, str, str],
        run_id: Identifier,
        sandbox_uid: Identifier,
        generation: int,
        after: int,
        now: int,
    ):
        header = self._header(session_id, owner, now)
        if header is None or self._mode(header) != "persistent":
            raise ChatAuthorizationRefusedError("chat mailbox unavailable")
        lease = header.get("chatLease")
        if (
            not isinstance(lease, dict)
            or lease.get("run_id") != run_id
            or lease.get("sandbox_uid") != sandbox_uid
            or lease.get("generation") != generation
            or not isinstance(lease.get("expires_at"), int | Decimal)
            or lease["expires_at"] <= now
        ):
            raise ChatAuthorizationRefusedError("chat mailbox lease unavailable")
        sequence = self._sequence(header)
        if after > sequence:
            raise ChatAuthorizationRefusedError("chat mailbox cursor ahead of accepted turns")
        if after:
            previous = self.table.get_item(Key={"PK": f"session#{session_id}", "SK": f"mailbox#{after:08d}"}, ConsistentRead=True).get("Item")
            if (
                previous is not None
                and _owns_context_row(previous, owner)
                and previous.get("mode") == "persistent"
                and previous.get("turnId") == run_id
                and previous.get("status") == "result_recorded"
            ):
                return None
            if (
                previous is None
                or not _owns_context_row(previous, owner)
                or previous.get("mode") != "persistent"
                or previous.get("status") not in ("completed", "failed")
            ):
                raise ChatAuthorizationRefusedError("chat mailbox cursor not committed")
        if after == sequence:
            return None
        entry = self.table.get_item(Key={"PK": f"session#{session_id}", "SK": f"mailbox#{after + 1:08d}"}, ConsistentRead=True).get("Item")
        if (
            entry is None
            or not _owns_context_row(entry, owner)
            or entry.get("mode") != "persistent"
            or entry.get("status") != "queued"
            or entry.get("sequence", after + 1) != after + 1
        ):
            raise ChatAuthorizationUnavailableError("chat mailbox entry unavailable")
        try:
            turn = AcceptedTurn(turn_id=entry["turnId"], message=entry["message"])
        except (KeyError, ValidationError):
            raise ChatAuthorizationRefusedError("chat mailbox turn changed") from None
        receipt = self.table.get_item(Key={"PK": f"session#{session_id}", "SK": f"mailbox-id#{turn.turn_id}"}, ConsistentRead=True).get("Item")
        digest = hashlib.sha256(json.dumps(turn.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt_sequence = receipt.get("sequence") if receipt else None
        if (
            receipt is None
            or not _owns_context_row(receipt, owner)
            or receipt.get("mode") != "persistent"
            or isinstance(receipt_sequence, bool)
            or not isinstance(receipt_sequence, int | Decimal)
            or receipt_sequence != after + 1
            or receipt.get("digest") != digest
        ):
            raise ChatAuthorizationRefusedError("chat mailbox turn not accepted")
        current = self._header(session_id, owner, now)
        if current is None or current.get("chatLease") != lease or self._mode(current) != "persistent":
            raise ChatAuthorizationRefusedError("chat mailbox lease changed")
        return {"sequence": after + 1, "turn_id": entry["turnId"], "message": entry["message"]}

    def initial_cursor(
        self,
        *,
        session_id: Identifier,
        owner: tuple[str, str, str],
        run_id: Identifier,
        sandbox_uid: Identifier,
        generation: int,
        message: str,
        now: int,
        allow_expired: bool = False,
    ) -> int:
        header = self._header(session_id, owner, now)
        if header is None or self._mode(header) != "persistent":
            raise ChatAuthorizationRefusedError("chat mailbox unavailable")
        lease = header.get("chatLease")
        if (
            not isinstance(lease, dict)
            or lease.get("run_id") != run_id
            or lease.get("sandbox_uid") != sandbox_uid
            or lease.get("generation") != generation
            or not isinstance(lease.get("expires_at"), int | Decimal)
            or (not allow_expired and lease["expires_at"] <= now)
        ):
            raise ChatAuthorizationRefusedError("chat mailbox lease unavailable")
        receipt = self.table.get_item(Key={"PK": f"session#{session_id}", "SK": f"mailbox-id#{run_id}"}, ConsistentRead=True).get("Item")
        if receipt is None or not _owns_context_row(receipt, owner) or receipt.get("mode") != "persistent":
            raise ChatAuthorizationRefusedError("chat initial turn not accepted")
        sequence = receipt.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int | Decimal)
            or int(sequence) != sequence
            or not 1 <= sequence <= self._sequence(header)
        ):
            raise ChatAuthorizationUnavailableError("chat initial sequence unavailable")
        entry = self.table.get_item(Key={"PK": f"session#{session_id}", "SK": f"mailbox#{int(sequence):08d}"}, ConsistentRead=True).get("Item")
        if (
            entry is None
            or not _owns_context_row(entry, owner)
            or entry.get("turnId") != run_id
            or not isinstance(entry.get("message"), str)
            or entry.get("mode") != "persistent"
            or RegexScrubber().scrub_text(entry["message"]).content != message
        ):
            raise ChatAuthorizationRefusedError("chat initial turn changed")
        digest = hashlib.sha256(
            json.dumps(AcceptedTurn(turn_id=run_id, message=entry["message"]).model_dump(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if receipt.get("digest") != digest:
            raise ChatAuthorizationRefusedError("chat initial turn digest changed")
        current = self._header(session_id, owner, now)
        if current is None or current.get("chatLease") != lease or self._mode(current) != "persistent":
            raise ChatAuthorizationRefusedError("chat mailbox lease changed")
        return int(sequence)

    def finalized_entry(
        self, *, session_id: Identifier, owner: tuple[str, str, str], run_id: Identifier, message: str, outcome: str, finalized_at: int
    ):
        receipt = self.table.get_item(Key={"PK": f"session#{session_id}", "SK": f"mailbox-id#{run_id}"}, ConsistentRead=True).get("Item")
        sequence = receipt.get("sequence") if receipt else None
        if (
            receipt is None
            or not _owns_context_row(receipt, owner)
            or receipt.get("mode") != "persistent"
            or isinstance(sequence, bool)
            or not isinstance(sequence, int | Decimal)
            or int(sequence) != sequence
            or not 1 <= sequence <= 99_999_999
        ):
            raise ChatAuthorizationRefusedError("chat terminal mailbox receipt unavailable")
        entry = self.table.get_item(Key={"PK": f"session#{session_id}", "SK": f"mailbox#{int(sequence):08d}"}, ConsistentRead=True).get("Item")
        if (
            entry is None
            or not _owns_context_row(entry, owner)
            or entry.get("mode") != "persistent"
            or entry.get("turnId") != run_id
            or entry.get("status") != outcome
            or entry.get("finalizedAt") != finalized_at
        ):
            raise ChatAuthorizationRefusedError("chat terminal mailbox changed")
        try:
            turn = AcceptedTurn(turn_id=run_id, message=entry["message"])
        except (KeyError, ValidationError):
            raise ChatAuthorizationRefusedError("chat terminal mailbox changed") from None
        digest = hashlib.sha256(json.dumps(turn.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if receipt.get("digest") != digest or RegexScrubber().scrub_text(turn.message).content != message:
            raise ChatAuthorizationRefusedError("chat terminal mailbox digest changed")
        return receipt, entry

    def finish_pending_mode(self, *, session_id: Identifier, owner: tuple[str, str, str], lease: dict, now: int):
        header = self._header(session_id, owner, now)
        if (
            not header
            or header.get("sessionMode") != "ephemeral"
            or header.get("sessionState") != "idle"
            or header.get("sessionPendingMode") != "persistent"
            or header.get("chatLease") != lease
        ):
            raise ChatAuthorizationRefusedError("chat pending mode changed")
        try:
            self.table.update_item(
                Key={"PK": f"session#{session_id}", "SK": "header"},
                UpdateExpression="SET sessionMode = :persistent, sessionState = :idle REMOVE sessionPendingMode, chatLease",
                ConditionExpression=(
                    "tenantId = :tenant AND teamId = :team AND ownerUserId = :user AND #status = :active "
                    "AND #ttl = :ttl AND #ttl > :now AND sessionMode = :ephemeral "
                    "AND sessionPendingMode = :persistent AND sessionState = :idle AND chatLease = :lease"
                ),
                ExpressionAttributeNames={"#status": "status", "#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":tenant": owner[0],
                    ":team": owner[1],
                    ":user": owner[2],
                    ":active": "active",
                    ":ttl": header["ttl"],
                    ":now": now,
                    ":persistent": "persistent",
                    ":ephemeral": "ephemeral",
                    ":idle": "idle",
                    ":lease": lease,
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ChatAuthorizationRefusedError("chat pending mode changed") from None
            raise ChatAuthorizationUnavailableError("chat pending mode unavailable") from None
        except BotoCoreError:
            raise ChatAuthorizationUnavailableError("chat pending mode unavailable") from None

    def select_mode(self, *, session_id: Identifier, owner: tuple[str, str, str], mode: Literal["ephemeral", "persistent"], now: int):
        header = self._header(session_id, owner, now)
        if header is None:
            stamp = datetime.fromtimestamp(now, UTC).isoformat()
            try:
                self.table.put_item(
                    Item={
                        "PK": f"session#{session_id}",
                        "SK": "header",
                        **_owner_fields(owner),
                        "status": "active",
                        "ttl": now + retention_seconds(),
                        "createdAt": stamp,
                        "lastActivityAt": stamp,
                        "sessionMode": mode,
                        "sessionState": "idle",
                        "sessionTurnSequence": 0,
                    },
                    ConditionExpression="attribute_not_exists(PK)",
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise ChatAuthorizationRefusedError("chat session changed") from None
                raise ChatAuthorizationUnavailableError("chat session mode unavailable") from None
            except BotoCoreError:
                raise ChatAuthorizationUnavailableError("chat session mode unavailable") from None
            return mode
        previous = self._mode(header)
        if header.get("sessionPendingMode"):
            if header["sessionPendingMode"] == mode:
                return previous
            raise ChatAuthorizationRefusedError("chat session mode change pending")
        if previous == mode:
            return mode
        lease = header.get("chatLease")
        if isinstance(lease, dict) and lease.get("run_id") and header.get("sessionState") == "ended" and header.get("sessionCleanupFinishedAt"):
            try:
                self.table.update_item(
                    Key={"PK": f"session#{session_id}", "SK": "header"},
                    UpdateExpression="SET sessionMode = :mode, sessionState = :idle REMOVE chatLease, sessionEndReason",
                    ConditionExpression=(
                        "tenantId = :tenant AND teamId = :team AND ownerUserId = :user "
                        "AND #status = :active AND #ttl = :ttl AND #ttl > :now "
                        "AND #mode = :previous AND sessionState = :ended AND attribute_exists(sessionCleanupFinishedAt) AND chatLease = :lease"
                    ),
                    ExpressionAttributeNames={"#status": "status", "#ttl": "ttl", "#mode": "sessionMode"},
                    ExpressionAttributeValues={
                        ":tenant": owner[0],
                        ":team": owner[1],
                        ":user": owner[2],
                        ":active": "active",
                        ":ttl": header["ttl"],
                        ":now": now,
                        ":mode": mode,
                        ":previous": previous,
                        ":ended": "ended",
                        ":idle": "idle",
                        ":lease": lease,
                    },
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise ChatAuthorizationRefusedError("chat session mode changed") from None
                raise ChatAuthorizationUnavailableError("chat session mode unavailable") from None
            except BotoCoreError:
                raise ChatAuthorizationUnavailableError("chat session mode unavailable") from None
            return mode
        if header.get("sessionState") in {"ending", "recovering", "ended"}:
            raise ChatAuthorizationRefusedError("chat session must end cleanup before mode change")
        if lease is not None and (not isinstance(lease, dict) or not lease.get("run_id") or not lease.get("sandbox_uid")):
            raise ChatAuthorizationRefusedError("chat session must end before mode change")
        if isinstance(lease, dict) and lease.get("run_id"):
            try:
                self.table.update_item(
                    Key={"PK": f"session#{session_id}", "SK": "header"},
                    UpdateExpression="SET sessionPendingMode = :mode",
                    ConditionExpression=(
                        "tenantId = :tenant AND teamId = :team AND ownerUserId = :user "
                        "AND #status = :active AND #ttl = :ttl AND #ttl > :now AND #mode = :previous "
                        "AND chatLease = :lease AND attribute_not_exists(sessionPendingMode)"
                    ),
                    ExpressionAttributeNames={"#status": "status", "#ttl": "ttl", "#mode": "sessionMode"},
                    ExpressionAttributeValues={
                        ":tenant": owner[0],
                        ":team": owner[1],
                        ":user": owner[2],
                        ":active": "active",
                        ":ttl": header["ttl"],
                        ":now": now,
                        ":mode": mode,
                        ":previous": previous,
                        ":lease": lease,
                    },
                )
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    raise ChatAuthorizationRefusedError("chat session mode changed") from None
                raise ChatAuthorizationUnavailableError("chat session mode unavailable") from None
            except BotoCoreError:
                raise ChatAuthorizationUnavailableError("chat session mode unavailable") from None
            return previous
        values = {":tenant": owner[0], ":team": owner[1], ":user": owner[2], ":active": "active", ":ttl": header["ttl"], ":now": now, ":mode": mode}
        if "sessionTurnSequence" in header:
            values[":sequence"] = self._sequence(header)
        if "sessionMode" in header:
            values[":previous"] = previous
        try:
            self.table.update_item(
                Key={"PK": f"session#{session_id}", "SK": "header"},
                UpdateExpression="SET sessionMode = :mode",
                ConditionExpression=(
                    "tenantId = :tenant AND teamId = :team AND ownerUserId = :user AND "
                    "#status = :active AND #ttl = :ttl AND #ttl > :now AND "
                    + ("#sequence = :sequence" if "sessionTurnSequence" in header else "attribute_not_exists(#sequence)")
                    + (" AND #mode = :previous" if "sessionMode" in header else " AND attribute_not_exists(#mode)")
                    + " AND (attribute_not_exists(chatLease) OR chatLease.expires_at <= :now)"
                ),
                ExpressionAttributeNames={"#status": "status", "#ttl": "ttl", "#sequence": "sessionTurnSequence", "#mode": "sessionMode"},
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ChatAuthorizationRefusedError("chat session changed") from None
            raise ChatAuthorizationUnavailableError("chat session mode unavailable") from None
        except BotoCoreError:
            raise ChatAuthorizationUnavailableError("chat session mode unavailable") from None
        return mode

    def accept(self, *, session_id: Identifier, owner: tuple[str, str, str], turn: AcceptedTurn, now: int, expected_mode=None):
        key = {"PK": f"session#{session_id}", "SK": f"mailbox-id#{turn.turn_id}"}
        digest = hashlib.sha256(json.dumps(turn.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for _attempt in range(5):
            header = self._header(session_id, owner, now)
            receipt = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
            if receipt:
                saved_sequence = receipt.get("sequence")
                saved_expiry = receipt.get("ttl")
                if (
                    header is None
                    or not _owns_context_row(receipt, owner)
                    or receipt.get("digest") != digest
                    or receipt.get("mode") not in ("ephemeral", "persistent")
                    or isinstance(saved_sequence, bool)
                    or not isinstance(saved_sequence, int | Decimal)
                    or int(saved_sequence) != saved_sequence
                    or not 1 <= saved_sequence <= self._sequence(header)
                    or isinstance(saved_expiry, bool)
                    or not isinstance(saved_expiry, int | Decimal)
                    or saved_expiry <= now
                ):
                    raise ChatAuthorizationRefusedError("chat turn id already used")
                entry = self.table.get_item(Key={"PK": key["PK"], "SK": f"mailbox#{int(saved_sequence):08d}"}, ConsistentRead=True).get("Item")
                if (
                    entry is None
                    or not _owns_context_row(entry, owner)
                    or entry.get("turnId") != turn.turn_id
                    or entry.get("message") != turn.message
                    or entry.get("mode") != receipt["mode"]
                    or entry.get("ttl") != saved_expiry
                ):
                    raise ChatAuthorizationRefusedError("chat accepted turn changed")
                return {"sequence": int(saved_sequence), "mode": receipt["mode"]}
            sequence = self._sequence(header)
            mode = self._mode(header)
            if header and header.get("sessionState") in {"ending", "recovering", "ended"}:
                raise ChatAuthorizationRefusedError("chat session ended")
            if header and header.get("sessionPendingMode"):
                raise ChatAuthorizationRefusedError("chat session mode change pending")
            if expected_mode is not None and mode != expected_mode:
                raise ChatAuthorizationRefusedError("chat session mode changed")
            if header is None:
                stamp = datetime.fromtimestamp(now, UTC).isoformat()
                header_write = {
                    "Put": {
                        "TableName": self.table.name,
                        "Item": {
                            "PK": key["PK"],
                            "SK": "header",
                            **_owner_fields(owner),
                            "status": "active",
                            "ttl": now + retention_seconds(),
                            "createdAt": stamp,
                            "lastActivityAt": stamp,
                            "sessionMode": mode,
                            "sessionState": "idle",
                            "sessionTurnSequence": 1,
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
                expiry = now + retention_seconds()
            else:
                activity = datetime.fromtimestamp(now, UTC).isoformat()
                previous_activity = header.get("lastActivityAt")
                if previous_activity is not None:
                    try:
                        recorded = datetime.fromisoformat(previous_activity)
                        if recorded.tzinfo is None:
                            raise ValueError
                        activity = max(datetime.fromtimestamp(now, UTC), recorded.astimezone(UTC)).isoformat()
                    except (TypeError, ValueError):
                        raise ChatAuthorizationUnavailableError("chat activity unavailable") from None
                names = {"#status": "status", "#ttl": "ttl", "#sequence": "sessionTurnSequence", "#mode": "sessionMode", "#state": "sessionState"}
                condition = "tenantId = :tenant AND teamId = :team AND ownerUserId = :user AND #status = :active AND #ttl = :ttl AND #ttl > :now AND "
                condition += "#sequence = :previous" if "sessionTurnSequence" in header else "attribute_not_exists(#sequence)"
                condition += " AND attribute_not_exists(sessionPendingMode)"
                condition += " AND #state = :state" if "sessionState" in header else " AND attribute_not_exists(#state)"
                condition += " AND #mode = :mode" if "sessionMode" in header else " AND attribute_not_exists(#mode)"
                condition += (
                    " AND lastActivityAt = :previous_activity" if previous_activity is not None else " AND attribute_not_exists(lastActivityAt)"
                )
                values = {
                    ":tenant": owner[0],
                    ":team": owner[1],
                    ":user": owner[2],
                    ":active": "active",
                    ":ttl": header["ttl"],
                    ":now": now,
                    ":next": sequence + 1,
                    ":mode": mode,
                    ":activity": activity,
                }
                if "sessionState" in header:
                    values[":state"] = header["sessionState"]
                if "sessionTurnSequence" in header:
                    values[":previous"] = sequence
                if previous_activity is not None:
                    values[":previous_activity"] = previous_activity
                header_write = {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": {"PK": key["PK"], "SK": "header"},
                        "UpdateExpression": "SET #sequence = :next, #mode = :mode, lastActivityAt = :activity",
                        "ConditionExpression": condition,
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": values,
                    }
                }
                expiry = int(header["ttl"])
            entry = {
                "PK": key["PK"],
                "SK": f"mailbox#{sequence + 1:08d}",
                **_owner_fields(owner),
                "turnId": turn.turn_id,
                "message": turn.message,
                "mode": mode,
                "status": "queued",
                "ttl": expiry,
            }
            receipt = {**key, **_owner_fields(owner), "digest": digest, "sequence": sequence + 1, "mode": mode, "ttl": expiry}
            notification = []
            if mode == "persistent":
                notification = [
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": {
                                "PK": "chat-notifications",
                                "SK": f"turn#{turn.turn_id}",
                                **_owner_fields(owner),
                                "sessionId": session_id,
                                "sequence": sequence + 1,
                                "ttl": expiry,
                            },
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    }
                ]
            try:
                self.table.meta.client.transact_write_items(
                    TransactItems=[
                        header_write,
                        {"Put": {"TableName": self.table.name, "Item": entry, "ConditionExpression": "attribute_not_exists(PK)"}},
                        {"Put": {"TableName": self.table.name, "Item": receipt, "ConditionExpression": "attribute_not_exists(PK)"}},
                        *notification,
                    ]
                )
                return {"sequence": sequence + 1, "mode": mode}
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "TransactionCanceledException" or not any(
                    reason.get("Code") == "ConditionalCheckFailed" for reason in error.response.get("CancellationReasons", [])
                ):
                    raise ChatAuthorizationUnavailableError("chat turn acceptance unavailable") from None
            except BotoCoreError:
                raise ChatAuthorizationUnavailableError("chat turn acceptance unavailable") from None
        raise ChatAuthorizationUnavailableError("chat turn acceptance contention")
