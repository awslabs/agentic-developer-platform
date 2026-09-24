"""Trusted observer cannot expose execution methods or continue after revocation."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.services import lifecycle_recovery as recovery


def sessions(monkeypatch, *, account="123456789012"):
    monkeypatch.setenv(
        "SUPERPLANE_RECOVERY_OBSERVATION_ROLE_ARN",
        f"arn:aws:iam::{account}:role/recovery",
    )
    sts, read = Mock(), Mock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "fixture-access",
            "SecretAccessKey": "fixture-secret",
            "SessionToken": "fixture-session",
        }
    }
    read.get_caller_identity.return_value = {"Account": account}
    base = SimpleNamespace(client=Mock(return_value=sts))
    assumed = SimpleNamespace(client=Mock(return_value=read))
    factory = Mock(side_effect=[base, assumed])
    factory.sessions = (base, assumed)
    monkeypatch.setattr("boto3.Session", factory)
    return sts, read, factory


async def test_lifecycle_provider_has_exact_read_policy_and_bounded_no_retry_clients(
    monkeypatch,
):
    sts, read, factory = sessions(monkeypatch)
    current = AsyncMock()
    async with recovery.observation_provider(
        account_id="123456789012", region="us-east-1", current=current
    ) as provider:
        assert await provider.aws_read("sts", "get_caller_identity") == {
            "Account": "123456789012"
        }
        for service, method in [
            ("sts", "assume_role"),
            ("eks", "create_access_entry"),
            ("ec2", "run_instances"),
        ]:
            with pytest.raises(HTTPException):
                await provider.aws_read(service, method)
        assert not hasattr(provider, "delivery_role")
        assert not hasattr(provider, "session")
        provider.reads = 64
        with pytest.raises(HTTPException):
            await provider.aws_read("sts", "get_caller_identity")
    read.get_caller_identity.assert_called_once_with()
    assert current.await_count == 4
    policy = json.loads(sts.assume_role.call_args.kwargs["Policy"])
    assert policy["Statement"][0]["Action"] == sorted(recovery.READS.values())
    assert not any("*" in action for action in policy["Statement"][0]["Action"])
    assert sts.assume_role.call_args.kwargs["DurationSeconds"] == 900
    # The only credential-bearing return is consumed inside the API's local
    # closure; the worker's HTTP transport receives only observed facts.
    assert factory.call_count == 2
    for session in factory.sessions:
        config = session.client.call_args.kwargs["config"]
        assert config.retries == {"total_max_attempts": 1}
        assert config.connect_timeout == config.read_timeout == 3
        assert config.ignore_configured_endpoint_urls is True


@pytest.mark.parametrize(
    "change",
    [
        None,
        "artifact",
        "digest",
        "phase",
        "key",
        "query",
        "claim",
        "stale",
        "mutable",
        "facts",
    ],
)
async def test_worker_observation_transport_binds_only_original_claim_and_result(
    change,
):
    from harness_jobs.identity import OperationRefused
    from superplane_executor.recovery_authority import RecoveryAuthority

    lease = SimpleNamespace(
        operation_id="operation",
        org_id="org",
        workspace_id="workspace",
        holder="recovery",
        attempt_id="attempt",
        fence_token=2,
    )

    async def response(path, body):
        assert path.endswith("/recovery/lifecycle")
        assert set(body) == {"claim", "query_id", "idempotency_key"}
        result = {
            **body,
            "version": 1,
            "observation_only": True,
            "checked_at": datetime.now(UTC).isoformat(),
            "result_artifact_id": "a" * 64,
            "plan_digest": "b" * 64,
            "phase": "apply-infrastructure",
            "facts": {"provider_snapshot": {"cluster_arn": "original"}},
        }
        if change in {"artifact", "digest", "phase", "key", "query"}:
            field = {
                "artifact": "result_artifact_id",
                "digest": "plan_digest",
                "phase": "phase",
                "key": "idempotency_key",
                "query": "query_id",
            }[change]
            result[field] = "different"
        elif change == "claim":
            result["claim"] = {**body["claim"], "fence_token": 3}
        elif change == "stale":
            result["checked_at"] = (
                datetime.now(UTC) - timedelta(minutes=1)
            ).isoformat()
        elif change == "mutable":
            result["observation_only"] = False
        elif change == "facts":
            result["facts"] = ["invalid"]
        return result

    authority = RecoveryAuthority(SimpleNamespace(post=AsyncMock(side_effect=response)))
    authority.resolve_recovery = AsyncMock()
    call = authority.lifecycle(
        lease,
        "original-key",
        artifact_id="a" * 64,
        plan_digest="b" * 64,
        phase="apply-infrastructure",
    )
    if change is None:
        assert await call == {"provider_snapshot": {"cluster_arn": "original"}}
        authority.resolve_recovery.assert_awaited_once_with(lease)
    else:
        with pytest.raises(OperationRefused):
            await call


async def test_changed_observation_role_account_never_resolves_a_session(monkeypatch):
    _, _, factory = sessions(monkeypatch)
    with pytest.raises(HTTPException):
        async with recovery.observation_provider(
            account_id="999999999999", region="us-east-1", current=AsyncMock()
        ):
            pytest.fail("foreign account must be refused")
    factory.assert_not_called()


async def test_revocation_after_read_prevents_any_observation_result(monkeypatch):
    _, read, _ = sessions(monkeypatch)
    current = AsyncMock(side_effect=[None, None, None, HTTPException(403, "revoked")])
    with pytest.raises(HTTPException):
        async with recovery.observation_provider(
            account_id="123456789012", region="us-east-1", current=current
        ) as provider:
            await provider.aws_read("sts", "get_caller_identity")
    read.get_caller_identity.assert_called_once_with()


def test_oversized_or_nonobject_observations_are_refused():
    for value in ({"unbounded": "x" * 128}, ["facts"]):
        with pytest.raises(HTTPException):
            recovery.bounded_json(value, 100)


@pytest.mark.parametrize(
    "change", ["renewed", "fence", "principal", "runtime-deadline"]
)
async def test_live_observation_accepts_renewal_but_refuses_changed_claim(
    monkeypatch, change
):
    from harness_jobs.identity import ResolvedPrincipal
    from harness_jobs.leases import ExecutionLease
    from harness_jobs.recovery_grant import RecoveryGrant
    from superplane_executor.authority import VerifiedOperation
    from workspace_provisioning import recovery_observer

    now = datetime.now(UTC)
    lease = ExecutionLease(
        "operation",
        "org",
        "workspace",
        "recovery-holder",
        2,
        "recovery-attempt",
        now + timedelta(seconds=30),
        now,
        now + timedelta(minutes=10),
        1,
    )
    principal = ResolvedPrincipal(
        "org", "workspace", "recovery-run#1", frozenset({"workspace:recover"})
    )
    operation = VerifiedOperation(
        RecoveryGrant(principal, lease), "job", "a" * 64, "{}", "confirmed", 1, 900, 100
    )
    renewed = replace(lease, expires_at=now + timedelta(seconds=60))
    if change == "fence":
        renewed = replace(renewed, fence_token=3)
    if change == "runtime-deadline":
        renewed = replace(renewed, runtime_deadline=now + timedelta(minutes=20))
    actor = (
        replace(principal, subject="different-run#1")
        if change == "principal"
        else principal
    )
    latest = replace(operation, grant=RecoveryGrant(actor, renewed))
    artifact = {"artifact_id": "b" * 64, "account_id": "123456789012"}
    source, context = SimpleNamespace(region="us-east-1"), object()
    calls = 0

    async def resolve(*_):
        nonlocal calls
        calls += 1
        return operation if calls == 1 else latest, context, artifact, source

    @asynccontextmanager
    async def provider(**arguments):
        await arguments["current"]()
        yield object()

    monkeypatch.setattr(recovery, "lifecycle_context", resolve)
    monkeypatch.setattr(recovery, "observation_provider", provider)
    monkeypatch.setattr(
        recovery_observer,
        "observe_result",
        AsyncMock(return_value={"provider_snapshot": {}}),
    )
    body = SimpleNamespace(idempotency_key="original-step")
    if change == "renewed":
        result = await recovery.observe_lifecycle(object(), body)
        assert result["result_artifact_id"] == artifact["artifact_id"]
        assert calls == 3
    else:
        with pytest.raises(HTTPException):
            await recovery.observe_lifecycle(object(), body)
        recovery_observer.observe_result.assert_not_called()
