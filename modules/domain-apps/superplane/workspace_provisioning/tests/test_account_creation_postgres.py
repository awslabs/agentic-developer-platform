"""Original grant + shared RPC + maintained producer, using remote PostgreSQL.

AWS transports are stateful doubles. No live accounts or local runtime tests.
New-account public runtime activation is deliberately not part of this checkpoint.
"""

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from account_factory.bootstrap import RoleTier, bootstrap_plan
from account_factory.modes import OwnershipMode
from account_provisioning.creation_runner import _decode_reference, encode_reference
from harness_jobs.execution import CallOutcome, OperationExecutor
from harness_jobs.execution_rpc import ExecutionRPCServer
from workspace_provisioning import account_creation, account_runtime, runtime
from workspace_provisioning.artifacts import (
    canonical,
    continuation_parameters,
    digest,
    initial_execution_steps,
)
from workspace_provisioning.lifecycle_policy import policy_digest
from workspace_provisioning.preview import preview_workspace
from workspace_provisioning.runtime_config import (
    LifecycleRefused,
    supported_runtime_modes,
)

from .postgres_bridge import requires_harness_postgres
from .test_lifecycle_runtime_postgres import Scenario, harness as lifecycle_harness
from .test_preview import authority, request_for

pytestmark = requires_harness_postgres
harness = lifecycle_harness


