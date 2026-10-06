"""Atomic timeline range replacement retaining authorized, expandable sources."""

import hashlib
import json
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import Field, model_validator

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationUnavailableError
from src.agentauth.chat_history_store import _Item, history_version
from src.agentauth.chat_history_summary import ChatSummaryWriter, SummaryAppend
from src.agentauth.chat_history_write import ChatHistoryConflictError
from src.chat_logging.scrubber import RegexScrubber
from src.orchestration.chat_data_migration import _owner_fields

MAX_COMPACTION_ITEMS = 94


class HistoryCompaction(SummaryAppend):
    from_ordinal: int = Field(ge=0, le=99_999_999)
    to_ordinal: int = Field(ge=0, le=99_999_999)

    @model_validator(mode="after")
    def ordered_range(self):
        if self.from_ordinal > self.to_ordinal:
            raise ValueError("compaction range must be ordered")
        return self


class ChatHistoryCompactor(ChatSummaryWriter):
    def _range(self, header: dict, write: HistoryCompaction) -> list[dict]:
        query = {
            "KeyConditionExpression": Key("PK").eq(header["PK"])
            & Key("SK").between(f"item#{write.from_ordinal:08d}", f"item#{write.to_ordinal:08d}"),
            "ConsistentRead": True,
            "ScanIndexForward": True,
            "Limit": MAX_COMPACTION_ITEMS + 1,
        }
        rows, items = [], []
        while True:
            try:
                page = self.table.query(**query)
            except (BotoCoreError, ClientError):
                raise ChatAuthorizationUnavailableError("chat compaction range unavailable") from None
            for row in page.get("Items", []):
                item = self.history._project(row, header, _Item)
                if row.get("SK") != f"item#{item['ordinal']:08d}" or not write.from_ordinal <= item["ordinal"] <= write.to_ordinal:
                    raise ChatAuthorizationUnavailableError("chat compaction ordering unavailable")
                rows.append(row)
                items.append(item)
            if len(rows) > MAX_COMPACTION_ITEMS:
                raise ChatHistoryConflictError("chat compaction range exceeds atomic limit")
            continuation = page.get("LastEvaluatedKey")
            if not continuation:
                break
            if not rows or continuation != {"PK": header["PK"], "SK": rows[-1]["SK"]} or continuation == query.get("ExclusiveStartKey"):
                raise ChatAuthorizationUnavailableError("chat compaction page unavailable")
            query["ExclusiveStartKey"] = continuation
            query["Limit"] = MAX_COMPACTION_ITEMS + 1 - len(rows)
        if not items or items[0]["ordinal"] != write.from_ordinal or items[-1]["ordinal"] != write.to_ordinal:
            raise ChatHistoryConflictError("chat compaction endpoints missing")
        if [item["ref"] for item in items if item["type"] == "msg"] != write.source_ids or [
            item["ref"] for item in items if item["type"] == "sum"
        ] != write.parent_ids:
            raise ChatHistoryConflictError("chat compaction sources do not match timeline")
        return rows

    def _item_write(self, row: dict, owner: dict, replacement: dict | None) -> dict:
        fields = sorted(set(row) | set(owner))
        names, values, conditions = {}, {}, []
        for index, field in enumerate(fields):
            name, value = f"#field{index}", f":value{index}"
            names[name] = field
            if field in row:
                conditions.append(f"{name} = {value}")
                values[value] = row[field]
            else:
                conditions.append(f"attribute_not_exists({name})")
        operation = {
            "TableName": self.table.name,
            "ConditionExpression": " AND ".join(conditions),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": _encoded(values),
        }
        if replacement is not None:
            return {"Put": {**operation, "Item": _encoded(replacement)}}
        return {"Delete": {**operation, "Key": _encoded({"PK": row["PK"], "SK": row["SK"]})}}

    def compact(self, token: str, *, run_id: str, session_id: str, write: HistoryCompaction, now: int) -> dict:
        launch, header = self._owned(token, run_id, session_id, now)
        digest = hashlib.sha256(json.dumps(write.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt_key = "compaction-write#" + hashlib.sha256(write.idempotency_key.encode()).hexdigest()
        receipt = self._receipt(session_id, receipt_key, digest, header, fields=frozenset({"summary_id", "version"}))
        if receipt is not None:
            return receipt
        if history_version(header) != write.expected_version or write.expected_version == 99_999_999:
            raise ChatHistoryConflictError("chat history version changed")
        rows = self._range(header, write)
        sources = self._sources(session_id, header, write, timeline=rows)
        result = {"summary_id": "sum_" + uuid.uuid4().hex, "version": write.expected_version + 1}
        owner = _owner_fields((launch.tenant_id, launch.team_id, launch.user_id))
        provenance = {**owner, "PK": header["PK"], "runId": launch.run_id, "leaseGeneration": launch.lease_generation}
        records = [
            {
                **provenance,
                "SK": "sum#" + result["summary_id"],
                **sources,
                "content": RegexScrubber().scrub_text(write.content).content,
                "tokens": write.tokens,
            },
            {**provenance, "SK": receipt_key, "requestDigest": digest, "result": result},
        ]
        replacement = {
            **provenance,
            "SK": rows[0]["SK"],
            "ordinal": write.from_ordinal,
            "type": "sum",
            "ref": result["summary_id"],
            "tokens": write.tokens,
        }
        return self._commit(
            token,
            launch=launch,
            header=header,
            write=write,
            records=records,
            result=result,
            receipt_key=receipt_key,
            digest=digest,
            next_ordinal=self._next_ordinal(header),
            now=now,
            item_writes=[self._item_write(row, owner, replacement if index == 0 else None) for index, row in enumerate(rows)],
            rewrite=True,
        )
