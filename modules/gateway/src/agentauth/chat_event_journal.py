"""Owner-scoped output sequences, atomic append intents and bounded durable replay."""

import json
import re
import secrets
from decimal import Decimal

from boto3.dynamodb.conditions import Key

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.chat_admission import _encoded, retention_seconds
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError
from src.orchestration.chat_data_migration import _owner_fields, _owns_context_row

EVENT_RETENTION_SECONDS = 86400
MAX_SEQUENCE = 99_999_999
CURSOR = re.compile(r"[a-f0-9]{32}:[0-9]{1,8}\Z")


def integer(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int | Decimal) or int(value) != value or not minimum <= value <= MAX_SEQUENCE:
        raise ChatAuthorizationUnavailableError("chat event sequence invalid")
    return int(value)


def public_event(row):
    return {
        "sequence": integer(row["sequence"], 1),
        "event_id": row["eventId"],
        "kind": row["kind"],
        "payload": json.loads(row["payload"]),
        "created_at": int(row["createdAt"]),
        "cursor": f"{row['journalId']}:{int(row['sequence'])}",
    }


class ChatEventJournal:
    def __init__(self, table):
        self.table = table

    def _get(self, session_id, key):
        return self.table.get_item(Key={"PK": f"session#{session_id}", "SK": key}, ConsistentRead=True).get("Item")

    def _state(self, session_id, owner, generation):
        state = self._get(session_id, "output-state")
        if state is not None and (not _owns_context_row(state, owner) or state.get("sessionGeneration") != generation):
            raise ChatAuthorizationRefusedError("chat event owner changed")
        if state is not None:
            integer(state.get("sequence"))
            if not re.fullmatch(r"[a-f0-9]{32}", state.get("journalId", "")):
                raise ChatAuthorizationUnavailableError("chat event journal invalid")
        return state

    def prepare(self, delivery, event_id, kind, payload, *, now):
        owner = (delivery.tenant_id, delivery.team_id, delivery.user_id)
        encoded_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if (
            kind not in {"ag_ui", "terminal"}
            or not isinstance(event_id, str)
            or not 1 <= len(event_id) <= 256
            or len(encoded_payload.encode()) > 200_000
        ):
            raise ChatAuthorizationUnavailableError("chat event exceeds bound")
        receipt_key = "output-id#" + envelope_digest({"generation": delivery.session_generation, "event_id": event_id})
        digest = envelope_digest({"kind": kind, "payload": payload})
        receipt = self._get(delivery.session_id, receipt_key)
        state = self._state(delivery.session_id, owner, delivery.session_generation)
        if receipt is not None:
            if (
                state is None
                or not _owns_context_row(receipt, owner)
                or receipt.get("digest") != digest
                or receipt.get("journalId") != state["journalId"]
                or receipt.get("sessionGeneration") != delivery.session_generation
            ):
                raise ChatAuthorizationRefusedError("chat event id reused")
            sequence = integer(receipt.get("sequence"), 1)
            event = self._get(delivery.session_id, f"output#{state['journalId']}#{sequence:08d}")
            if (
                event is None
                or not _owns_context_row(event, owner)
                or event.get("payload") != encoded_payload
                or event.get("kind") != kind
                or event.get("eventId") != event_id
                or event.get("ttl", 0) <= now
                or sequence > state["sequence"]
                or event.get("sequence") != sequence
                or event.get("sessionGeneration") != delivery.session_generation
                or event.get("journalId") != state["journalId"]
            ):
                raise ChatAuthorizationUnavailableError("chat event receipt unavailable")
            return public_event(event), []
        if state is not None and state.get("ttl", 0) <= now:
            raise ChatAuthorizationUnavailableError("chat event journal expired")
        sequence = integer(state["sequence"] + 1 if state else 1, 1)
        common = {
            "PK": f"session#{delivery.session_id}",
            **_owner_fields(owner),
            "sessionGeneration": delivery.session_generation,
            "journalId": state["journalId"] if state else secrets.token_hex(16),
            "sequence": sequence,
        }
        expiry = now + retention_seconds()
        event = {
            **common,
            "SK": f"output#{common['journalId']}#{sequence:08d}",
            "eventId": event_id,
            "kind": kind,
            "payload": encoded_payload,
            "createdAt": now,
            "ttl": min(expiry, now + EVENT_RETENTION_SECONDS),
        }
        receipt = {**common, "SK": receipt_key, "digest": digest, "ttl": expiry}
        updated = {**common, "SK": "output-state", "ttl": max(expiry, state["ttl"] if state else expiry)}
        state_put = {"TableName": self.table.name, "Item": _encoded(updated), "ConditionExpression": "attribute_not_exists(PK)"}
        if state is not None:
            fields = [field for field in state if field not in {"PK", "SK"}]
            state_put.update(
                ConditionExpression=" AND ".join(f"#field{index} = :value{index}" for index in range(len(fields))),
                ExpressionAttributeNames={f"#field{index}": field for index, field in enumerate(fields)},
                ExpressionAttributeValues=_encoded({f":value{index}": state[field] for index, field in enumerate(fields)}),
            )
        return public_event(event), [
            {"Put": state_put},
            *[
                {"Put": {"TableName": self.table.name, "Item": _encoded(item), "ConditionExpression": "attribute_not_exists(PK)"}}
                for item in (event, receipt)
            ],
        ]

    def replay(self, *, session_id, owner, generation, cursor, limit, now):
        state = self._state(session_id, owner, generation)
        if state is None:
            return {
                "status": "history_refresh_required",
                "reason": "journal_unavailable",
                "events": [],
                "cursor": None,
                "has_more": False,
                "retention_seconds": min(EVENT_RETENTION_SECONDS, retention_seconds()),
            }
        latest = integer(state["sequence"])
        response = {
            "status": "ok",
            "events": [],
            "cursor": f"{state['journalId']}:0",
            "has_more": False,
            "latest_sequence": latest,
            "retention_seconds": min(EVENT_RETENTION_SECONDS, retention_seconds()),
        }

        def gap(reason):
            return {
                **response,
                "status": "history_refresh_required",
                "reason": reason,
                "events": [],
                "cursor": f"{state['journalId']}:{latest}",
                "has_more": False,
            }

        if state.get("ttl", 0) <= now:
            return gap("retention_gap")
        after = 0
        if cursor is not None:
            if not CURSOR.fullmatch(cursor):
                raise ValueError("invalid chat event cursor")
            journal_id, position = cursor.split(":")
            after = int(position)
            if journal_id != state["journalId"] or after > latest:
                return gap("cursor_changed")
        response["cursor"] = f"{state['journalId']}:{after}"
        if after == latest:
            return response
        prefix = f"output#{state['journalId']}#"
        page = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"session#{session_id}") & Key("SK").between(f"{prefix}{after + 1:08d}", f"{prefix}{latest:08d}"),
            ConsistentRead=True,
            Limit=limit,
        )
        used = 0
        for row in page.get("Items", []):
            if not _owns_context_row(row, owner) or row.get("sessionGeneration") != generation or row.get("journalId") != state["journalId"]:
                raise ChatAuthorizationRefusedError("chat event owner changed")
            sequence = integer(row.get("sequence"), 1)
            if sequence != after + 1 or row.get("SK") != f"{prefix}{sequence:08d}" or row.get("ttl", 0) <= now:
                return gap("retention_gap")
            event = public_event(row)
            size = len(json.dumps(event, ensure_ascii=False).encode())
            if used + size > 256_000:
                break
            used += size
            response["events"].append(event)
            after = sequence
            response["cursor"] = event["cursor"]
        if not response["events"]:
            return gap("retention_gap")
        if not page.get("LastEvaluatedKey") and after < latest and len(response["events"]) == len(page.get("Items", [])):
            return gap("retention_gap")
        response["has_more"] = after < latest
        return response


def terminal_event_writes(authority, item, *, now):
    from src.agentauth.chat_delivery import ChatDelivery
    from src.agentauth.chat_terminal_delivery import terminal_delivery_payload

    document = json.loads(item["document"]["S"])
    delivery = ChatDelivery.model_validate(document["delivery"])
    payload = {**terminal_delivery_payload(document), "automatic_replay_permitted": False}
    return ChatEventJournal(authority.context_table).prepare(delivery, document["delivery_id"], "terminal", payload, now=now)[1]