class AccountScenario(Scenario):
    def __init__(self, harness, tmp_path, monkeypatch, *, immediate=False):
        super().__init__(
            harness, tmp_path, monkeypatch, OwnershipMode.EXISTING_ACCOUNT_MANAGED
        )
        self.request = replace(
            request_for(OwnershipMode.NEW_ACCOUNT_MANAGED),
            workspace_id="ws-1",
            region="us-east-1",
            availability_zones=("us-east-1a", "us-east-1b"),
        )
        self.authorization = replace(
            authority(), workspace_id="ws-1", operation_org_id="org-a"
        )
        self.policy["permitted_modes"].append("new-account-managed")
        self.policy["permitted_organizational_units"] = [
            self.request.organizational_unit_id
        ]
        document = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"}
            ],
        }
        trust = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"AWS": "arn:aws:iam::000000000001:role/provider"},
                }
            ],
        }
        steps = [
            step
            for step in bootstrap_plan(self.request, self.authorization).steps
            if isinstance(step.tier, RoleTier)
        ]
        self.policy["runtime"]["new_account"] = {
            "child_access_role_name": "OrganizationAccountAccessRole",
            "trust_policies": {step.name: deepcopy(trust) for step in steps},
            "permission_policy_arns": {
                step.name: "arn:aws:iam::${ACCOUNT_ID}:policy/" + step.name
                for step in steps
            },
            "permission_policy_documents": {
                step.name: deepcopy(document) for step in steps
            },
            "runtime_roles": {
                name: {
                    "role_name": name,
                    "trust_policy": deepcopy(trust),
                    "policy_arn": "arn:aws:iam::${ACCOUNT_ID}:policy/runtime-" + name,
                    "policy_document": deepcopy(document),
                }
                for name in ("provider", "registrar", "installer", "supervisor")
            },
            "audit_trail_arn": "arn:aws:cloudtrail:us-east-1:000000000001:trail/organization",
        }
        self.revise()
        self.accounts, self.assumed_roles = [], []
        self.lose_create_reply = False
        self.cancel_after_accept = self.revoked = False
        self.expire_after_accept = False
        self.child_account_id = "000000000003"
        self.parent = {"Id": "r-fixture", "Type": "ROOT"}
        scenario = self

        class Management:
            def client(self, service, **kwargs):
                assert service in {"organizations", "cloudtrail"}
                return self

            def describe_organization(self):
                return {
                    "Organization": {
                        "Id": scenario.request.organization_id,
                        "ManagementAccountId": scenario.request.management_account_id,
                    }
                }

            def describe_organizational_unit(self, *, OrganizationalUnitId):
                return {
                    "OrganizationalUnit": {
                        "Id": OrganizationalUnitId,
                        "Arn": f"arn:aws:organizations::000000000001:ou/{scenario.request.organization_id}/{OrganizationalUnitId}",
                    }
                }

            def list_roots(self):
                return {"Roots": [{"Id": "r-fixture"}]}

            def list_organizational_units_for_parent(self, *, ParentId):
                assert ParentId == "r-fixture"
                return {
                    "OrganizationalUnits": [
                        {"Id": scenario.request.organizational_unit_id}
                    ]
                }

            def describe_trails(self, **kwargs):
                return {
                    "trailList": [
                        {
                            "TrailARN": scenario.policy["runtime"]["new_account"][
                                "audit_trail_arn"
                            ],
                            "HomeRegion": "us-east-1",
                            "IsOrganizationTrail": True,
                            "IsMultiRegionTrail": True,
                            "IncludeGlobalServiceEvents": True,
                            "LogFileValidationEnabled": True,
                        }
                    ]
                }

            def get_trail_status(self, **kwargs):
                return {"IsLogging": True}

            def create_account(self, **arguments):
                async def evidence():
                    async with harness.connect() as connection:
                        return await connection.fetch(
                            "SELECT * FROM harness_provider_call_intent"
                        )

                calls = asyncio.run_coroutine_threadsafe(
                    evidence(), scenario.loop
                ).result(5)
                assert len(calls) == 1 and calls[0]["stage"] == "intended"
                assert calls[0]["idempotency_key"].startswith("operation-step:")
                assert calls[0]["provider"] == "aws-organizations"
                scenario.accounts.append(arguments)
                if scenario.cancel_after_accept or scenario.expire_after_accept:
                    from harness_jobs.recovery import request_cancellation

                    async def cancel():
                        operation = next(iter(scenario.operations.values()))
                        async with harness.connect() as connection:
                            if scenario.expire_after_accept:
                                await connection.execute(
                                    "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second' "
                                    "WHERE operation_id=$1",
                                    operation.grant.lease.operation_id,
                                )
                                return
                            assert await request_cancellation(
                                connection,
                                operation_id=operation.grant.lease.operation_id,
                                principal=operation.grant.principal,
                                reason="fixture cancellation",
                            )

                    asyncio.run_coroutine_threadsafe(cancel(), scenario.loop).result(5)
                    scenario.revoked = True
                if scenario.lose_create_reply:
                    raise OSError("lost accepted account reply")
                return {
                    "CreateAccountStatus": {
                        "Id": "car-fixture",
                        "State": "SUCCEEDED" if immediate else "IN_PROGRESS",
                        **(
                            {"AccountId": scenario.child_account_id}
                            if immediate
                            else {}
                        ),
                    }
                }

            def describe_create_account_status(self, *, CreateAccountRequestId):
                assert CreateAccountRequestId == "car-fixture"
                if not immediate:

                    async def reference():
                        async with harness.connect() as connection:
                            return await connection.fetchval(
                                "SELECT provider_ref FROM harness_provider_call_intent WHERE provider='aws-organizations'"
                            )

                    persisted = asyncio.run_coroutine_threadsafe(
                        reference(), scenario.loop
                    ).result(5)
                    assert _decode_reference(persisted)[2] == "car-fixture"
                return {
                    "CreateAccountStatus": {
                        "Id": CreateAccountRequestId,
                        "State": "SUCCEEDED",
                        "AccountId": scenario.child_account_id,
                        "AccountName": "adp-ws-1",
                    }
                }

            def list_parents(self, *, ChildId):
                assert ChildId == scenario.child_account_id
                return {"Parents": [deepcopy(scenario.parent)]}

            def move_account(self, *, AccountId, SourceParentId, DestinationParentId):
                assert (
                    AccountId == scenario.child_account_id
                    and SourceParentId == scenario.parent["Id"]
                )
                scenario.parent = {
                    "Id": DestinationParentId,
                    "Type": "ORGANIZATIONAL_UNIT",
                }
                return {}

        self.management = Management()

        class Child:
            def client(self, service, **kwargs):
                assert service in {"iam", "s3control", "cloudtrail"}
                return self

            def get_policy(self, **kwargs):
                raise OSError("child IAM bootstrap unavailable")

        self.child = Child()

        async def delivery_role(operation):
            self.deliveries.append(operation.grant.lease.operation_id)
            assert operation.request.parameters["aws_account_id"] == "000000000001"
            return {"role_arn": "arn:aws:iam::000000000001:role/provider"}

        self.context.authority.delivery_role = delivery_role
        original_resolve = self.context.authority.resolve

        async def resolve(operation_id):
            if self.revoked:
                raise LifecycleRefused("fixture original execution authority revoked")
            return await original_resolve(operation_id)

        self.context.authority.resolve = resolve

        def assume(source, *, role_arn, region, verify, external_id=None):
            verify()
            self.assumed_roles.append(role_arn)
            if source is self.context.base_session:
                assert role_arn == "arn:aws:iam::000000000001:role/provider"
                return self.management
            assert source is self.management
            assert role_arn.startswith("arn:aws:iam::000000000003:role/")
            return self.child

        monkeypatch.setattr("workspace_provisioning.credentials.assume_session", assume)
        monkeypatch.setattr(account_runtime, "assume_session", assume)

    def revise(self):
        capacity = {
            key: int(self.parameters[key])
            for key in ("max_resource_units", "max_runtime_seconds", "max_cost_micros")
        }
        capacity.update(
            request={"isolation_mode": "namespace"},
            allocation_id="fixture-allocation",
            policy_revision=policy_digest(self.policy),
        )
        self.parameters.update(
            lifecycle_request=canonical(asdict(self.request)),
            aws_account_id=self.request.management_account_id,
            runtime_config_sha256=digest(self.policy["runtime"]),
            lifecycle_policy_sha256=policy_digest(self.policy),
            plan_revision=preview_workspace(
                self.request,
                authorization=self.authorization,
                requested_capacity=capacity,
                cost_estimate=None,
                approval_required=True,
            ).revision,
        )
        self.parameters["execution_steps"] = initial_execution_steps(self.parameters)

    async def created(self):
        operation = await self.admit(self.parameters)
        result = await account_creation.run_account_creation(operation, self.context)
        return operation, await self.row(result["artifact_id"])


