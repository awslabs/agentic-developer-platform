"""Scoped AgentCore memory controls; service acceptance is not recall visibility."""

import hashlib
import json
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.agentcore_governance import fence_revision, mutation_retry_epoch
from src.agentauth.chat_capability import ChatAuthorizationRefusedError


class MemoryManagementUnavailableError(Exception):
    pass


class MemoryRevisionConflictError(Exception):
    pass


class AgentCoreManagement:
    def __init__(self, provider):
        self.provider = provider

    @staticmethod
    def operation_receipt(namespace, record_id, revision, kind, text=None):
        values = [namespace, record_id, revision, kind]
        if kind == "correct":
            values.append(text)
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def public(record):
        text = record["content"]["text"]
        revision = hashlib.sha256(
            json.dumps([record["memoryRecordId"], text, str(record.get("createdAt", ""))], separators=(",", ":")).encode()
        ).hexdigest()
        return {"record_id": record["memoryRecordId"], "text": text, "revision": revision, "source": "AgentCore memory"}

    def _valid(self, record, namespace, record_id=None):
        if (
            not isinstance(record, dict)
            or (record_id is not None and record.get("memoryRecordId") != record_id)
            or not isinstance(record.get("memoryRecordId"), str)
            or record.get("namespaces") != [namespace]
            or record.get("memoryStrategyId") != self.provider.strategy_id
            or not isinstance(record.get("content"), dict)
            or not isinstance(record["content"].get("text"), str)
        ):
            raise ChatAuthorizationRefusedError("memory scope refused")
        return record

    def inspect(self, namespace, *, export=False, governance=None):
        visible = []
        partial = False
        next_token = None
        for _ in range(5 if export else 1):
            parameters = {
                "memoryId": self.provider.memory_id,
                "namespace": namespace,
                "memoryStrategyId": self.provider.strategy_id,
                "maxResults": 20,
            }
            if next_token:
                parameters["nextToken"] = next_token
            try:
                response = self.provider.client.list_memory_records(**parameters)
            except (BotoCoreError, ClientError) as error:
                raise MemoryManagementUnavailableError("memory service unavailable") from error
            records = response.get("memoryRecordSummaries")
            if not isinstance(records, list):
                raise MemoryManagementUnavailableError("memory response unavailable")
            for record in records[:20]:
                try:
                    self._valid(record, namespace)
                except ChatAuthorizationRefusedError:
                    partial = True
                    continue
                if len(record["content"]["text"]) > 2000:
                    partial = True
                    continue
                visible.append(record if governance is not None else self.public(record))
            next_token = response.get("nextToken")
            if not next_token:
                break
        if governance is not None:
            state = governance()
            if state["epoch"]:
                retained = []
                for record in visible:
                    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
                    if metadata.get("adp_epoch") != {"stringValue": str(state["epoch"])}:
                        partial = True
                        continue
                    if record["memoryRecordId"] in state["fences"]:
                        partial = True
                        continue
                    retained.append(self.public(record))
                visible = retained
                for record_id, fence in state["fences"].items():
                    if fence["kind"] == "correct" and len(visible) < (100 if export else 20):
                        visible.append(
                            {
                                "record_id": record_id,
                                "text": fence["text"],
                                "revision": fence_revision(record_id, fence),
                                "source": "Explicit correction",
                            }
                        )
            else:
                visible = [self.public(record) for record in visible]
        return {"records": visible, "status": "partial" if partial or next_token else "ok"}

    def visible_receipt(self, namespace, receipt_id, *, epoch):
        next_token = None
        for _ in range(5):
            parameters = {
                "memoryId": self.provider.memory_id,
                "namespace": namespace,
                "memoryStrategyId": self.provider.strategy_id,
                "maxResults": 20,
            }
            if next_token:
                parameters["nextToken"] = next_token
            try:
                response = self.provider.client.list_memory_records(**parameters)
            except (BotoCoreError, ClientError) as error:
                raise MemoryManagementUnavailableError("memory service unavailable") from error
            records = response.get("memoryRecordSummaries") if isinstance(response, dict) else None
            if not isinstance(records, list):
                raise MemoryManagementUnavailableError("memory response unavailable")
            for summary in records[:20]:
                if (
                    not isinstance(summary, dict)
                    or summary.get("namespaces") != [namespace]
                    or summary.get("memoryStrategyId") != self.provider.strategy_id
                ):
                    continue
                marker = summary.get("metadata")
                if not isinstance(marker, dict) or marker.get("adp_receipt") != {"stringValue": receipt_id}:
                    continue
                record_id = summary.get("memoryRecordId")
                if not isinstance(record_id, str) or not record_id:
                    continue
                try:
                    result = self.provider.client.get_memory_record(memoryId=self.provider.memory_id, namespace=namespace, memoryRecordId=record_id)
                    record = result.get("memoryRecord") if isinstance(result, dict) else None
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                        continue
                    raise MemoryManagementUnavailableError("memory service unavailable") from error
                except BotoCoreError as error:
                    raise MemoryManagementUnavailableError("memory service unavailable") from error
                if (
                    isinstance(record, dict)
                    and record.get("memoryRecordId") == record_id
                    and record.get("namespaces") == [namespace]
                    and record.get("memoryStrategyId") == self.provider.strategy_id
                    and isinstance(record.get("metadata"), dict)
                    and record["metadata"].get("adp_receipt") == {"stringValue": receipt_id}
                    and record["metadata"].get("adp_epoch") == {"stringValue": str(epoch)}
                    and isinstance(record.get("content"), dict)
                    and isinstance(record["content"].get("text"), str)
                    and record["content"]["text"].strip()
                ):
                    return record_id
            next_token = response.get("nextToken")
            if not next_token:
                break
        return None

    def _current(self, namespace, record_id, revision, *, governance=None, kind=None, text=None):
        if governance is not None:
            prior = governance()["fences"].get(record_id)
            if prior:
                mutation_retry_epoch(prior, record_id, revision, kind, text)
                return
        try:
            response = self.provider.client.get_memory_record(memoryId=self.provider.memory_id, namespace=namespace, memoryRecordId=record_id)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("ResourceNotFoundException", "ValidationException"):
                raise ChatAuthorizationRefusedError("memory scope refused") from None
            raise MemoryManagementUnavailableError("memory service unavailable") from error
        except BotoCoreError as error:
            raise MemoryManagementUnavailableError("memory service unavailable") from error
        record = self._valid(response.get("memoryRecord"), namespace, record_id)
        if self.public(record)["revision"] != revision:
            raise MemoryRevisionConflictError("memory revision changed")

    def correct(self, namespace, session_id, record_id, revision, text, *, fence=None, governance=None):
        self._current(namespace, record_id, revision, governance=governance, kind="correct", text=text)
        epoch = fence("correct", record_id, revision, text) if fence is not None else None
        receipt = self.operation_receipt(namespace, record_id, revision, "correct", text)
        try:
            response = self.provider.client.create_event(
                memoryId=self.provider.memory_id,
                actorId=namespace.rsplit("/", 1)[-1],
                sessionId=session_id,
                eventTimestamp=datetime.now(UTC),
                clientToken=receipt,
                payload=[{"conversational": {"content": {"text": text}, "role": "USER"}}],
                metadata={"adp_receipt": {"stringValue": receipt}, **({"adp_epoch": {"stringValue": str(epoch)}} if epoch is not None else {})},
            )
        except (BotoCoreError, ClientError) as error:
            raise MemoryManagementUnavailableError("memory service unavailable") from error
        event = response.get("event") if isinstance(response, dict) else None
        if (
            not isinstance(event, dict)
            or event.get("memoryId") != self.provider.memory_id
            or event.get("actorId") != namespace.rsplit("/", 1)[-1]
            or event.get("sessionId") != session_id
            or not isinstance(event.get("eventId"), str)
            or not event["eventId"]
        ):
            raise MemoryManagementUnavailableError("memory service receipt unavailable")
        current_revision = (
            fence_revision(record_id, {"revision": revision, "epoch": epoch, "kind": "correct", "text": text}) if epoch is not None else revision
        )
        return {
            "status": "pending",
            "phase": "event_accepted",
            "receipt_id": receipt,
            "revision": current_revision,
            "service_event_id": event["eventId"],
        }

    def forget(self, namespace, record_id, revision, *, fence=None, governance=None):
        self._current(namespace, record_id, revision, governance=governance, kind="forget")
        receipt = self.operation_receipt(namespace, record_id, revision, "forget")
        if fence is not None:
            fence("forget", record_id, revision)
        try:
            self.provider.client.delete_memory_record(memoryId=self.provider.memory_id, namespace=namespace, memoryRecordId=record_id)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("ResourceNotFoundException", "ValidationException"):
                raise ChatAuthorizationRefusedError("memory scope refused") from None
            raise MemoryManagementUnavailableError("memory service unavailable") from error
        except BotoCoreError as error:
            raise MemoryManagementUnavailableError("memory service unavailable") from error
        return {"status": "pending", "phase": "deletion_requested", "record_id": record_id, "receipt_id": receipt}

    def operation_status(self, namespace, record_id, receipt_id, fence):
        if fence["kind"] == "correct":
            visible = self.visible_receipt(namespace, receipt_id, epoch=fence["epoch"])
            if visible and visible != record_id:
                return {"status": "available", "phase": "service_record_verified", "coverage": "verified", "service_record_id": visible}
            return {"status": "pending", "phase": "extraction_pending", "coverage": "unverified", "service_record_id": None}
        try:
            response = self.provider.client.get_memory_record(memoryId=self.provider.memory_id, namespace=namespace, memoryRecordId=record_id)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return {"status": "pending", "phase": "target_absent", "coverage": "partial", "service_record_id": None}
            raise MemoryManagementUnavailableError("memory service unavailable") from error
        except BotoCoreError as error:
            raise MemoryManagementUnavailableError("memory service unavailable") from error
        self._valid(response.get("memoryRecord") if isinstance(response, dict) else None, namespace, record_id)
        return {"status": "pending", "phase": "deletion_requested", "coverage": "unverified", "service_record_id": None}
