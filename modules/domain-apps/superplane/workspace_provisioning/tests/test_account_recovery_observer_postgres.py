"""Real scoped recovery grants authorize request-only reads, never execution."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from harness_jobs.execution import CallOutcome
from harness_jobs.recovery import sweep_scoped_expired_leases
from harness_jobs.recovery_grant import RecoveryGrant
from workspace_provisioning import account_creation
from workspace_provisioning.account_recovery_observer import observe_account_creation
from workspace_provisioning.runtime_config import LifecycleRefused

from .postgres_bridge import requires_harness_postgres
from .test_account_creation_postgres import (
    AccountScenario,
    harness as lifecycle_harness,
)

pytestmark = requires_harness_postgres
harness = lifecycle_harness


async def interrupted_creation(scenario, monkeypatch):
    async def interrupted(*args, **kwargs):
        raise OSError("worker stopped before account handoff publication")

    operation = await scenario.admit(scenario.parameters)
    with monkeypatch.context() as isolated:
        isolated.setattr(account_creation, "publish_created_account", interrupted)
        with pytest.raises(Exception):
            await account_creation.run_account_creation(operation, scenario.context)
    async with scenario.harness.connect() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE operation_id=$1",
            operation.grant.lease.operation_id,
        )
    return operation


@pytest.mark.parametrize("status", ["SUCCEEDED", "IN_PROGRESS", "FAILED"])
def test_actual_recovery_grant_observes_recorded_creation_without_settlement(
    harness, tmp_path, monkeypatch, status
):
    scenario = AccountScenario(harness, tmp_path, monkeypatch)

    async def run():
        original = await interrupted_creation(scenario, monkeypatch)
        principal = replace(
            original.grant.principal,
            subject="authenticated-recovery-run",
            permissions=frozenset({"workspace:provision", "workspace:recover"}),
        )
        facts, reads = [], []

        class Provider:
            async def aws_read(self, service, method, **arguments):
                reads.append((service, method, arguments))
                if (service, method) == ("sts", "get_caller_identity"):
                    return {"Account": "000000000001"}
                assert (service, method, arguments) == (
                    "organizations",
                    "describe_create_account_status",
                    {"CreateAccountRequestId": "car-fixture"},
                )
                return {
                    "CreateAccountStatus": {
                        "Id": "car-fixture",
                        "AccountName": "adp-ws-1",
                        "State": status,
                        **(
                            {"AccountId": "000000000003"}
                            if status == "SUCCEEDED"
                            else {}
                        ),
                        **(
                            {"FailureReason": "CONCURRENT_ACCOUNT_MODIFICATION"}
                            if status == "FAILED"
                            else {}
                        ),
                    }
                }

        async def observe(lease, key, provider, kind, target):
            assert lease.fence_token > original.grant.lease.fence_token
            operation = SimpleNamespace(
                **{**vars(original), "grant": RecoveryGrant(principal, lease)}
            )
            async with harness.connect() as connection:
                before = await connection.fetchrow(
                    "SELECT * FROM harness_provider_call_intent WHERE idempotency_key=$1",
                    key,
                )
            result = await observe_account_creation(
                operation, scenario.context, idempotency_key=key, provider=Provider()
            )
            async with harness.connect() as connection:
                assert (
                    await connection.fetchrow(
                        "SELECT * FROM harness_provider_call_intent WHERE idempotency_key=$1",
                        key,
                    )
                    == before
                )
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM workspace_lifecycle_artifacts"
                    )
                    == 0
                )
            facts.append(result)
            # The outer regression sweep deliberately retains all outcomes. The
            # production helper returned facts only and did not settle anything.
            return CallOutcome.UNKNOWN, "observation facts only", None

        async with harness.connect() as connection:
            report = await sweep_scoped_expired_leases(
                connection,
                principal=principal,
                candidates=frozenset({original.grant.lease.operation_id}),
                observe_claim=observe,
            )
        assert report.results and len(facts) == 1
        assert facts[0]["creation_status"] == status.lower().replace("_", "-")
        assert facts[0]["creation_request_id"] == "car-fixture"
        assert facts[0]["account_id"] == (
            "000000000003" if status == "SUCCEEDED" else None
        )
        assert (
            facts[0]["release_permitted"] is False
            and facts[0]["bootstrap_verified"] is False
        )
        assert len(reads) == 2 and len(scenario.accounts) == 1
        assert scenario.assumed_roles == ["arn:aws:iam::000000000001:role/provider"]

    harness.run(run())


@pytest.mark.parametrize(
    "failure",
    [
        "execution-grant",
        "expired-claim",
        "revoked-subject",
        "wrong-request",
        "management-account",
        "missing-id",
        "changed-call",
    ],
)
def test_account_recovery_refuses_stale_authority_and_changed_original_identity(
    harness, tmp_path, monkeypatch, failure
):
    scenario = AccountScenario(harness, tmp_path, monkeypatch)
    scenario.lose_create_reply = failure == "missing-id"

    async def run():
        original = await interrupted_creation(scenario, monkeypatch)
        principal = replace(
            original.grant.principal,
            subject="authenticated-recovery-run",
            permissions=frozenset({"workspace:provision", "workspace:recover"}),
        )
        refused, reads = [], []

        async def observe(lease, key, provider, kind, target):
            operation = SimpleNamespace(
                **{**vars(original), "grant": RecoveryGrant(principal, lease)}
            )
            if failure == "execution-grant":
                operation = original

            class Provider:
                async def aws_read(self, service, method, **arguments):
                    reads.append((service, method))
                    if failure in {"expired-claim", "revoked-subject", "changed-call"}:
                        async with harness.connect() as connection:
                            if failure == "expired-claim":
                                await connection.execute(
                                    "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1",
                                    lease.operation_id,
                                )
                            elif failure == "revoked-subject":
                                await connection.execute(
                                    "DELETE FROM harness_recovery_claim_bindings WHERE operation_id=$1",
                                    lease.operation_id,
                                )
                            else:
                                await connection.execute(
                                    "UPDATE harness_provider_call_intent SET provider_ref='request=car-other' WHERE idempotency_key=$1",
                                    key,
                                )
                    if service == "sts":
                        return {
                            "Account": "000000000009"
                            if failure == "management-account"
                            else "000000000001"
                        }
                    assert (service, method) == (
                        "organizations",
                        "describe_create_account_status",
                    )
                    return {
                        "CreateAccountStatus": {
                            "Id": "car-other"
                            if failure == "wrong-request"
                            else "car-fixture",
                            "AccountName": "adp-ws-1",
                            "State": "SUCCEEDED",
                            "AccountId": "000000000003",
                        }
                    }

            with pytest.raises(LifecycleRefused):
                await observe_account_creation(
                    operation,
                    scenario.context,
                    idempotency_key=key,
                    provider=Provider(),
                )
            refused.append(True)
            return CallOutcome.UNKNOWN, "refused observation", None

        async with harness.connect() as connection:
            await sweep_scoped_expired_leases(
                connection,
                principal=principal,
                candidates=frozenset({original.grant.lease.operation_id}),
                observe_claim=observe,
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 0
            )
        assert refused == [True] and len(scenario.accounts) == 1
        if failure in {"execution-grant", "missing-id"}:
            assert reads == []

    harness.run(run())