@pytest.mark.parametrize("immediate", [False, True])
def test_maintained_creation_uses_one_real_admitted_call_and_separate_child_approval(
    harness, tmp_path, monkeypatch, immediate
):
    scenario = AccountScenario(harness, tmp_path, monkeypatch, immediate=immediate)

    async def run():
        operation, row = await scenario.created()
        assert row["account_id"] == "000000000003"
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    operation.grant.lease.operation_id,
                )
                == "succeeded"
            )
            calls = await connection.fetch("SELECT * FROM harness_provider_call_intent")
        assert len(calls) == 1 and calls[0]["outcome"].startswith("succeeded")
        assert _decode_reference(calls[0]["provider_ref"])[0] == "000000000003"
        assert len(scenario.accounts) == 1
        assert scenario.assumed_roles == ["arn:aws:iam::000000000001:role/provider"]
        parameters = continuation_parameters(row)
        assert parameters["aws_account_id"] == "000000000001"
        child_operation = await scenario.admit(parameters)
        assert (
            child_operation.grant.lease.operation_id
            != operation.grant.lease.operation_id
        )
        _, _, _, validated, step = await runtime.validate_phase(
            child_operation, scenario.context
        )
        assert step.step_id == "bootstrap-account" and validated == row
        assert (
            await account_runtime.child_session(
                child_operation,
                scenario.context,
                scenario.policy["runtime"],
                scenario.request,
                row,
                scenario.management,
            )
            is scenario.child
        )
        assert scenario.assumed_roles[-1].endswith(
            ":role/OrganizationAccountAccessRole"
        )
        assert supported_runtime_modes(scenario.policy["runtime"]) == {
            "managed",
            "adopt",
        }
        with pytest.raises(LifecycleRefused, match="complete reviewed account recipe"):
            await runtime.run_lifecycle(child_operation, scenario.context)

    harness.run(run())


