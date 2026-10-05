"""Worker adapter for dedicated validation; no cluster credentials or commands."""

import time
import uuid

from lib.codex_validation import ValidationUnavailable, ValidationCancelled
from lib.task_tool_artifacts import publish_host_json


class ServiceValidationExecutor:
    def __init__(self, *, client, attempt, workspace=None):
        self.client, self.attempt, self.workspace = client, attempt, workspace

    def _call(self, operation, operation_id=None, payload=None):
        body = {"schema_version": "1.0", "attempt": self.attempt, "operation": operation,
                "operation_id": operation_id or str(uuid.uuid4())}
        if payload is not None:
            body["payload"] = payload
        receipt = self.client.validation_service(body)
        if receipt.get("schema_version") != "1.0":
            raise ValidationUnavailable("Validation service receipt invalid")
        return receipt

    def _boundary(self):
        if self._call("inspect").get("idle") is not True:
            raise ValidationUnavailable("Validation service has unresolved prior work")

    def recover(self):
        receipt = self._call("cancel_jobs")
        return receipt.get("phase") == "cancelled" and receipt.get("pending") == []

    def run_repository(self, *, check, repository, expected_head, cancelled):
        if self.workspace is None or self.workspace.root != repository:
            raise ValidationUnavailable("Validation service workspace unavailable")
        if cancelled.is_set():
            raise ValidationCancelled("Validation stopped before admission")
        manifest = self.workspace.export_changes(expected_head=expected_head, allow_empty=True)
        artifact = publish_host_json(self.client, attempt=self.attempt, result=manifest)
        operation = str(uuid.uuid4())
        payload = {"check": check.name, **{key: artifact[key] for key in ["artifact_id", "content_sha256", "byte_length"]}}
        deadline = time.monotonic() + 165
        receipt = self._call("run", operation, payload)
        while True:
            if cancelled.is_set():
                if self.recover():
                    raise ValidationCancelled("Validation termination confirmed")
                raise ValidationUnavailable("Validation cancellation remains pending")
            if receipt.get("operation_id") != operation:
                raise ValidationUnavailable("Validation service operation differs")
            phase = receipt.get("phase")
            if phase == "completed":
                result = receipt.get("result")
                if (not isinstance(result, dict) or result.get("commit") != expected_head
                        or result.get("tree") != manifest["tree"] or result.get("check") != check.name):
                    raise ValidationUnavailable("Validation service source receipt differs")
                return result
            if phase == "cancelled":
                raise ValidationCancelled("Validation service stopped execution")
            if phase not in {"pending", "running"} or time.monotonic() >= deadline:
                raise ValidationUnavailable("Validation service execution outcome unconfirmed")
            cancelled.wait(1)
            receipt = self._call("status", operation)
