"""Real admitted phase handoffs; transport doubles cannot synthesize readiness.

This covers run_lifecycle, shared RPC/execution, saved-plan bytes, adoption SDK
discovery, SQL artifacts and the network effect journal in one composition.
Bootstrap stops at its adapter boundary: these tests deliberately do not claim
canonical bootstrap, live provider success, deployment or story completion.
Run only in remote CI or the disposable EC2 regression harness.
"""

import asyncio
from builtins import BaseExceptionGroup
from copy import deepcopy
from dataclasses import asdict, replace
import importlib.util
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest

from harness_jobs import OperationFacadeService, OperationStore, REQUIRED_PERMISSION
from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.identity import decode_payload

from account_factory.modes import OwnershipMode
from workspace_provisioning import bootstrap_runtime, runtime, terraform
from workspace_provisioning.artifacts import (
    canonical,
    continuation_parameters,
    digest,
    initial_execution_steps,
    read_artifact,
)
from workspace_provisioning.lifecycle_policy import policy_digest
from workspace_provisioning.preview import preview_workspace
from workspace_provisioning.runtime_config import LifecycleRefused

from .postgres_bridge import Harness, requires_harness_postgres
from .test_lifecycle_adoption import fixture as discovery_fixture
from .test_lifecycle_policy import policy
from .test_preview import authority, request_for
from .test_retirement_execution_postgres import (
    _Approves,
    _Ledger,
    _principal,
    _Resolver,
)

pytestmark = requires_harness_postgres


@pytest.fixture
def harness(tmp_path_factory, request):
    with Harness.started(tmp_path_factory, request.node.name) as value:
        output = StringIO()
        context = MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
        )
        with Operations.context(context):
            for name in ("019_lifecycle_artifacts", "020_lifecycle_effects"):
                path = (
                    Path(__file__).resolve().parents[2]
                    / "src/superplane-api/alembic/versions"
                    / (name + ".py")
                )
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.upgrade()

        async def migrate():
            async with value.connect() as connection:
                await connection.execute(output.getvalue())

        value.run(migrate())
        yield value


