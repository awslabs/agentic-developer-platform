"""A static projection is not admission: only a bound, live grant authorizes work."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from harness_jobs.execution import CallOutcome
from harness_jobs.identity import OperationRefused

from superplane_executor import service
from superplane_executor.handoff import (
    HANDOFF_VERSION,
    bound_operation,
    live_handoff,
    read_handoff,
)


def document(grants):
    return json.dumps({"version": HANDOFF_VERSION, "grants": grants})


def grant(operation="op-1", attempt="attempt-1", job="job-1", seconds=300):
    return {
        "operation_id": operation,
        "attempt_id": attempt,
        "job_id": job,
        "not_after": (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
    }


def operation(operation_id="op-1", attempt="attempt-1", job="job-1"):
    lease = Mock(operation_id=operation_id, attempt_id=attempt)
    return Mock(grant=Mock(lease=lease), job_id=job)


def test_operation_selector_is_refused_as_a_handoff(tmp_path):
    # The previous projection shape: it names operations but binds no attempt, job
    # or expiry. Accepting it would make a Secret reference into admission.
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(["op-1", "op-2"]))
    with pytest.raises(OperationRefused, match="not admission"):
        read_handoff(path)


@pytest.mark.parametrize(
    "body",
    [
        "{}",
        json.dumps({"version": 2, "grants": [grant()]}),
        json.dumps({"version": HANDOFF_VERSION, "grants": []}),
        json.dumps({"version": HANDOFF_VERSION, "grants": [{"operation_id": "op-1"}]}),
        "not json",
    ],
)
def test_malformed_handoff_is_refused(tmp_path, body):
    path = tmp_path / "handoff.json"
    path.write_text(body)
    with pytest.raises(OperationRefused):
        read_handoff(path)


def test_absent_handoff_is_refused(tmp_path):
    with pytest.raises(OperationRefused, match="unavailable"):
        read_handoff(tmp_path / "absent")


def test_unknown_grant_field_is_refused(tmp_path):
    # An unrecognized key must not ride along beside the validated ones.
    extra = grant() | {"scope": "*"}
    path = tmp_path / "handoff.json"
    path.write_text(document([extra]))
    with pytest.raises(OperationRefused, match="fields unsupported"):
        read_handoff(path)


def test_naive_expiry_is_refused(tmp_path):
    naive = grant() | {"not_after": "2030-01-01T00:00:00"}
    path = tmp_path / "handoff.json"
    path.write_text(document([naive]))
    with pytest.raises(OperationRefused, match="explicit offset"):
        read_handoff(path)


def test_duplicate_grant_is_refused(tmp_path):
    path = tmp_path / "handoff.json"
    path.write_text(document([grant(), grant(attempt="attempt-2")]))
    with pytest.raises(OperationRefused, match="duplicate"):
        read_handoff(path)


def test_live_bound_grant_is_accepted(tmp_path):
    path = tmp_path / "handoff.json"
    path.write_text(document([grant()]))
    handoffs = read_handoff(path)
    bound_operation(live_handoff(handoffs, "op-1"), operation())


def test_expired_grant_is_refused(tmp_path):
    path = tmp_path / "handoff.json"
    path.write_text(document([grant(seconds=-1)]))
    with pytest.raises(OperationRefused, match="expired"):
        live_handoff(read_handoff(path), "op-1")


def test_unlisted_operation_is_refused(tmp_path):
    path = tmp_path / "handoff.json"
    path.write_text(document([grant()]))
    with pytest.raises(OperationRefused, match="no run handoff"):
        live_handoff(read_handoff(path), "op-other")


@pytest.mark.parametrize(
    "resolved",
    [
        operation(attempt="attempt-other"),
        operation(job="job-other"),
        operation(operation_id="op-other"),
    ],
)
def test_grant_for_a_different_attempt_is_refused(tmp_path, resolved):
    # A copied projection must not authorize another attempt or job, even when the
    # Gateway itself reports a perfectly live lease.
    path = tmp_path / "handoff.json"
    path.write_text(document([grant()]))
    handoff = live_handoff(read_handoff(path), "op-1")
    with pytest.raises(OperationRefused, match="does not authorize"):
        bound_operation(handoff, resolved)


async def test_registry_refuses_before_asking_gateway(tmp_path):
    from superplane_executor.registry import AssignmentRegistry

    authority = AsyncMock()
    registry = AssignmentRegistry(
        domain_pool=AsyncMock(),
        execution_pool=AsyncMock(),
        authority=authority,
        instance_file=tmp_path / "instance",
        token_dir=tmp_path,
        submitter_id="submitter",
        validate_plan=AsyncMock(),
    )
    with pytest.raises(OperationRefused):
        await registry.verify("op-1")
    # No grant means the operation is never even resolved, so nothing downstream can
    # observe a half-authorized request.
    authority.resolve.assert_not_awaited()


async def test_idle_without_a_grant_opens_no_dependencies(monkeypatch, tmp_path):
    selector = tmp_path / "operations.json"
    selector.write_text(json.dumps(["op-1"]))
    handoff = tmp_path / "handoff.json"
    handoff.write_text(document([grant(seconds=-1)]))
    monkeypatch.setenv("SUPERPLANE_EXECUTION_OPERATION_FILE", str(selector))
    monkeypatch.setenv("SUPERPLANE_RUN_HANDOFF_FILE", str(handoff))
    serve = AsyncMock()
    monkeypatch.setattr(service, "serve", serve)
    waiting = asyncio.Event()

    async def wait(awaitable, timeout):
        awaitable.close()
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr(service.asyncio, "wait_for", wait)
    task = asyncio.create_task(service.run(asyncio.Event()))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # An expired grant with a populated selector is still idle, not degraded.
    serve.assert_not_called()


# ---------------------------------------------------------------------------
# Grant lifetime across the awaited authorization work
#
# `verify` awaits `resolve`, `target`, `validate_plan` and `preflight`. A grant can
# expire or be withdrawn while those run, so a single read at the start establishes
# only that authority existed *then*. These pin the re-read at the boundary where
# authority is actually conferred, and assert the provider is never reached.
# ---------------------------------------------------------------------------


def _registry(tmp_path, handoffs, *, on_preflight=None):
    """A registry whose awaited steps all succeed, so only grant state can refuse."""
    from uuid import uuid4

    from superplane_executor.registry import AssignmentRegistry

    instance = tmp_path / "instance"
    instance.write_text(str(uuid4()))
    resolved = operation()
    authority = AsyncMock()
    authority.resolve.return_value = resolved

    async def preflight(_operation):
        if on_preflight is not None:
            await on_preflight()

    authority.preflight.side_effect = preflight
    registry = AssignmentRegistry(
        domain_pool=AsyncMock(),
        execution_pool=AsyncMock(),
        authority=authority,
        instance_file=instance,
        token_dir=tmp_path / "tokens",
        submitter_id="submitter",
        validate_plan=AsyncMock(return_value={}),
        handoffs=dict(handoffs),
    )
    registry.target = AsyncMock(
        return_value={
            "controller_expires_at": datetime.now(UTC) + timedelta(seconds=300),
            "domain_org_id": str(uuid4()),
        }
    )
    return registry, resolved


def _grants(seconds=300):
    return {
        "op-1": read_handoff_grant(seconds),
    }


def read_handoff_grant(seconds):
    from superplane_executor.handoff import RunHandoff

    return RunHandoff(
        "op-1", "attempt-1", "job-1", datetime.now(UTC) + timedelta(seconds=seconds)
    )


async def test_grant_expiring_during_preflight_is_refused(tmp_path):
    # The grant is live when verification starts and lapses while preflight awaits.
    # The earlier read must not carry the authorization past its deadline.
    async def expire():
        registry.handoffs = _grants(seconds=-1)

    registry, _ = _registry(tmp_path, _grants(), on_preflight=expire)
    with pytest.raises(OperationRefused, match="expired"):
        await registry.verify("op-1")


async def test_grant_withdrawn_during_preflight_is_refused(tmp_path):
    # Withdrawal is not expiry: the document no longer grants this operation at all.
    async def withdraw():
        registry.handoffs = {}

    registry, _ = _registry(tmp_path, _grants(), on_preflight=withdraw)
    with pytest.raises(OperationRefused, match="no run handoff"):
        await registry.verify("op-1")


async def test_grant_replaced_during_preflight_is_refused(tmp_path):
    # A replacement naming a different attempt is a different authorization. It must
    # not be honoured as a continuation of the one verification started under.
    from superplane_executor.handoff import RunHandoff

    async def replace():
        registry.handoffs = {
            "op-1": RunHandoff(
                "op-1",
                "attempt-other",
                "job-1",
                datetime.now(UTC) + timedelta(seconds=300),
            )
        }

    registry, _ = _registry(tmp_path, _grants(), on_preflight=replace)
    with pytest.raises(OperationRefused, match="replaced|does not authorize"):
        await registry.verify("op-1")


async def test_grant_live_through_authorization_is_accepted(tmp_path):
    # The positive case, so the checks above are known to refuse for the intended
    # reason rather than because verification cannot succeed at all.
    registry, resolved = _registry(tmp_path, _grants())
    verified, _, _, handoff = await registry.verify("op-1")
    assert verified is resolved
    assert handoff.attempt_id == "attempt-1"


async def test_expiring_grant_authorizes_no_provider_mutation(tmp_path):
    # The consequence that matters: a grant lost during the awaited work must leave
    # the provider untouched, not merely be reported afterwards.
    from superplane_executor.provider import Provider

    async def expire():
        registry.handoffs = _grants(seconds=-1)

    registry, _ = _registry(tmp_path, _grants(), on_preflight=expire)
    sky = AsyncMock()
    provider = Provider(
        sky=sky,
        workspace=AsyncMock(),
        domain_pool=AsyncMock(),
        execution_pool=AsyncMock(),
    )
    provider.registry = registry
    call = Mock(
        operation_id="op-1",
        org_id="org",
        workspace_id="ws",
        job_id="job-1",
        attempt_id="attempt-1",
        fence_token=1,
        operation_kind="launch",
        provider="aws",
        target="target",
        idempotency_key="key",
    )
    # A refused grant is reported conservatively as UNKNOWN rather than raised, so the
    # reservation is retained. The property under test is that no provider traffic
    # happened at all: the launch was never submitted.
    outcome, _, _ = await provider(call)
    assert outcome is CallOutcome.UNKNOWN
    sky.submit.assert_not_awaited()