def test_lost_creation_reply_never_dispatches_another_account(
    harness, tmp_path, monkeypatch
):
    scenario = AccountScenario(harness, tmp_path, monkeypatch)
    scenario.lose_create_reply = True

    async def run():
        operation = await scenario.admit(scenario.parameters)
        for _ in range(2):
            with pytest.raises(Exception):
                await account_creation.run_account_creation(operation, scenario.context)
        assert len(scenario.accounts) == 1
        async with harness.connect() as connection:
            calls = await connection.fetch("SELECT * FROM harness_provider_call_intent")
            assert len(calls) == 1 and calls[0]["stage"] == "intended"
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 0
            )

    harness.run(run())


def test_interrupted_handoff_retains_accepted_id_and_resumes_by_observation(
    harness, tmp_path, monkeypatch
):
    scenario = AccountScenario(harness, tmp_path, monkeypatch)
    real_publish = account_creation.publish_created_account

    async def interrupted(*args, **kwargs):
        raise OSError("handoff transport interrupted after accepted creation")

    async def run():
        operation = await scenario.admit(scenario.parameters)
        monkeypatch.setattr(account_creation, "publish_created_account", interrupted)
        with pytest.raises(Exception):
            await account_creation.run_account_creation(operation, scenario.context)
        async with harness.connect() as connection:
            call = await connection.fetchrow(
                "SELECT * FROM harness_provider_call_intent"
            )
            assert call["stage"] == "intended"
            assert _decode_reference(call["provider_ref"])[2] == "car-fixture"
        monkeypatch.setattr(account_creation, "publish_created_account", real_publish)
        result = await account_creation.run_account_creation(
            operation, scenario.context
        )
        assert (await scenario.row(result["artifact_id"]))[
            "account_id"
        ] == "000000000003"
        assert len(scenario.accounts) == 1
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_provider_call_intent"
                )
                == 1
            )

    harness.run(run())


@pytest.mark.parametrize("withdrawal", ["cancellation", "expiry"])
def test_authority_loss_during_create_keeps_the_returned_request_id(
    harness, tmp_path, monkeypatch, withdrawal
):
    scenario = AccountScenario(harness, tmp_path, monkeypatch)
    scenario.cancel_after_accept = withdrawal == "cancellation"
    scenario.expire_after_accept = withdrawal == "expiry"

    async def run():
        operation = await scenario.admit(scenario.parameters)
        with pytest.raises(Exception):
            await account_creation.run_account_creation(operation, scenario.context)
        async with harness.connect() as connection:
            call = await connection.fetchrow(
                "SELECT * FROM harness_provider_call_intent"
            )
            assert call["stage"] == "intended"
            assert _decode_reference(call["provider_ref"])[2] == "car-fixture"
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 0
            )
        assert len(scenario.accounts) == 1
        assert scenario.assumed_roles == ["arn:aws:iam::000000000001:role/provider"]

    harness.run(run())


def test_successful_call_resumes_committed_handoff_before_operation_settlement(
    harness, tmp_path, monkeypatch
):
    scenario = AccountScenario(harness, tmp_path, monkeypatch)
    real_reconcile = account_creation.CreationExecution.reconcile

    async def interrupted(adapter, **kwargs):
        await real_reconcile(adapter, **kwargs)
        raise OSError("lost successful observation reply before operation settlement")

    async def run():
        operation = await scenario.admit(scenario.parameters)
        monkeypatch.setattr(
            account_creation.CreationExecution, "reconcile", interrupted
        )
        with pytest.raises(Exception):
            await account_creation.run_account_creation(operation, scenario.context)
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT outcome FROM harness_provider_call_intent"
                )
            ).startswith("succeeded")
            assert (
                await connection.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    operation.grant.lease.operation_id,
                )
                != "succeeded"
            )
            artifact_id = await connection.fetchval(
                "SELECT artifact_id FROM workspace_lifecycle_artifacts"
            )
        monkeypatch.setattr(
            account_creation.CreationExecution, "reconcile", real_reconcile
        )
        result = await account_creation.run_account_creation(
            operation, scenario.context
        )
        assert result["artifact_id"] == artifact_id and len(scenario.accounts) == 1
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    operation.grant.lease.operation_id,
                )
                == "succeeded"
            )

    harness.run(run())


