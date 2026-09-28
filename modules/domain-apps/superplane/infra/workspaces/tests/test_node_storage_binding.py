"""Storage cannot silently disappear from a claimed node-capacity upper bound."""

import copy
import pytest

from test_plan_safety import REGION, TEST_KMS_KEY, _launch_template, _node_group, _plan
from check_workspace_plan import _estimate
from workspace_ownership import WorkspaceOwnershipError


@pytest.mark.parametrize("action", ["create", "update", "no-op"])
def test_known_encrypted_root_is_counted_for_each_resulting_action(action):
    plan = _plan(_node_group((action,), max_size=10), _launch_template((action,)))
    estimate = _estimate(plan, aws_region=REGION)
    disk = next(
        x for x in estimate["components"] if x["component"].startswith("Node root EBS")
    )
    assert disk["monthly_usd"] == pytest.approx(40)


@pytest.mark.parametrize(
    "problem",
    [
        "no-template",
        "absent-mappings",
        "empty-mappings",
        "wrong-device",
        "unknown-size",
        "unencrypted",
        "unknown-encryption",
        "wrong-key",
        "unknown-key",
        "foreign-template",
        "wrong-version",
        "floating-version",
        "extra-disk",
    ],
)
def test_unbound_or_unpriced_root_disk_is_refused(problem):
    plan = _plan(_node_group(max_size=10), _launch_template())
    node, template = plan["resource_changes"][:2]
    values = template["change"]["after"]
    ebs = values["block_device_mappings"][0]["ebs"][0]
    if problem == "no-template":
        plan["resource_changes"].remove(template)
    elif problem == "absent-mappings":
        del values["block_device_mappings"]
    elif problem == "empty-mappings":
        values["block_device_mappings"] = []
    elif problem == "wrong-device":
        values["block_device_mappings"][0]["device_name"] = "/dev/other"
    elif problem == "unknown-size":
        del ebs["volume_size"]
    elif problem == "unencrypted":
        ebs["encrypted"] = False
    elif problem == "unknown-encryption":
        del ebs["encrypted"]
    elif problem == "wrong-key":
        ebs["kms_key_id"] = TEST_KMS_KEY + "-other"
    elif problem == "unknown-key":
        del ebs["kms_key_id"]
    elif problem == "foreign-template":
        node["change"]["after"]["launch_template"][0]["id"] = "lt-other"
    elif problem in ("wrong-version", "floating-version"):
        node["change"]["after"]["launch_template"][0]["version"] = (
            "2" if problem == "wrong-version" else "$Latest"
        )
    else:
        values["block_device_mappings"].append(
            copy.deepcopy(values["block_device_mappings"][0])
        )
    with pytest.raises(WorkspaceOwnershipError):
        _estimate(plan, aws_region=REGION)


def test_native_owned_create_accepts_computed_ids_bound_by_verified_configuration():
    plan = _plan(_node_group(max_size=10), _launch_template())
    plan["variables"]["kms_key_arn"]["value"] = ""
    node, template = plan["resource_changes"][:2]
    node["change"]["after"]["launch_template"] = [{}]
    del template["change"]["after"]["block_device_mappings"][0]["ebs"][0]["kms_key_id"]
    template["change"]["after_unknown"] = {
        "block_device_mappings": [{"ebs": [{"kms_key_id": True}]}]
    }
    plan["resource_changes"].append(
        {
            "address": "aws_kms_key.workspace[0]",
            "change": {
                "actions": ["create"],
                "after": {},
                "after_unknown": {"arn": True},
            },
        }
    )
    plan["configuration"] = {
        "root_module": {
            "resources": [
                {
                    "address": template["address"],
                    "expressions": {
                        "block_device_mappings": [
                            {
                                "ebs": [
                                    {
                                        "kms_key_id": {
                                            "references": ["local.kms_key_arn"]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                },
                {
                    "address": node["address"],
                    "expressions": {
                        "launch_template": [
                            {
                                "id": {
                                    "references": [
                                        "aws_launch_template.node.id",
                                        "aws_launch_template.node",
                                    ]
                                },
                                "version": {
                                    "references": [
                                        "aws_launch_template.node.latest_version",
                                        "aws_launch_template.node",
                                    ]
                                },
                            }
                        ]
                    },
                },
            ]
        }
    }
    assert _estimate(plan, aws_region=REGION)["bounded_monthly_usd"] > 0
    plan["configuration"]["root_module"]["resources"][0]["expressions"][
        "block_device_mappings"
    ][0]["ebs"][0]["kms_key_id"] = {"references": ["local.other_key"]}
    with pytest.raises(WorkspaceOwnershipError, match="binding"):
        _estimate(plan, aws_region=REGION)


@pytest.mark.parametrize(
    "field,value",
    [("iops", 16000), ("throughput", 1000), ("volume_initialization_rate", 300)],
)
def test_purchased_gp3_extras_are_not_omitted_from_baseline_estimate(field, value):
    plan = _plan(_node_group(), _launch_template())
    plan["resource_changes"][1]["change"]["after"]["block_device_mappings"][0]["ebs"][
        0
    ][field] = value
    with pytest.raises(WorkspaceOwnershipError, match="baseline"):
        _estimate(plan, aws_region=REGION)
