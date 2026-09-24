"""Original account request -> protected HTTP read -> shared recovery -> bootstrap.

The database, admission, claim acquisition, HTTP route, observer, immutable
handoff, settlement receipt and maintained bootstrap are real. Cloud transport
and authenticated network identity are test inputs. Runs only in disposable CI.
"""

# The canonical transport fixtures are checkout test sources, not modules in the
# deployed superplane-bootstrap wheel. API-only collection starts below the
# module root, so it must declare this test-source path independently.
# ruff: noqa: E402

from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request

MODULE_TEST_ROOT = Path(__file__).resolve().parents[3]
if str(MODULE_TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_TEST_ROOT))

from harness_jobs.recovery_grant import lock_recovery_grant
from superplane_executor.recovery_authority import RecoveryAuthority, claim_identity
from workspace_provisioning import account_creation, account_runtime
from workspace_provisioning.artifacts import canonical, continuation_parameters
from workspace_provisioning.recovery import LifecycleRecovery
from workspace_provisioning.tests.test_account_bootstrap_postgres import Child
from workspace_provisioning.tests.test_account_canonical_postgres import (
    CanonicalScenario,
)
from workspace_provisioning.tests.test_bootstrap_runtime_postgres import (
    bootstrap_harness as canonical_harness,
)

from app.config import settings
from app.routers import controller_recovery as routes
from app.services import account_creation_recovery as service

account_harness = canonical_harness


async def accepted_without_artifact(scenario):
    scenario.expire_after_accept = True
    original_create = scenario.management.create_account

    def pending(**arguments):
        result = original_create(**arguments)
        result["CreateAccountStatus"].update(State="IN_PROGRESS")
        result["CreateAccountStatus"].pop("AccountId", None)
        return result

    scenario.management.create_account = pending
    operation = await scenario.admit(scenario.parameters)
    with pytest.raises(Exception):
        await account_creation.run_account_creation(operation, scenario.context)
    lease = operation.grant.lease
    async with scenario.harness.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM workspace_lifecycle_artifacts"
            )
            == 0
        )
        await connection.execute(
            "INSERT INTO workspaces(id,org_id,name,isolation_mode,is_default,status,provisioning_operation_id) "
            "VALUES($1,$2,'recover-account','dedicated',false,'pending',$3)",
            UUID(lease.workspace_id),
            UUID(lease.org_id),
            lease.operation_id,
        )
    return operation


