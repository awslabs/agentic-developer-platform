"""Management account observation is read-only and bound to the original request."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services import account_creation_recovery as recovery
from .test_lifecycle_recovery_provider import sessions


async def test_account_provider_uses_separate_management_role_and_only_three_reads(
    monkeypatch,
):
    sts, _, factory = sessions(monkeypatch)
    monkeypatch.setenv(
        "SUPERPLANE_ACCOUNT_RECOVERY_OBSERVATION_ROLE_ARN",
        "arn:aws:iam::123456789012:role/account-recovery",
    )
    async with recovery.observation_provider(
        account_id="123456789012",
        region="us-east-1",
        current=AsyncMock(),
    ) as provider:
        await provider.aws_read("sts", "get_caller_identity")
        for service, method in (
            ("organizations", "create_account"),
            ("organizations", "move_account"),
            ("sts", "assume_role"),
            ("eks", "describe_cluster"),
        ):
            with pytest.raises(HTTPException):
                await provider.aws_read(service, method)
        provider.reads = 3
        with pytest.raises(HTTPException):
            await provider.aws_read("sts", "get_caller_identity")
    assert sts.assume_role.call_args.kwargs["RoleArn"].endswith("role/account-recovery")
    policy = json.loads(sts.assume_role.call_args.kwargs["Policy"])
    assert policy["Statement"][0]["Action"] == sorted(recovery.READS.values())
    assert factory.call_count == 2


async def test_account_provider_cannot_fall_back_to_lifecycle_or_child_role(
    monkeypatch,
):
    _, _, factory = sessions(monkeypatch)
    monkeypatch.delenv(
        "SUPERPLANE_ACCOUNT_RECOVERY_OBSERVATION_ROLE_ARN", raising=False
    )
    for role in (None, "arn:aws:iam::999999999999:role/child"):
        if role:
            monkeypatch.setenv("SUPERPLANE_ACCOUNT_RECOVERY_OBSERVATION_ROLE_ARN", role)
        with pytest.raises(HTTPException):
            async with recovery.observation_provider(
                account_id="123456789012",
                region="us-east-1",
                current=AsyncMock(),
            ):
                pytest.fail("wrong management authority")
    factory.assert_not_called()


@pytest.mark.parametrize("status", ["in-progress", "succeeded", "failed"])
@pytest.mark.parametrize(
    "change",
    [
        None,
        "request",
        "call",
        "digest",
        "phase",
        "claim",
        "release",
        "bootstrap",
        "child",
        "root",
    ],
)
async def test_account_transport_requires_original_request_without_result_artifact(
    status, change
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
        assert path.endswith("/recovery/account-creation")
        assert set(body) == {"claim", "query_id", "idempotency_key"}
        facts = {
            "observation_only": True,
            "original_call_key": body["idempotency_key"],
            "plan_digest": "b" * 64,
            "creation_request_id": "car-original",
            "creation_status": status,
            "bootstrap_verified": False,
            "release_permitted": False,
            "account_id": "123456789012" if status == "succeeded" else None,
            **(
                {"creation_source_parent_id": "r-root"} if status == "succeeded" else {}
            ),
        }
        result = {
            **body,
            "version": 1,
            "observation_only": True,
            "checked_at": datetime.now(UTC).isoformat(),
            "plan_digest": "b" * 64,
            "phase": "create-account",
            "facts": facts,
        }
        if change == "request":
            facts["creation_request_id"] = "car-other"
        elif change == "call":
            facts["original_call_key"] = "another-call"
        elif change == "digest":
            result["plan_digest"] = "c" * 64
        elif change == "phase":
            result["phase"] = "apply-infrastructure"
        elif change == "claim":
            result["claim"] = {**body["claim"], "fence_token": 3}
        elif change in {"release", "bootstrap"}:
            facts[
                "release_permitted" if change == "release" else "bootstrap_verified"
            ] = True
        elif change == "child":
            facts["account_id"] = "invalid"
        elif change == "root" and status == "succeeded":
            facts["creation_source_parent_id"] = "ou-changed"
        return result

    authority = RecoveryAuthority(SimpleNamespace(post=AsyncMock(side_effect=response)))
    authority.resolve_recovery = AsyncMock()
    result = authority.account_creation(
        lease,
        "original-call",
        plan_digest="b" * 64,
        request_id="car-original",
    )
    if change is None or change == "root" and status != "succeeded":
        assert (await result)["creation_status"] == status
    else:
        with pytest.raises(OperationRefused):
            await result
