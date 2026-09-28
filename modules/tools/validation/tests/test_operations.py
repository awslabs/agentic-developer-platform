"""Artifact integrity and owner execution/termination behavior."""

import base64
from contextlib import contextmanager
import hashlib
import json
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest

from validation_tools.operations import ValidationRequest, execute_job, read_manifest


def inputs(identity):
    attempt = {"run": {key: getattr(identity, key) for key in ["task_id", "invocation_id", "generation"]},
               "runtime_attempt_id": identity.runtime_attempt_id}
    content = b'{"tree":"example"}'
    request = ValidationRequest(check="unit", artifact_id="art_" + str(uuid.uuid4()),
        content_sha256=hashlib.sha256(content).hexdigest(), byte_length=len(content))
    receipt = {"schema_version": "1.0", "operation": "read", "run": attempt["run"],
        **request.model_dump(exclude={"check"}), "content_type": "application/json",
        "content_base64": base64.b64encode(content).decode()}
    return attempt, request, receipt


@pytest.mark.parametrize("field,value", [("artifact_id", "other"), ("run", {}), ("content_type", "text/plain"),
    ("content_sha256", "0" * 64), ("byte_length", 999), ("content_base64", "e30=")])
def test_artifact_receipt_must_match_exact_admission(jobs, field, value):
    _, identity, _ = jobs
    attempt, request, receipt = inputs(identity)
    receipt[field] = value
    authority = SimpleNamespace(post=lambda *args: receipt)
    with pytest.raises(HTTPException):
        read_manifest(authority, attempt, request)


@pytest.mark.parametrize("mode,phase", [("success", "completed"), ("failure", "cancelled"),
    ("lost_cleanup", "unknown"), ("close", "cancelled")])
def test_execute_once_and_preserve_uncertain_cleanup(jobs, mode, phase):
    store, identity, _ = jobs
    attempt, request, receipt = inputs(identity)
    operation = str(uuid.uuid4())
    store.admit(identity, operation, request.model_dump())
    calls = []
    class Executor:
        def recover(self):
            calls.append("recover")
            if mode == "lost_cleanup":
                raise RuntimeError("unconfirmed")
    @contextmanager
    def factory(task_id):
        assert task_id == identity.task_id
        yield Executor()
    def runner(**kwargs):
        assert kwargs["manifest"] == json.loads(base64.b64decode(receipt["content_base64"]))
        calls.append("run")
        if mode == "close":
            store.close(identity)
        if mode in {"failure", "lost_cleanup"}:
            raise RuntimeError("check failed before receipt")
        return {"status": "passed"}
    authority = SimpleNamespace(post=lambda *args: receipt, authorize=lambda **kwargs: SimpleNamespace(identity=identity))
    for _ in range(2):
        execute_job(jobs=store, authority=authority, attempt=attempt, identity=identity, operation_id=operation,
            executor_factory=factory, remaining_ms=lambda: 180000, runner=runner)
    assert calls.count("run") == 1
    assert store.read(identity, operation)["phase"] == phase
    assert store.close(identity) == ([operation] if phase == "unknown" else [])