@asynccontextmanager
async def recovery_composition(
    scenario, operation, tmp_path, monkeypatch, *, status="SUCCEEDED", failure=None
):
    original_authority = scenario.context.authority
    lease = operation.grant.lease
    principal = replace(
        operation.grant.principal,
        subject="authenticated-recovery-run",
        permissions=frozenset({"workspace:recover"}),
    )
    policy = tmp_path / "account-recovery-policy.json"
    policy.write_text(
        canonical({"version": 1, "tenants": {lease.org_id: scenario.policy}})
    )
    monkeypatch.setattr(settings, "superplane_lifecycle_config_file", str(policy))
    app = FastAPI()
    app.include_router(routes.router)
    app.state.trust_composition = SimpleNamespace(
        operation_connect=scenario.harness.connect
    )
    request = SimpleNamespace(app=app)
    submitter = SimpleNamespace(
        lease_scopes=frozenset({"controller_recovery/" + lease.org_id})
    )

    async def authenticate(request: Request):
        return submitter

    app.dependency_overrides[routes._authenticated_submitter] = authenticate
    monkeypatch.setattr(routes, "_authenticated_submitter", authenticate)
    reads, delivered = [], []
    control = SimpleNamespace(status=status, failure=failure, lose_settlement=False)

    class Provider:
        async def aws_read(self, service_name, method, **arguments):
            reads.append((service_name, method, arguments))
            if method == "get_caller_identity":
                return {"Account": scenario.request.management_account_id}
            if method == "describe_create_account_status":
                assert arguments == {"CreateAccountRequestId": "car-fixture"}
                return {
                    "CreateAccountStatus": {
                        "Id": "car-fixture",
                        "State": control.status,
                        "AccountName": "adp-" + lease.workspace_id,
                        **(
                            {"AccountId": scenario.child_account_id}
                            if control.status == "SUCCEEDED"
                            else {}
                        ),
                        **(
                            {"FailureReason": "CONCURRENT_ACCOUNT_MODIFICATION"}
                            if control.status == "FAILED"
                            else {}
                        ),
                    }
                }
            assert (service_name, method, arguments) == (
                "organizations",
                "list_parents",
                {"ChildId": scenario.child_account_id},
            )
            if control.failure in {"expired", "revoked"}:
                async with scenario.harness.connect() as connection:
                    if control.failure == "expired":
                        await connection.execute(
                            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second' "
                            "WHERE operation_id=$1",
                            lease.operation_id,
                        )
                    else:
                        await connection.execute(
                            "DELETE FROM harness_recovery_claim_bindings WHERE operation_id=$1",
                            lease.operation_id,
                        )
            if control.failure == "missing":
                return {"Parents": []}
            parent = {"Id": "r-fixture", "Type": "ROOT"}
            if control.failure == "ou":
                parent = {"Id": "ou-changed", "Type": "ORGANIZATIONAL_UNIT"}
            if control.failure == "malformed":
                parent["Id"] = "r-invalid/"
            return {
                "Parents": [parent],
                **({"NextToken": "more"} if control.failure == "pagination" else {}),
            }

    @asynccontextmanager
    async def cloud(**arguments):
        assert arguments["account_id"] == scenario.request.management_account_id
        await arguments["current"]()
        yield Provider()

    monkeypatch.setattr(service, "observation_provider", cloud)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://account-recovery"
    ) as client:

        class Transport:
            async def post(self, path, body):
                assert path.endswith("/recovery/account-creation")
                response = await client.post(
                    "/internal/controller/recovery/account-creation", json=body
                )
                if response.status_code == 422:
                    pytest.fail(
                        "recovery fixture request was rejected: " + response.text
                    )
                response.raise_for_status()
                return response.json()

        class Authority(RecoveryAuthority):
            async def resolve_recovery(self, claim):
                current = await routes.claim_operation(
                    request, routes.Claim(**claim_identity(claim))
                )
                assert current.grant.principal == principal
                async with (
                    scenario.harness.connect() as connection,
                    connection.transaction(),
                ):
                    assert await lock_recovery_grant(connection, current.grant)
                return current

            async def deliver_settlement(self, **receipt):
                assert receipt["accounting"]["budget"] == "retain"
                assert receipt["accounting"]["release_permitted"] is False
                delivered.append(receipt)
                if control.lose_settlement:
                    control.lose_settlement = False
                    raise OSError("receiver accepted settlement; reply lost")
                return receipt["receipt_id"]

        authority = Authority(Transport())
        context = SimpleNamespace(**{**vars(scenario.context), "authority": authority})
        provider = SimpleNamespace(
            execution_pool=SimpleNamespace(acquire=scenario.harness.connect),
            domain_pool=SimpleNamespace(acquire=scenario.harness.connect),
        )
        recovery = LifecycleRecovery(
            provider,
            principal=principal,
            operation_id=lease.operation_id,
            context=context,
            ledger=authority,
        )
        try:
            yield recovery, control, reads, delivered
        finally:
            scenario.context.authority = original_authority


async def next_sweep(scenario, operation):
    async with scenario.harness.connect() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1",
            operation.grant.lease.operation_id,
        )
        await connection.execute(
            "UPDATE harness_provider_call_intent SET reconcile_after=NULL"
        )


