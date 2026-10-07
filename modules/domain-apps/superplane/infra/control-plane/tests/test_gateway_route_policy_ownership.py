"""Regression for the real 50ca3e5 installer plan's app-owned Gateway grant."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from domain_ownership import validate_plan

ACCOUNT = "879318057152"
ADDRESS = "aws_iam_role_policy.gateway_route_read"
# Public identity/permission fields copied from the refused saved AWS plan.
VALUES = {
    "name": "adp-dev-superplane-gateway-route-read",
    "role": "adp-dev-role-gateway-service",
    "policy": json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    "Resource": "arn:aws:s3:::adp-terraform-state-879318057152/domain-routes/dev/superplane/public-route.json",
                }
            ],
        }
    ),
}


def plan(actions=("create",), before=None, after=VALUES, address=ADDRESS):
    return {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": address,
                "change": {
                    "actions": list(actions),
                    "before": copy.deepcopy(before),
                    "after": copy.deepcopy(after),
                    "after_unknown": {"id": True},
                },
            }
        ],
    }


def check(document, **kw):
    return validate_plan(
        document,
        account_id=kw.get("account_id", ACCOUNT),
        environment=kw.get("environment", "dev"),
    )


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
def test_app_policy_owned_but_deletion_still_requires_destructive_gate(actions):
    before = None if actions == ("create",) else VALUES
    after = None if actions == ("delete",) else VALUES
    report = check(plan(actions, before, after))
    assert report.ok
    assert report.has_destructive_changes == ("delete" in actions)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "adp-dev-superplane-another-policy"),
        ("role", "adp-prod-role-gateway-service"),
        ("role", "adp-dev-superplane-control-plane"),
        ("role", "another-gateway"),
        ("role", None),
        ("name", None),
        ("policy", None),
        ("policy", "{}"),
        ("policy", "not-json"),
        ("id", "another-role:another-policy"),
    ],
)
@pytest.mark.parametrize("side", ["before", "after"])
def test_wrong_or_unknown_identity_refused_on_either_side(field, value, side):
    document = plan(("update",), VALUES, VALUES)
    document["resource_changes"][0]["change"][side][field] = value
    assert not check(document).ok


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["Statement"][0].update(Action=["s3:*"]),
        lambda p: p["Statement"][0].update(Action=["s3:GetObject", "s3:PutObject"]),
        lambda p: p["Statement"][0].update(Resource="*"),
        lambda p: p["Statement"][0].update(
            Resource=p["Statement"][0]["Resource"].replace(
                "879318057152", "111111111111"
            )
        ),
        lambda p: p["Statement"][0].update(
            Resource="arn:aws:s3:::adp-terraform-state-879318057152/domain-routes/prod/superplane/public-route.json"
        ),
        lambda p: p["Statement"][0].update(NotAction=["iam:*"]),
        lambda p: p["Statement"].append(
            {"Effect": "Allow", "Action": "*", "Resource": "*"}
        ),
    ],
)
@pytest.mark.parametrize("side", ["before", "after"])
def test_permission_expansion_or_foreign_object_refused(mutation, side):
    document = plan(("update",), VALUES, VALUES)
    policy = json.loads(VALUES["policy"])
    mutation(policy)
    document["resource_changes"][0]["change"][side]["policy"] = json.dumps(policy)
    assert not check(document).ok


@pytest.mark.parametrize("field", ["name", "role", "policy"])
def test_unknown_authority_refused_even_with_residual_known_values(field):
    document = plan()
    document["resource_changes"][0]["change"]["after_unknown"][field] = True
    assert not check(document).ok


@pytest.mark.parametrize("account", [None, "", "111111111111"])
def test_selected_account_required(account):
    assert not check(plan(), account_id=account).ok


def test_selected_environment_and_exact_resource_required():
    assert not check(plan(), environment="prod").ok
    assert not check(plan(address="aws_iam_role_policy.unrelated")).ok
    assert not check(
        plan(address="aws_iam_role.gateway_route_read", after={"name": VALUES["role"]})
    ).ok
    assert check(plan(address="module.superplane." + ADDRESS)).ok


def test_owned_composite_id_and_drift_are_validated():
    values = dict(VALUES, id=VALUES["role"] + ":" + VALUES["name"])
    assert check(plan(("no-op",), values, values)).ok
    document = plan(("no-op",), values, values)
    drift = copy.deepcopy(document["resource_changes"][0])
    drift["change"]["before"]["role"] = "foreign-role"
    document["resource_drift"] = [drift]
    assert not check(document).ok
