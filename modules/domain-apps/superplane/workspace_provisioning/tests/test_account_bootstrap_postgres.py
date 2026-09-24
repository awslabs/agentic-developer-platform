"""Real private account bootstrap, maintained verification/registration, remote PG."""

from copy import deepcopy
import json

import pytest

from account_provisioning.bootstrap_runner import role_name_for
from account_factory.bootstrap import RoleTier, bootstrap_plan
from workspace_provisioning import account_runtime
from workspace_provisioning.artifacts import continuation_parameters
from workspace_provisioning.runtime import validate_phase

from .postgres_bridge import requires_harness_postgres
from .test_account_creation_postgres import (
    AccountScenario,
    harness as lifecycle_harness,
)

pytestmark = requires_harness_postgres
harness = lifecycle_harness


class Missing(Exception):
    def __init__(self, code="NoSuchEntity"):
        self.response = {"Error": {"Code": code}}


class Child:
    """Stateful SDK transport; verification reads actual prior mutation results."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.roles, self.policies, self.attachments, self.block = {}, {}, {}, None
        self.mutations = []

    def client(self, service, **kwargs):
        if service == "cloudtrail":
            return self.scenario.management
        assert service in {"iam", "s3control"}
        return self

    def get_policy(self, *, PolicyArn):
        if PolicyArn not in self.policies:
            raise Missing()
        return {
            "Policy": {
                "Arn": PolicyArn,
                "PolicyId": "ANPA-fixture",
                "DefaultVersionId": "v1",
            }
        }

    def get_policy_version(self, *, PolicyArn, VersionId):
        assert VersionId == "v1"
        return {"PolicyVersion": {"Document": deepcopy(self.policies[PolicyArn])}}

    def create_policy(self, *, PolicyName, Path, PolicyDocument):
        arn = (
            f"arn:aws:iam::{self.scenario.child_account_id}:policy" + Path + PolicyName
        )
        assert arn not in self.policies
        self.mutations.append(("create_policy", arn))
        self.policies[arn] = json.loads(PolicyDocument)
        return self.get_policy(PolicyArn=arn)

    def get_role(self, *, RoleName):
        if RoleName not in self.roles:
            raise Missing()
        return {"Role": deepcopy(self.roles[RoleName])}

    def create_role(self, *, RoleName, AssumeRolePolicyDocument, Description):
        assert RoleName not in self.roles
        self.mutations.append(("create_role", RoleName))
        self.roles[RoleName] = {
            "Arn": f"arn:aws:iam::{self.scenario.child_account_id}:role/" + RoleName,
            "RoleId": "AROA-fixture-" + RoleName,
            "RoleName": RoleName,
            "AssumeRolePolicyDocument": json.loads(AssumeRolePolicyDocument),
        }
        return self.get_role(RoleName=RoleName)

    def list_attached_role_policies(self, *, RoleName):
        return {
            "AttachedPolicies": [
                {"PolicyArn": arn} for arn in self.attachments.get(RoleName, [])
            ]
        }

    def list_role_policies(self, *, RoleName):
        return {"PolicyNames": []}

    def attach_role_policy(self, *, RoleName, PolicyArn):
        assert RoleName in self.roles and PolicyArn in self.policies
        assert RoleName not in self.attachments
        self.mutations.append(("attach_role_policy", RoleName))
        self.attachments[RoleName] = [PolicyArn]
        return {}

    def create_service_linked_role(self, *, AWSServiceName):
        assert AWSServiceName == "autoscaling.amazonaws.com"
        name = "AWSServiceRoleForAutoScaling"
        assert name not in self.roles
        self.mutations.append(("create_service_linked_role", name))
        self.roles[name] = {
            "Arn": f"arn:aws:iam::{self.scenario.child_account_id}:role/aws-service-role/autoscaling.amazonaws.com/"
            + name,
            "RoleId": "AROA-fixture-autoscaling",
            "RoleName": name,
        }
        return self.get_role(RoleName=name)

    def get_public_access_block(self, *, AccountId):
        assert AccountId == self.scenario.child_account_id
        if self.block is None:
            raise Missing("NoSuchPublicAccessBlockConfiguration")
        return {"PublicAccessBlockConfiguration": deepcopy(self.block)}

    def put_public_access_block(self, *, AccountId, PublicAccessBlockConfiguration):
        assert AccountId == self.scenario.child_account_id
        self.mutations.append(("put_public_access_block", AccountId))
        self.block = deepcopy(PublicAccessBlockConfiguration)
        return {}


@pytest.mark.parametrize("interrupted", [False, True])
def test_separately_approved_account_bootstrap_verifies_maintained_registration(
    harness, tmp_path, monkeypatch, interrupted
):
    scenario = AccountScenario(harness, tmp_path, monkeypatch)
    scenario.child = child = Child(scenario)

    async def run():
        creation, source = await scenario.created()
        operation = await scenario.admit(continuation_parameters(source))
        if interrupted:
            from harness_jobs.execution import OperationExecutor

            async def lost_settlement(*args):
                raise OSError(
                    "lost shared settlement after successful bootstrap observation"
                )

            with monkeypatch.context() as isolated:
                isolated.setattr(OperationExecutor, "_settle", lost_settlement)
                with pytest.raises(Exception):
                    await account_runtime.run_account_bootstrap(
                        operation, scenario.context
                    )
            assert len(child.mutations) == 23
        result = await account_runtime.run_account_bootstrap(
            operation, scenario.context
        )
        row = await scenario.row(result["artifact_id"])
        metadata = json.loads(row["artifact_metadata_json"])
        registration = metadata["created_account_registration"]
        assert registration["account_id"] == "000000000003"
        assert registration["operation_id"] == creation.grant.lease.operation_id
        assert (
            registration["organization_id"] == "org-a"
            and registration["workspace_id"] == "ws-1"
        )
        assert metadata["creation_artifact_id"] == source["artifact_id"]
        assert metadata["next_phase"] == "prepare-infrastructure"
        assert scenario.parent == {
            "Id": scenario.request.organizational_unit_id,
            "Type": "ORGANIZATIONAL_UNIT",
        }
        roles = {
            role_name_for(step)
            for step in bootstrap_plan(scenario.request, scenario.authorization).steps
            if isinstance(step.tier, RoleTier)
        }
        assert set(child.roles) == roles | {
            "provider",
            "registrar",
            "installer",
            "supervisor",
            "AWSServiceRoleForAutoScaling",
        }
        assert (
            len(child.policies) == 7
            and len(child.attachments) == 7
            and all(child.block.values())
        )
        assert len(child.mutations) == 23 and len(scenario.accounts) == 1
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    operation.grant.lease.operation_id,
                )
                == "succeeded"
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_effects WHERE event='confirmed'"
                )
                == 24
            )
        parameters = continuation_parameters(row)
        assert parameters["aws_account_id"] == "000000000001"
        infrastructure = await scenario.admit(parameters)
        _, _, _, approved, step = await validate_phase(infrastructure, scenario.context)
        assert approved == row and step.step_id == "prepare-infrastructure"
        assert (
            await account_runtime.child_session(
                infrastructure,
                scenario.context,
                scenario.policy["runtime"],
                scenario.request,
                row,
                scenario.management,
                bootstrap=False,
            )
            is child
        )
        assert scenario.assumed_roles[-1] == "arn:aws:iam::000000000003:role/provider"

    harness.run(run())