class Scenario:
    def __init__(self, harness, tmp_path, monkeypatch, mode):
        self.harness, self.operations = harness, {}
        self.ledger, self.deliveries, self.process_calls = _Ledger(), [], []
        self.creates, self.bootstrap_calls = [], []
        self.lose_apply_reply = self.lose_ingress_reply = False
        self.facade = OperationFacadeService(
            connect=harness.connect,
            resolver=_Resolver(_principal()),
            approvals=_Approves(),
            ledger=self.ledger,
        )
        self.policy = policy()
        self.policy.update(
            aws_organization_id="o-testorg1234",
            management_cluster="fixture-management",
            permitted_regions=["us-east-1"],
        )
        self.request = replace(
            request_for(mode),
            workspace_id="ws-1",
            region="us-east-1",
            availability_zones=("us-east-1a", "us-east-1b")
            if mode.creates_cluster
            else (),
            existing_cluster_name=None if mode.creates_cluster else "adopted",
        )
        authorization = replace(
            authority(),
            workspace_id="ws-1",
            operation_org_id="org-a",
            permitted_modes=frozenset(
                {
                    OwnershipMode.EXISTING_ACCOUNT_MANAGED,
                    OwnershipMode.BRING_EXISTING_CLUSTER,
                }
            ),
            permitted_organizational_units=frozenset(),
        )
        public = {"isolation_mode": "namespace"}
        capacity = {
            "max_resource_units": 1,
            "max_runtime_seconds": 900,
            "max_cost_micros": 3000000,
            "request": public,
            "allocation_id": "fixture-allocation",
            "policy_revision": policy_digest(self.policy),
        }
        revision = preview_workspace(
            self.request,
            authorization=authorization,
            requested_capacity=capacity,
            cost_estimate=None,
            approval_required=True,
        ).revision
        self.parameters = {
            "plan_revision": revision,
            "workspace_name": "fixture",
            "lifecycle_request": canonical(asdict(self.request)),
            "lifecycle_inputs": canonical(public),
            "runtime_config_sha256": digest(self.policy["runtime"]),
            "lifecycle_policy_sha256": policy_digest(self.policy),
            "allocation_id": "fixture-allocation",
            **{
                key: str(capacity[key])
                for key in (
                    "max_resource_units",
                    "max_runtime_seconds",
                    "max_cost_micros",
                )
            },
            **{
                "lifecycle_allocation_" + key: str(capacity[key])
                for key in (
                    "max_resource_units",
                    "max_runtime_seconds",
                    "max_cost_micros",
                )
            },
        }
        self.parameters["execution_steps"] = initial_execution_steps(self.parameters)
        scenario = self

        class Authority:
            async def resolve(self, operation_id):
                return scenario.operations[operation_id]

            async def delivery_role(self, operation):
                scenario.deliveries.append(operation.grant.lease.operation_id)
                return {"role_arn": "arn:aws:iam::000000000002:role/provider"}

        self.context = SimpleNamespace(
            connect=harness.connect,
            domain_connect=harness.connect,
            authority=Authority(),
            policy=self.policy,
            policy_fixture=True,
            state_root=tmp_path / "worker-state",
            base_session=object(),
        )
        with monkeypatch.context() as isolated:
            _, _, _, self.responses, _ = discovery_fixture(isolated)
        self.rules = self.responses["describe_security_group_rules"][
            "SecurityGroupRules"
        ]

        class Session:
            def client(self, service, *, region_name):
                assert service in {"sts", "eks", "ec2"} and region_name == "us-east-1"
                return self

            def get_caller_identity(self):
                return {"Account": "000000000002"}

            def describe_security_groups(self, *, GroupIds):
                return {
                    "SecurityGroups": [
                        {
                            "GroupId": group,
                            "VpcId": "vpc-0123456789abcdef0",
                            "OwnerId": "000000000002",
                        }
                        for group in GroupIds
                    ]
                }

            def describe_security_group_rules(self, *, Filters):
                assert Filters[0]["Name"] == "group-id"
                return {
                    "SecurityGroupRules": deepcopy(
                        [
                            rule
                            for rule in scenario.rules
                            if rule["GroupId"] in Filters[0]["Values"]
                        ]
                    )
                }

            def authorize_security_group_ingress(self, **arguments):
                async def durable():
                    async with harness.connect() as connection:
                        return await connection.fetchval(
                            "SELECT count(*) FROM workspace_lifecycle_effects WHERE event='intended'"
                        )

                assert (
                    asyncio.run_coroutine_threadsafe(durable(), scenario.loop).result(5)
                    == 1
                )
                scenario.creates.append(arguments)
                rule = {
                    "SecurityGroupRuleId": "sgr-11111111111111111",
                    "GroupId": arguments["GroupId"],
                    "GroupOwnerId": "000000000002",
                    "IsEgress": False,
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "ReferencedGroupInfo": arguments["IpPermissions"][0][
                        "UserIdGroupPairs"
                    ][0],
                    "Tags": arguments["TagSpecifications"][0]["Tags"],
                }
                scenario.rules.append(rule)
                if scenario.lose_ingress_reply:
                    raise OSError("lost ingress reply")
                return {"SecurityGroupRules": [deepcopy(rule)]}

            def __getattr__(self, method):
                assert method in scenario.responses, (
                    "unapproved provider method: " + method
                )
                return lambda **arguments: deepcopy(scenario.responses[method])

        self.session = Session()

        def assume(source, *, role_arn, region, verify, external_id=None):
            assert source is self.context.base_session
            assert role_arn == "arn:aws:iam::000000000002:role/provider"
            verify()
            return self.session

        monkeypatch.setattr("workspace_provisioning.credentials.assume_session", assume)
        source = tmp_path / "maintained"
        source.mkdir()
        (source / "scripts").mkdir()
        (source / "main.tf").write_text("# synthetic reviewed module\n")
        (source / ".terraform.lock.hcl").write_text("# synthetic provider lock\n")
        monkeypatch.setattr(terraform, "workspace_source", lambda: source)

        class Process:
            def __init__(self, *, binaries, directory, session, region, verify):
                assert session is scenario.session
                self.directory, self.verify = directory, verify

            def checked(self, argv, **kwargs):
                self.verify()
                scenario.process_calls.append(tuple(argv))
                if "prepare_workspace_plan.py" in argv[1]:
                    variables = json.loads(
                        (self.directory / "workspace.tfvars.json").read_text()
                    )
                    assert "node_instance_type" not in variables
                    review = self.directory / "review"
                    review.mkdir()
                    files = {
                        "workspace.tfplan": "reviewed-binary-plan",
                        "workspace-plan.json": "{}",
                        "workspace-authorization.proposed.json": "{}",
                        "workspace-inventory.json": '{"destructive_addresses":[]}',
                        "workspace-estimate.json": '{"bounded_monthly_usd":1}',
                        "workspace-backend.json": "{}",
                    }
                    for name, content in files.items():
                        (review / name).write_text(content)
                elif "apply_workspace_plan.py" in argv[1]:
                    reviewed = Path(argv[argv.index("--plan-file") + 1])
                    assert reviewed.read_text() == "reviewed-binary-plan"
                    if scenario.lose_apply_reply:
                        raise OSError("lost apply reply after provider allocation")
                elif argv[:2] == ["terraform", "output"]:
                    return canonical(scenario.outputs)
                else:
                    raise AssertionError("unapproved process")
                return ""

        monkeypatch.setattr(runtime, "WorkerProcesses", Process)

        def bootstrap(*args):
            scenario.bootstrap_calls.append(args)
            raise LifecycleRefused(
                "canonical bootstrap boundary reached; no readiness asserted"
            )

        monkeypatch.setattr(bootstrap_runtime, "bootstrap", bootstrap)

    async def admit(self, parameters):
        self.loop = asyncio.get_running_loop()
        progress = await self.facade.open_operation(
            action="provision",
            workspace_id="ws-1",
            org_id="org-a",
            permission=REQUIRED_PERMISSION,
            parameters=parameters,
        )
        lease = await self.harness.lease(progress.operation_id)
        async with self.harness.connect() as connection:
            record = await OperationStore().get(
                connection, _principal(), progress.operation_id
            )
        operation = SimpleNamespace(
            grant=ExecutionGrant(_principal(lease.holder), lease),
            job_id=record.job_id,
            plan_digest=record.plan_digest,
            request_payload=record.request_payload,
            request=decode_payload(record.request_payload),
            reservation_state="confirmed",
            max_runtime_seconds=900,
        )
        self.operations[progress.operation_id] = operation
        return operation

    async def row(self, artifact_id):
        return await read_artifact(
            self.harness.connect,
            artifact_id=artifact_id,
            org_id="org-a",
            workspace_id="ws-1",
        )

    async def prepared(self):
        operation = await self.admit(self.parameters)
        result = await runtime.run_lifecycle(operation, self.context)
        row = await self.row(result["artifact_id"])
        assert row["source_operation_id"] == operation.grant.lease.operation_id
        async with self.harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    row["source_operation_id"],
                )
                == "succeeded"
            )
        return row


