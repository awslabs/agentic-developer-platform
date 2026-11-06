"""A receipt selects fresh scratch without touching the stopped attempt's files."""

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest
from harness_jobs.identity import OperationRefused

from workspace_provisioning.bootstrap_retry import retry_directory
from workspace_provisioning.runtime_config import LifecycleRefused
from workspace_provisioning.terraform import operation_directory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", [None, "scope", "key", "fence", "workspace", "artifact", "mutation"]
)
async def test_retry_directory_preserves_original_and_refuses_substitution(
    tmp_path, monkeypatch, fault
):
    original = operation_directory(
        tmp_path, "org", "workspace", "operation", create=True
    )
    original.joinpath("retained-original").write_text("never overwrite")
    lease = SimpleNamespace(
        org_id="org", workspace_id="workspace", operation_id="operation", fence_token=3
    )
    operation = SimpleNamespace(
        grant=SimpleNamespace(lease=lease),
        plan_digest="plan",
        request=SimpleNamespace(parameters={"lifecycle_artifact_id": "a" * 64}),
    )
    proof = {
        "evidence_kind": "bootstrap-journal-barrier-v1",
        "bootstrap_mutations_started": False,
        "source_artifact_id": "a" * 64,
        "source_artifact_sha256": "b" * 64,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "owner_evidence_id": "owner-proof",
        "owner_evidence": {
            "record_id": "owner-proof",
            "evidence_kind": "bootstrap-journal-barrier-v1",
            "source_image": "qualified-image",
            "source_hashes": {"runtime": "qualified-source"},
        },
    }
    receipt = dict(
        org_id="org",
        workspace_id="workspace",
        operation_id="operation",
        plan_digest="plan",
        source_fence=1,
        source_attempt_id="source-attempt",
        original_key="original-key",
        replacement_key="retry-key",
        recovery_scope="unstarted-bootstrap",
        reauthorization_id="fresh-decision",
    )
    original_call = dict(
        operation_id="operation",
        org_id="org",
        workspace_id="workspace",
        stage="reconciled",
        outcome="failed",
        provider_ref=None,
        fence_token=1,
        attempt_id="source-attempt",
        job_id="job",
        provider="superplane-lifecycle",
        operation_kind="bootstrap-workspace",
        target="a" * 64,
    )
    receipt["source_intent"] = json.dumps(original_call)
    if fault in {"scope", "key", "workspace"}:
        receipt[
            {
                "scope": "recovery_scope",
                "key": "replacement_key",
                "workspace": "workspace_id",
            }[fault]
        ] = "foreign"
    elif fault == "fence":
        receipt["source_fence"] = 3
    elif fault == "artifact":
        proof["source_artifact_id"] = "foreign"
    elif fault == "mutation":
        proof["bootstrap_mutations_started"] = True
    receipt["proof"] = json.dumps(proof)

    class Connection:
        async def fetch(self, query, operation_id):
            assert "harness_provider_retries" in query
            assert operation_id == "operation"
            return [receipt]

        async def fetchrow(self, query, key):
            assert "harness_provider_call_intent" in query
            assert key == "original-key"
            return original_call

    @asynccontextmanager
    async def connect():
        yield Connection()

    async def read(connection, operation_id):
        assert operation_id == "operation"
        return receipt

    monkeypatch.setattr("workspace_provisioning.bootstrap_retry.receipt_for", read)
    context = SimpleNamespace(connect=connect, state_root=tmp_path)
    call = SimpleNamespace(idempotency_key="retry-key")
    if fault:
        with pytest.raises((LifecycleRefused, OperationRefused)):
            await retry_directory(operation, context, call)
        assert list(original.parent.iterdir()) == [original]
    else:
        retry = await retry_directory(operation, context, call)
        assert retry != original and list(retry.iterdir()) == []
        assert set(original.parent.iterdir()) == {original, retry}
        with pytest.raises(FileExistsError):
            await retry_directory(operation, context, call)
    assert original.joinpath("retained-original").read_text() == "never overwrite"