def test_account_recovery_observes_pending_then_joins_original_handoff_and_bootstrap(
    account_harness, tmp_path, monkeypatch
):
    scenario = CanonicalScenario(account_harness, tmp_path, monkeypatch)

    async def run():
        original = await accepted_without_artifact(scenario)
        lease = original.grant.lease
        async with recovery_composition(
            scenario, original, tmp_path, monkeypatch, status="IN_PROGRESS"
        ) as (recovery, control, reads, delivered):
            for _ in range(4):
                result = await recovery.run(limit=1)
                assert result[0].action == "deferred"
                assert not delivered and all(
                    method != "list_parents" for _, method, _ in reads
                )
                await next_sweep(scenario, original)
            async with account_harness.connect() as connection:
                call = await connection.fetchrow(
                    "SELECT * FROM harness_provider_call_intent"
                )
                assert (
                    call["stage"] == "intended"
                    and call["provider_ref"] == "request=car-fixture"
                )
                assert call["reconcile_attempts"] == 0
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM workspace_lifecycle_artifacts"
                    )
                    == 0
                )
            control.status, control.lose_settlement = "SUCCEEDED", True
            with pytest.raises(OSError):
                await recovery.run(limit=1)
            async with account_harness.connect() as connection:
                assert (
                    await connection.fetchval(
                        "SELECT state FROM harness_operations WHERE operation_id=$1",
                        lease.operation_id,
                    )
                    == "succeeded"
                )
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM harness_recovery_settlements"
                    )
                    == 1
                )
                row = dict(
                    await connection.fetchrow(
                        "SELECT * FROM workspace_lifecycle_artifacts"
                    )
                )
                assert row["producer_fence_token"] == lease.fence_token
                assert row["producer_attempt_id"] == lease.attempt_id
                assert row["producer_holder"] == lease.holder
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM harness_execution_audit WHERE event='account.creation_handoff' AND actor='authenticated-recovery-run'"
                    )
                    >= 1
                )
            assert await recovery.run(limit=1) == ()
            assert len(delivered) == 2 and delivered[0] == delivered[1]
        assert len(scenario.accounts) == 1
        scenario.revoked = False
        scenario.child = Child(scenario)
        following = await scenario.admit(continuation_parameters(row))
        result = await account_runtime.run_account_bootstrap(
            following, scenario.context
        )
        assert result["phase"] == "prepare-infrastructure"
        assert len(scenario.child.mutations) == 23
        assert len(scenario.accounts) == 1

    account_harness.run(run())


@pytest.mark.parametrize(
    "failure", ["missing", "pagination", "ou", "malformed", "expired", "revoked"]
)
def test_account_handoff_refuses_ambiguous_parents_and_lost_claim(
    account_harness, tmp_path, monkeypatch, failure
):
    scenario = CanonicalScenario(account_harness, tmp_path, monkeypatch)

    async def run():
        original = await accepted_without_artifact(scenario)
        async with recovery_composition(
            scenario, original, tmp_path, monkeypatch, failure=failure
        ) as (recovery, _, reads, delivered):
            results = await recovery.run(limit=1)
            assert results[0].action in {"deferred", "skipped"}
            assert not delivered and any(
                method == "list_parents" for _, method, _ in reads
            )
        async with account_harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT provider_ref FROM harness_provider_call_intent"
                )
                == "request=car-fixture"
            )
            assert (
                await connection.fetchval(
                    "SELECT stage FROM harness_provider_call_intent"
                )
                == "intended"
            )
        assert len(scenario.accounts) == 1

    account_harness.run(run())


@pytest.mark.parametrize("status", ["FAILED", "unreachable"])
def test_account_recovery_failed_or_unknown_retains_original_reservation(
    account_harness, tmp_path, monkeypatch, status
):
    scenario = CanonicalScenario(account_harness, tmp_path, monkeypatch)

    async def run():
        original = await accepted_without_artifact(scenario)
        async with recovery_composition(
            scenario, original, tmp_path, monkeypatch, status=status
        ) as (recovery, _, reads, delivered):
            for _ in range(3):
                results = await recovery.run(limit=1)
                if results[0].action != "deferred":
                    break
                await next_sweep(scenario, original)
            assert results[0].action == ("failed" if status == "FAILED" else "unknown")
            assert len(delivered) == 1
            assert delivered[0]["accounting"]["budget"] == "retain"
            assert all(method != "list_parents" for _, method, _ in reads)
        async with account_harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT provider_ref FROM harness_provider_call_intent"
                )
                == "request=car-fixture"
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_provider_call_intent"
                )
                == 1
            )
        assert len(scenario.accounts) == 1

    account_harness.run(run())
