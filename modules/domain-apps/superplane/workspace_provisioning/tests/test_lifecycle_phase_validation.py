"""Recovery can validate lineage without obtaining any execution authority."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from account_factory.modes import OwnershipMode
from workspace_provisioning import runtime
from workspace_provisioning.artifacts import (
    continuation_parameters,
    initial_execution_steps,
)
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_lifecycle_artifacts import artifact, Reader


def test_supplied_managed_runtime_refuses_before_authority_or_worker_state(
    monkeypatch, tmp_path
):
    async def validated(operation, context):
        return (
            {"workspace_variables": {"networking_mode": "supplied"}},
            SimpleNamespace(mode=OwnershipMode.EXISTING_ACCOUNT_MANAGED),
            None,
            None,
            None,
        )

    async def forbidden(*args, **kwargs):
        raise AssertionError("unsupported managed runtime must not obtain authority")

    monkeypatch.setattr(runtime, "validate_phase", validated)
    monkeypatch.setattr(runtime, "current_operation", forbidden)
    monkeypatch.setattr(runtime, "delivery_session", forbidden)
    context = SimpleNamespace(state_root=tmp_path / "never-created")
    with pytest.raises(LifecycleRefused, match="requires owned networking"):
        asyncio.run(runtime.run_lifecycle(object(), context))
    assert list(tmp_path.iterdir()) == []


def setup(monkeypatch, row):
    markers = ({"runtime": "approved"}, object(), object())
    monkeypatch.setattr(
        runtime, "validated_request", lambda operation, context: markers
    )
    parameters = continuation_parameters(row)
    operation = SimpleNamespace(
        request=SimpleNamespace(parameters=parameters),
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                org_id=row["org_id"], workspace_id=row["workspace_id"]
            )
        ),
    )
    source = {
        "state": "succeeded",
        "org_id": row["org_id"],
        "workspace_id": row["workspace_id"],
        "job_id": row["source_job_id"],
        "attempt_id": row["source_attempt_id"],
        "plan_digest": row["source_payload_digest"],
        "request_payload": row["source_request_payload"],
    }

    class Shared:
        async def fetchrow(self, sql, operation_id):
            assert operation_id == row["source_operation_id"]
            return source

    @asynccontextmanager
    async def connect():
        yield Shared()

    class NoAuthority:
        def __getattr__(self, name):
            raise AssertionError(
                "pure validation must not resolve/deliver/preflight: " + name
            )

    context = SimpleNamespace(
        connect=connect, domain_connect=Reader(row).connect, authority=NoAuthority()
    )
    return operation, context, source, markers


def test_exact_source_lineage_without_execution_resolution(monkeypatch):
    row = artifact()
    operation, context, _, markers = setup(monkeypatch, row)
    result = asyncio.run(runtime.validate_phase(operation, context))
    assert result[:3] == markers
    assert result[3] == row
    assert result[4].step_id == "apply-infrastructure"


@pytest.mark.parametrize(
    "key",
    [
        "max_cost_micros",
        "allocation_id",
        "lifecycle_policy_sha256",
        "credential_id",
        "lifecycle_request",
        "execution_steps",
    ],
)
def test_changed_continuation_cannot_reuse_original_proposal(monkeypatch, key):
    operation, context, _, _ = setup(monkeypatch, artifact())
    operation.request.parameters[key] = "changed"
    with pytest.raises(LifecycleRefused, match="continuation differs"):
        asyncio.run(runtime.validate_phase(operation, context))


@pytest.mark.parametrize(
    "key",
    [
        "job_id",
        "attempt_id",
        "org_id",
        "workspace_id",
        "plan_digest",
        "request_payload",
        "state",
    ],
)
def test_source_must_be_completed_original_admission(monkeypatch, key):
    operation, context, source, _ = setup(monkeypatch, artifact())
    source[key] = "changed"
    with pytest.raises(LifecycleRefused, match="completed original"):
        asyncio.run(runtime.validate_phase(operation, context))


def test_historical_recovery_does_not_extend_execution_proposal_expiry(monkeypatch):
    row = artifact()
    row["created_at"] -= timedelta(hours=2)
    operation, context, _, _ = setup(monkeypatch, row)
    with pytest.raises(LifecycleRefused, match="expired"):
        asyncio.run(runtime.validate_phase(operation, context))
    assert (
        asyncio.run(runtime.validate_phase(operation, context, require_fresh=False))[3]
        == row
    )


def test_initial_execution_has_only_the_exact_described_phase(monkeypatch):
    row = artifact()
    operation, context, _, _ = setup(monkeypatch, row)
    parameters = json.loads(row["parameters_json"])
    parameters["execution_steps"] = initial_execution_steps(parameters)
    operation.request.parameters = parameters
    assert (
        asyncio.run(runtime.validate_phase(operation, context))[4].step_id
        == "prepare-infrastructure"
    )
    parameters["execution_steps"] = "[]"
    with pytest.raises(LifecycleRefused, match="initial lifecycle descriptors"):
        asyncio.run(runtime.validate_phase(operation, context))