def errors(exception):
    if isinstance(exception, BaseExceptionGroup):
        return " ".join(errors(child) for child in exception.exceptions)
    return str(exception)


def test_managed_preparation_and_apply_preserve_review_and_original_allocation(
    harness, tmp_path, monkeypatch
):
    scenario = Scenario(
        harness, tmp_path, monkeypatch, OwnershipMode.EXISTING_ACCOUNT_MANAGED
    )

    async def run():
        from workspace_provisioning.adoption import prepare_adoption

        prepared = await scenario.prepared()
        assert len(scenario.process_calls) == 1
        applying = await scenario.admit(continuation_parameters(prepared))
        # Native-shaped Terraform outputs come from the real discovery adapter's
        # parser; the synthetic provider target is identical in this transport test.
        adoption_request = replace(
            scenario.request,
            mode=OwnershipMode.BRING_EXISTING_CLUSTER,
            existing_cluster_name="adopted",
            vpc_cidr=None,
            availability_zones=(),
            cluster_version=None,
        )
        _, metadata = await prepare_adoption(
            applying, scenario.context, adoption_request, scenario.session
        )
        scenario.outputs = metadata["outputs"]
        scenario.outputs["workspace_node_group"] = {
            "value": {
                "name": "nodes",
                "arn": scenario.responses["describe_nodegroup"]["nodegroup"][
                    "nodegroupArn"
                ],
                "launch_template_id": "lt-0123456789abcdef0",
                "launch_template_version": "2",
            }
        }
        # The owned module relies on the EKS-managed node SG rather than an
        # explicit launch-template SG, unlike the adopted-cluster fixture.
        vpc = scenario.responses["describe_cluster"]["cluster"]["resourcesVpcConfig"]
        vpc["clusterSecurityGroupId"] = "sg-22222222222222222"
        vpc["securityGroupIds"] = ["sg-11111111111111111"]
        scenario.responses["describe_launch_template_versions"][
            "LaunchTemplateVersions"
        ][0]["LaunchTemplateData"].pop("SecurityGroupIds")
        result = await runtime.run_lifecycle(applying, scenario.context)
        assert result["status"] == "awaiting_plan_approval"
        assert result["phase"] == "bootstrap-workspace"
        applied = await scenario.row(result["artifact_id"])
        facts = json.loads(applied["artifact_metadata_json"])
        assert facts["source_artifact_id"] == prepared["artifact_id"]
        assert (
            facts["allocation_source_operation_id"] == applying.grant.lease.operation_id
        )
        assert len(scenario.process_calls) == 3
        assert not scenario.creates and not scenario.bootstrap_calls

    harness.run(run())


