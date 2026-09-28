"""Private account → managed infrastructure handoffs through real admission/RPC.

This test reaches the existing canonical bootstrap adapter boundary. Its refusal
does not claim a ready workspace; canonical bootstrap has its separate composer
suite. Public new-account activation remains closed.
"""

from copy import deepcopy
import json

import pytest

from workspace_provisioning import account_runtime, terraform
from workspace_provisioning.artifacts import continuation_parameters

from .postgres_bridge import requires_harness_postgres
from .test_account_bootstrap_postgres import Child
from .test_account_creation_postgres import (
    AccountScenario,
    harness as lifecycle_harness,
)
from .test_provider_observation import fixture as provider_fixture

pytestmark = requires_harness_postgres
harness = lifecycle_harness


def infrastructure_child(scenario, monkeypatch):
    with monkeypatch.context() as isolated:
        outputs, responses, _, _ = provider_fixture(isolated)
    outputs = json.loads(json.dumps(outputs).replace("000000000002", "000000000003"))
    responses = json.loads(
        json.dumps(responses).replace("000000000002", "000000000003")
    )
    outputs.update(org_id="org-a", workspace_id="ws-1")
    scenario.outputs = {key: {"value": value} for key, value in outputs.items()}

    class Infrastructure:
        def get_caller_identity(self):
            return {"Account": "000000000003"}

        def describe_security_groups(self, *, GroupIds):
            return {
                "SecurityGroups": [
                    {
                        "GroupId": group,
                        "OwnerId": "000000000003",
                        "VpcId": outputs["vpc_id"],
                    }
                    for group in GroupIds
                ]
            }

        def describe_security_group_rules(self, *, Filters):
            groups = Filters[0]["Values"]
            return {
                "SecurityGroupRules": deepcopy(
                    [
                        rule
                        for rule in responses["describe_security_group_rules"][
                            "SecurityGroupRules"
                        ]
                        if rule["GroupId"] in groups
                    ]
                )
            }

        def authorize_security_group_ingress(self, **arguments):
            scenario.creates.append(arguments)
            rule = {
                "SecurityGroupRuleId": "sgr-11111111111111111",
                "GroupId": arguments["GroupId"],
                "GroupOwnerId": "000000000003",
                "IsEgress": False,
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "ReferencedGroupInfo": arguments["IpPermissions"][0][
                    "UserIdGroupPairs"
                ][0],
                "Tags": arguments["TagSpecifications"][0]["Tags"],
            }
            responses["describe_security_group_rules"]["SecurityGroupRules"].append(
                rule
            )
            return {"SecurityGroupRules": [deepcopy(rule)]}

        def __getattr__(self, method):
            assert method in responses
            return lambda **arguments: deepcopy(responses[method])

    class ProviderChild(Child):
        def client(self, service, **kwargs):
            return (
                Infrastructure()
                if service in {"sts", "eks", "ec2"}
                else super().client(service, **kwargs)
            )

    scenario.session = scenario.child = ProviderChild(scenario)


@pytest.mark.parametrize("fault", [None, "changed-plan", "lost-apply-reply"])
def test_account_and_infrastructure_phases_preserve_approved_child_lineage(
    harness, tmp_path, monkeypatch, fault
):
    scenario = AccountScenario(harness, tmp_path, monkeypatch)
    infrastructure_child(scenario, monkeypatch)

    async def run():
        creation, created = await scenario.created()
        bootstrap = await scenario.admit(continuation_parameters(created))
        result = await account_runtime.run_account_bootstrap(
            bootstrap, scenario.context
        )
        bootstrapped = await scenario.row(result["artifact_id"])
        prepare = await scenario.admit(continuation_parameters(bootstrapped))
        result = await account_runtime.run_account_infrastructure(
            prepare, scenario.context
        )
        prepared = await scenario.row(result["artifact_id"])
        assert prepared["account_id"] == "000000000003"
        assert prepare.request.parameters["aws_account_id"] == "000000000001"
        metadata = json.loads(prepared["artifact_metadata_json"])
        assert metadata["creation_artifact_id"] == created["artifact_id"]
        assert (
            metadata["created_account_registration"]["operation_id"]
            == creation.grant.lease.operation_id
        )
        assert scenario.assumed_roles[-1] == "arn:aws:iam::000000000003:role/provider"
        applying = await scenario.admit(continuation_parameters(prepared))
        if fault == "changed-plan":
            _, directory = terraform.verify_prepared_artifact(
                prepared, scenario.context
            )
            path = directory / "workspace.tfplan"
            path.chmod(0o600)
            path.write_text("different reviewed bytes")
        elif fault == "lost-apply-reply":
            scenario.lose_apply_reply = True
        if fault:
            with pytest.raises(Exception):
                await account_runtime.run_account_infrastructure(
                    applying, scenario.context
                )
            effects = list(scenario.process_calls)
            with pytest.raises(Exception):
                await account_runtime.run_account_infrastructure(
                    applying, scenario.context
                )
            assert scenario.process_calls == effects
            assert not scenario.bootstrap_calls
            assert len(scenario.accounts) == 1
            return
        result = await account_runtime.run_account_infrastructure(
            applying, scenario.context
        )
        applied = await scenario.row(result["artifact_id"])
        metadata = json.loads(applied["artifact_metadata_json"])
        assert metadata["creation_artifact_id"] == created["artifact_id"]
        assert (
            metadata["created_account_registration"]
            == json.loads(bootstrapped["artifact_metadata_json"])[
                "created_account_registration"
            ]
        )
        assert (
            metadata["provider_snapshot"]["cluster_arn"].split(":")[4] == "000000000003"
        )
        final = await scenario.admit(continuation_parameters(applied))
        with pytest.raises(Exception):
            await account_runtime.run_account_infrastructure(final, scenario.context)
        assert len(scenario.bootstrap_calls) == 1 and len(scenario.creates) == 1
        assert scenario.bootstrap_calls[0][4] == applied
        assert len(scenario.accounts) == 1
        async with harness.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_artifacts"
                )
                == 4
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workspace_lifecycle_effects WHERE event='confirmed'"
                )
                == 25
            )

    harness.run(run())
