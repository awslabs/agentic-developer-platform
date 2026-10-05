"""Private STS stays retained; uncertain API ingress creates never replay."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from workspace_provisioning import network
from workspace_provisioning.artifacts import canonical
from workspace_provisioning.runtime_config import LifecycleRefused

from .test_lifecycle_adoption import discover


def setup(monkeypatch):
    request, row, responses, _ = discover(monkeypatch)
    outputs = {
        key: value["value"]
        for key, value in json.loads(row["artifact_metadata_json"])["outputs"].items()
    }
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(org_id="org", workspace_id="workspace")
        ),
        request=SimpleNamespace(
            parameters={
                "lifecycle_request": canonical(
                    {"management_account_id": "000000000001"}
                )
            }
        ),
    )
    config = {"management_security_group_id": "sg-44444444444444444"}
    state = {"effects": {}, "creates": [], "lose_reply": False, "revoked": False}
    rules = copy.deepcopy(
        responses["describe_security_group_rules"]["SecurityGroupRules"]
    )

    class Journal:
        def __init__(self, operation, context, *, phase, recipe):
            assert phase == "bootstrap-workspace"
            assert set(recipe) == {"management-api-rule"}

        async def authority(self):
            if state["revoked"]:
                raise LifecycleRefused("revoked")

        async def intend(self, key, descriptor):
            await self.authority()
            if key in state["effects"]:
                if state["effects"][key] is None:
                    raise LifecycleRefused("ambiguous original intent")
                return state["effects"][key]
            state["effects"][key] = None
            return None

        async def confirm(self, key, descriptor, result):
            await self.authority()
            assert key in state["effects"] and state["effects"][key] is None
            state["effects"][key] = result

        async def complete(self):
            assert set(state["effects"]) == {"management-api-rule"}
            assert all(state["effects"].values())
            return copy.deepcopy(state["effects"])

    class SDK:
        def client(self, service, *, region_name):
            assert region_name == request.region
            assert service in {"ec2", "eks", "sts"}
            return self

        def get_caller_identity(self):
            return {"Account": outputs["account_id"]}

        def describe_cluster(self, **arguments):
            return copy.deepcopy(responses["describe_cluster"])

        def describe_vpc_endpoints(self, **arguments):
            return copy.deepcopy(responses["describe_vpc_endpoints"])

        def describe_security_groups(self, *, GroupIds):
            return {
                "SecurityGroups": [
                    {
                        "GroupId": group,
                        "OwnerId": outputs["account_id"],
                        "VpcId": outputs["vpc_id"],
                    }
                    for group in GroupIds
                ]
            }

        def describe_security_group_rules(self, *, Filters):
            group = Filters[0]["Values"][0]
            return {
                "SecurityGroupRules": copy.deepcopy(
                    [rule for rule in rules if rule["GroupId"] == group]
                )
            }

        def authorize_security_group_ingress(self, **arguments):
            assert state["effects"] == {"management-api-rule": None}
            assert arguments["GroupId"] == outputs["workspace_api_security_group_id"]
            state["creates"].append(arguments)
            permission = arguments["IpPermissions"][0]
            rule = {
                "SecurityGroupRuleId": "sgr-11111111111111111",
                "GroupId": arguments["GroupId"],
                "GroupOwnerId": outputs["account_id"],
                "IsEgress": False,
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "ReferencedGroupInfo": permission["UserIdGroupPairs"][0],
                "Tags": arguments["TagSpecifications"][0]["Tags"],
            }
            rules.append(rule)
            if state["lose_reply"]:
                raise OSError("lost provider reply after create")
            return {"SecurityGroupRules": [copy.deepcopy(rule)]}

    monkeypatch.setattr(network, "LifecycleEffects", Journal)
    return operation, outputs, config, SDK(), state, rules, responses


def run(values):
    operation, outputs, config, session, *_ = values
    return asyncio.run(
        network.establish_network(operation, object(), config, outputs, session)
    )


def test_only_api_rule_is_created_and_untagged_private_sts_is_retained(monkeypatch):
    values = setup(monkeypatch)
    evidence = run(values)
    assert len(values[4]["creates"]) == 1
    assert evidence["private-sts-rule"] == {
        "rule_id": "sgr-0123456789abcdef0",
        "created": False,
    }
    assert evidence["management-api-rule"]["created"] is True
    assert run(values) == evidence
    assert len(values[4]["creates"]) == 1


def test_lost_ingress_reply_never_dispatches_a_second_create(monkeypatch):
    values = setup(monkeypatch)
    values[4]["lose_reply"] = True
    with pytest.raises(OSError, match="lost provider reply"):
        run(values)
    with pytest.raises(LifecycleRefused, match="ambiguous"):
        run(values)
    assert len(values[4]["creates"]) == 1
    assert values[4]["effects"] == {"management-api-rule": None}


@pytest.mark.parametrize(
    "fault", ["missing-sts", "replaced-sts", "cluster-ca", "endpoint-owner", "revoked"]
)
def test_no_new_grant_when_prerequisite_identity_or_authority_changed(
    monkeypatch, fault
):
    values = setup(monkeypatch)
    if fault == "missing-sts":
        values[5].clear()
    elif fault == "replaced-sts":
        values[5][0]["SecurityGroupRuleId"] = "sgr-22222222222222222"
    elif fault == "cluster-ca":
        values[6]["describe_cluster"]["cluster"]["certificateAuthority"]["data"] = (
            "changed"
        )
    elif fault == "endpoint-owner":
        values[6]["describe_vpc_endpoints"]["VpcEndpoints"][0]["OwnerId"] = (
            "000000000003"
        )
    else:
        values[4]["revoked"] = True
    with pytest.raises(LifecycleRefused):
        run(values)
    assert not values[4]["creates"]
