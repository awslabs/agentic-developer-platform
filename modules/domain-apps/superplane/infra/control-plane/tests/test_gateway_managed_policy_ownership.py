"""The shared Gateway role can exhaust AWS's fixed 10 KiB inline-policy quota."""

import copy
import json

import pytest

from test_gateway_route_policy_ownership import VALUES, check

POLICY = "aws_iam_policy.gateway_route_read"
ATTACHMENT = "aws_iam_role_policy_attachment.gateway_route_read"
ARN = "arn:aws:iam::879318057152:policy/" + VALUES["name"]
MANAGED = {"name": VALUES["name"], "path": "/", "policy": VALUES["policy"]}
ATTACHED = {"role": VALUES["role"], "policy_arn": ARN}


def plan(actions=("create",), prefix=""):
    return {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": prefix + address,
                "change": {
                    "actions": list(actions),
                    "before": None if actions == ("create",) else copy.deepcopy(values),
                    "after": None if actions == ("delete",) else copy.deepcopy(values),
                    "after_unknown": {
                        "id": True,
                        **({"arn": True} if address == POLICY else {}),
                    },
                },
            }
            for address, values in [(POLICY, MANAGED), (ATTACHMENT, ATTACHED)]
        ],
    }


@pytest.mark.parametrize(
    "actions",
    [
        ("create",),
        ("no-op",),
        ("update",),
        ("delete",),
        ("delete", "create"),
        ("create", "delete"),
    ],
)
@pytest.mark.parametrize("prefix", ["", "module.superplane."])
def test_exact_managed_grant_and_attachment(actions, prefix):
    report = check(plan(actions, prefix))
    assert report.ok
    assert report.has_destructive_changes == ("delete" in actions)


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "name", "adp-dev-superplane-other"),
        (0, "path", "/other/"),
        (0, "arn", ARN.replace("879318057152", "111111111111")),
        (0, "id", "foreign-policy"),
        (0, "policy", "{}"),
        (0, "policy", None),
        (1, "role", "adp-prod-role-gateway-service"),
        (1, "role", "adp-dev-superplane-control-plane"),
        (1, "role", None),
        (1, "policy_arn", ARN.replace("879318057152", "111111111111")),
        (1, "policy_arn", "arn:aws:iam::aws:policy/AdministratorAccess"),
        (1, "policy_arn", None),
    ],
)
def test_wrong_identity_refused_on_both_sides(side, index, field, value):
    p = plan(("update",))
    p["resource_changes"][index]["change"][side][field] = value
    assert not check(p).ok


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize(
    "field,value", [("Action", ["s3:*"]), ("Resource", "*"), ("NotAction", ["iam:*"])]
)
def test_policy_permission_expansion_refused(side, field, value):
    p = plan(("update",))
    policy = json.loads(VALUES["policy"])
    policy["Statement"][0][field] = value
    p["resource_changes"][0]["change"][side]["policy"] = json.dumps(policy)
    assert not check(p).ok


@pytest.mark.parametrize(
    "index,field",
    [(0, "name"), (0, "path"), (0, "policy"), (1, "role"), (1, "policy_arn")],
)
def test_unknown_authority_refused(index, field):
    p = plan()
    p["resource_changes"][index]["change"]["after_unknown"][field] = True
    assert not check(p).ok


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["resource_changes"].pop(0),
        lambda p: p["resource_changes"].append(copy.deepcopy(p["resource_changes"][0])),
        lambda p: p["resource_changes"][0].update(address="module.other." + POLICY),
        lambda p: p["resource_changes"][0]["change"].update(before=None),
        lambda p: p["resource_changes"][0]["change"].update(after=None),
    ],
)
def test_attachment_requires_unambiguous_same_module_policy_on_both_sides(mutation):
    p = plan(("update",))
    mutation(p)
    assert not check(p).ok


def test_existing_unchanged_managed_policy_can_be_attached():
    p = plan()
    p["resource_changes"][0]["change"].update(actions=["no-op"], before=MANAGED)
    assert check(p).ok


def test_legacy_inline_removal_remains_destructive():
    p = plan()
    p["resource_changes"].append(
        {
            "address": "aws_iam_role_policy.gateway_route_read",
            "change": {"actions": ["delete"], "before": VALUES, "after": None},
        }
    )
    report = check(p)
    assert report.ok and report.has_destructive_changes


def test_wrong_account_environment_and_attachment_address_refused():
    assert not check(plan(), account_id="111111111111").ok
    assert not check(plan(), environment="prod").ok
    p = plan()
    p["resource_changes"][1]["address"] = "aws_iam_role_policy_attachment.unrelated"
    assert not check(p).ok