def test_producer_adapter_refuses_an_extra_retry_key(harness, tmp_path, monkeypatch):
    from account_provisioning.creation_runner import creation_key, creation_target

    scenario = AccountScenario(harness, tmp_path, monkeypatch)

    async def run():
        operation = await scenario.admit(scenario.parameters)

        async def forbidden(*args, **kwargs):
            raise AssertionError("unapproved producer call reached dispatch")

        executor = OperationExecutor(
            operation.grant.lease, connect=harness.connect, provider_call=forbidden
        )
        server = ExecutionRPCServer(
            connect=harness.connect, provider_call=forbidden, authenticate=forbidden
        )
        adapter = account_creation.CreationExecution(
            operation, scenario.context, scenario.request, executor, server, forbidden
        )
        with pytest.raises(LifecycleRefused, match="retry generation"):
            await adapter.execute_provider(
                idempotency_key=creation_key(adapter, 1),
                provider="aws-organizations",
                operation_kind="create-account",
                target=creation_target(scenario.request),
            )
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_provider_call_intent"
                )
                == 0
            )
        assert scenario.accounts == [] and scenario.assumed_roles == []

    harness.run(run())


@pytest.mark.parametrize(
    "tamper", ["child", "shared-call", "unapproved-source", "input-row"]
)
def test_changed_creation_provenance_refuses_child_credentials(
    harness, tmp_path, monkeypatch, tamper
):
    scenario = AccountScenario(harness, tmp_path, monkeypatch)

    async def run():
        operation, row = await scenario.created()
        child_operation = await scenario.admit(continuation_parameters(row))
        if tamper == "child":
            scenario.child_account_id = "000000000004"
        elif tamper == "shared-call":
            async with harness.connect() as connection:
                await connection.execute(
                    "UPDATE harness_provider_call_intent SET provider_ref=$1 WHERE operation_id=$2",
                    encode_reference(request_id="car-other", account_id="000000000003"),
                    operation.grant.lease.operation_id,
                )
        elif tamper == "input-row":
            row = {**row, "account_id": "000000000004"}
        else:
            child_operation = operation
        with pytest.raises(LifecycleRefused):
            await account_runtime.child_session(
                child_operation,
                scenario.context,
                scenario.policy["runtime"],
                scenario.request,
                row,
                scenario.management,
            )
        assert scenario.assumed_roles == ["arn:aws:iam::000000000001:role/provider"]

    harness.run(run())


def test_creation_success_survives_separately_approved_bootstrap_failure(
    harness, tmp_path, monkeypatch
):
    scenario = AccountScenario(harness, tmp_path, monkeypatch)

    async def run():
        creation, row = await scenario.created()
        operation = await scenario.admit(continuation_parameters(row))

        async def hook(call):
            assert call.operation_kind == "bootstrap-account"
            await account_runtime.bootstrap_account_phase(
                operation,
                scenario.context,
                scenario.policy["runtime"],
                scenario.request,
                scenario.authorization,
                row,
                scenario.management,
            )
            raise AssertionError("child IAM failure cannot claim bootstrap success")

        async def authenticate(_token):
            return operation.grant

        server = ExecutionRPCServer(
            connect=harness.connect, provider_call=hook, authenticate=authenticate
        )
        executor = OperationExecutor(
            operation.grant.lease, connect=harness.connect, provider_call=hook
        )
        call, _ = await server.execute_step(
            operation.grant, executor, "bootstrap-account"
        )
        assert call.outcome is not CallOutcome.SUCCEEDED
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    creation.grant.lease.operation_id,
                )
                == "succeeded"
            )
            effects = await connection.fetch(
                "SELECT * FROM workspace_lifecycle_effects ORDER BY effect_key,event"
            )
            assert {effect["effect_key"] for effect in effects} == {
                "place-account",
                "policy-"
                + next(
                    iter(
                        scenario.policy["runtime"]["new_account"][
                            "permission_policy_arns"
                        ]
                    )
                ),
            }
        assert scenario.parent["Type"] == "ORGANIZATIONAL_UNIT"
        assert len(scenario.accounts) == 1
        assert (await scenario.row(row["artifact_id"]))["account_id"] == "000000000003"

    harness.run(run())