def test_adoption_handoff_uses_real_network_journal_before_bootstrap(
    harness, tmp_path, monkeypatch
):
    scenario = Scenario(
        harness, tmp_path, monkeypatch, OwnershipMode.BRING_EXISTING_CLUSTER
    )

    async def run():
        prepared = await scenario.prepared()
        assert not scenario.process_calls and not scenario.creates
        operation = await scenario.admit(continuation_parameters(prepared))
        with pytest.raises(Exception) as exc:
            await runtime.run_lifecycle(operation, scenario.context)
        # Shared execution converts an interrupted provider phase to UNKNOWN.
        assert "no unique durable result" in errors(exc.value)
        assert len(scenario.bootstrap_calls) == 1 and len(scenario.creates) == 1
        network = scenario.bootstrap_calls[0][-1]
        assert network["private-sts-rule"] == {
            "created": False,
            "rule_id": "sgr-0123456789abcdef0",
        }
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_effects WHERE event='confirmed'"
                )
                == 1
            )

    harness.run(run())


@pytest.mark.parametrize("fault", ["changed-plan", "lost-apply-reply"])
def test_unfinished_apply_never_offers_bootstrap_or_replays(
    harness, tmp_path, monkeypatch, fault
):
    scenario = Scenario(
        harness, tmp_path, monkeypatch, OwnershipMode.EXISTING_ACCOUNT_MANAGED
    )

    async def run():
        prepared = await scenario.prepared()
        if fault == "changed-plan":
            _, review = terraform.verify_prepared_artifact(prepared, scenario.context)
            path = review / "workspace.tfplan"
            path.chmod(0o600)
            path.write_text("changed")
        else:
            scenario.lose_apply_reply = True
        operation = await scenario.admit(continuation_parameters(prepared))
        with pytest.raises(Exception):
            await runtime.run_lifecycle(operation, scenario.context)
        calls = list(scenario.process_calls)
        with pytest.raises(Exception):
            await runtime.run_lifecycle(operation, scenario.context)
        assert scenario.process_calls == calls
        assert len(calls) == (1 if fault == "changed-plan" else 2)
        assert not scenario.bootstrap_calls and not scenario.creates
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    operation.grant.lease.operation_id,
                )
                != "succeeded"
            )
        assert not scenario.ledger.released

    harness.run(run())


def test_lost_api_ingress_reply_keeps_intent_and_does_not_replay(
    harness, tmp_path, monkeypatch
):
    scenario = Scenario(
        harness, tmp_path, monkeypatch, OwnershipMode.BRING_EXISTING_CLUSTER
    )

    async def run():
        prepared = await scenario.prepared()
        scenario.lose_ingress_reply = True
        operation = await scenario.admit(continuation_parameters(prepared))
        for _ in range(2):
            with pytest.raises(Exception):
                await runtime.run_lifecycle(operation, scenario.context)
        assert len(scenario.creates) == 1 and not scenario.bootstrap_calls
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_effects WHERE event='intended'"
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_effects WHERE event='confirmed'"
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 1
            )
        assert not scenario.ledger.released

    harness.run(run())
