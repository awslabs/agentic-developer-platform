"""Worker service adapter never exports check commands or trusts a wrong tree."""

from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from lib import codex_service_validation as module
from lib.codex_validation import ValidationCheck, ValidationUnavailable, ValidationCancelled


@pytest.fixture
def adapter(monkeypatch):
    calls = []
    result = {"check": "unit", "commit": "a" * 40, "tree": "b" * 40, "status": "passed"}
    phases = ["pending", "completed"]
    def service(body):
        calls.append(body)
        if body["operation"] == "inspect":
            return {"schema_version": "1.0", "idle": True}
        if body["operation"] == "cancel_jobs":
            return {"schema_version": "1.0", "phase": "cancelled", "pending": []}
        return {"schema_version": "1.0", "operation_id": body["operation_id"], "phase": phases.pop(0), "result": result}
    artifact = {"artifact_id": "fixture", "content_sha256": "c" * 64, "byte_length": 100}
    monkeypatch.setattr(module, "publish_host_json", lambda *args, **kwargs: artifact)
    workspace = SimpleNamespace(root=Path("/repo"), export_changes=lambda **kwargs: {"tree": "b" * 40})
    executor = module.ServiceValidationExecutor(client=SimpleNamespace(validation_service=service), attempt={"fixture": True}, workspace=workspace)
    check = ValidationCheck(name="unit", image="registry.example/check@sha256:" + "a" * 64, argv=("check",))
    return executor, calls, result, check


def test_only_manifest_reference_and_check_name_leave_worker(adapter):
    executor, calls, result, check = adapter
    executor._boundary()
    assert executor.run_repository(check=check, repository=Path("/repo"), expected_head="a" * 40, cancelled=threading.Event()) == result
    assert calls[1]["payload"] == {"check": "unit", "artifact_id": "fixture", "content_sha256": "c" * 64, "byte_length": 100}
    assert calls[1]["operation_id"] == calls[2]["operation_id"]
    assert executor.recover()


def test_wrong_tree_never_becomes_a_validation_receipt(adapter):
    executor, _, result, check = adapter
    result["tree"] = "d" * 40
    with pytest.raises(ValidationUnavailable):
        executor.run_repository(check=check, repository=Path("/repo"), expected_head="a" * 40, cancelled=threading.Event())


def test_stop_before_admission_creates_no_remote_job(adapter):
    executor, calls, _, check = adapter
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ValidationCancelled):
        executor.run_repository(check=check, repository=Path("/repo"), expected_head="a" * 40, cancelled=cancelled)
    assert calls == []
