"""Managed identity exceptions are bounded to a reviewed creation plan."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from installation import adapter_staging, producer_role
from installation.config import Refusal


@pytest.fixture
def managed(environment):
    environment["api_producer_role"] = {"api_id": "abcdefghij", "stage": "dev"}
    return environment


def test_default_omission_preserves_external_behavior(environment):
    producer_role.validate(environment)
    producer_role.inspect_plan(SimpleNamespace(env=environment), {})


def test_selected_managed_role_requires_preserved_optin(managed):
    managed["api_adapters"] = {"dispatcher": producer_role.dispatcher(managed)}
    managed.pop("api_producer_role")
    with pytest.raises(Refusal, match="every upgrade"):
        producer_role.validate(managed)


@pytest.fixture
def planned(managed):
    issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
    trust, policy = producer_role.documents(managed, issuer)
    installer = SimpleNamespace(
        env=managed,
        aws=Mock(side_effect=AssertionError("saved-plan inspection must not call AWS")),
        json=Mock(
            side_effect=AssertionError("saved-plan inspection must not parse transport")
        ),
        receipt={
            "api_producer_role_preflight": {
                "role_missing": True,
                "role_arn": producer_role.expected_arn(managed),
                "oidc": issuer,
            }
        },
    )
    plan = {
        "resource_changes": [
            {
                "address": "aws_iam_role.api_producer[0]",
                "change": {
                    "actions": ["create"],
                    "after": {
                        "name": producer_role.expected_name(managed),
                        "path": "/",
                        "assume_role_policy": json.dumps(trust),
                        "managed_policy_arns": [],
                        "inline_policy": [
                            {
                                "name": producer_role.expected_name(managed),
                                "policy": json.dumps(policy),
                            }
                        ],
                    },
                    "after_unknown": {
                        "arn": True,
                        "unique_id": True,
                        "id": True,
                        "inline_policy": [{"name": False, "policy": False}],
                    },
                },
            }
        ]
    }
    return installer, plan


def test_new_role_allows_computed_aws_identity_only(planned):
    producer_role.inspect_plan(*planned)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "different"),
        ("path", "/different/"),
        ("arn", "arn:aws:iam::111111111111:role/different"),
        ("assume_role_policy", "{}"),
        ("managed_policy_arns", ["arn:aws:iam::aws:policy/AdministratorAccess"]),
        ("inline_policy", []),
    ],
)
def test_plan_substitution_refused(planned, field, value):
    installer, plan = planned
    plan["resource_changes"][0]["change"]["after"][field] = value
    with pytest.raises(Refusal):
        producer_role.inspect_plan(installer, plan)


def test_unknown_trust_refused(planned):
    installer, plan = planned
    plan["resource_changes"][0]["change"]["after_unknown"]["assume_role_policy"] = True
    with pytest.raises(Refusal):
        producer_role.inspect_plan(installer, plan)


def test_resume_existing_identity_cannot_be_recreated(planned):
    installer, plan = planned
    installer.receipt["api_producer_role_preflight"].pop("role_missing")
    installer.receipt["api_producer_role_preflight"]["role_id"] = "AROATEST"
    with pytest.raises(Refusal):
        producer_role.inspect_plan(installer, plan)
    plan["resource_changes"][0]["change"]["actions"] = ["no-op"]
    plan["resource_changes"][0]["change"]["after_unknown"] = {}
    plan["resource_changes"][0]["change"]["after"].update(
        arn=producer_role.expected_arn(installer.env), unique_id="AROATEST"
    )
    producer_role.inspect_plan(installer, plan)


def test_another_resource_cannot_attach_to_the_managed_role(planned):
    installer, plan = planned
    plan["resource_changes"].append(
        {
            "address": "aws_iam_role_policy.extra",
            "change": {
                "actions": ["create"],
                "after": {"role": producer_role.expected_name(installer.env)},
            },
        }
    )
    with pytest.raises(Refusal, match="Another Terraform resource"):
        producer_role.inspect_plan(installer, plan)


@pytest.mark.parametrize(
    "reference,accepted",
    [
        ("aws_iam_role.control_plane.id", True),
        ("aws_iam_role.skypilot.name", True),
        ("aws_iam_role.api_producer[0].name", False),
        ("var.external_role", False),
    ],
)
def test_unknown_attachment_target_must_resolve_to_existing_module_roles(
    planned, reference, accepted
):
    installer, plan = planned
    plan["resource_changes"].append(
        {
            "address": "aws_iam_role_policy.own",
            "change": {
                "actions": ["create"],
                "after": {},
                "after_unknown": {"role": True},
            },
        }
    )
    plan["configuration"] = {
        "root_module": {
            "resources": [
                {
                    "address": "aws_iam_role_policy.own",
                    "expressions": {"role": {"references": [reference]}},
                }
            ]
        }
    }
    if accepted:
        producer_role.inspect_plan(installer, plan)
    else:
        with pytest.raises(Refusal, match="Unknown IAM role target"):
            producer_role.inspect_plan(installer, plan)


@pytest.mark.parametrize("selected", [False, True])
def test_every_plan_forwards_desired_role_value(
    tmp_path, environment, release, monkeypatch, selected
):
    from .test_complete_command import setup

    if selected:
        environment["api_producer_role"] = {"api_id": "abcdefghij", "stage": "dev"}
    installer, _ = setup(tmp_path, environment, release, monkeypatch)
    # This test isolates tfvar persistence; exact plan validation is covered above.
    monkeypatch.setattr(producer_role, "inspect_plan", lambda *args: None)
    installer.receipt["api_producer_role_preflight"] = {"role_missing": True}
    for _ in range(2):
        installer.terraform()
        variables = json.loads(
            (
                installer.directory / "terraform" / "installation.auto.tfvars.json"
            ).read_text()
        )
        assert variables["api_producer_role"] == environment.get("api_producer_role")


def test_missing_role_exception_requires_exact_optin_and_error(managed):
    installer = SimpleNamespace(env=copy.deepcopy(managed), json=Mock())
    installer.env["api_adapters"] = {"dispatcher": producer_role.dispatcher(managed)}
    installer.aws = Mock(
        return_value=SimpleNamespace(
            returncode=1,
            stderr="An error occurred (NoSuchEntity) when calling the GetRole operation: not found",
        )
    )
    assert adapter_staging.role_identity(installer, {}, allow_missing=True)[
        "role_missing"
    ]
    with pytest.raises(Refusal):
        adapter_staging.role_identity(installer, {})
    installer.aws.return_value.stderr = "AccessDenied"
    with pytest.raises(Refusal):
        adapter_staging.role_identity(installer, {}, allow_missing=True)


def test_wrong_existing_role_is_never_waived(managed):
    installer = SimpleNamespace(
        env=managed, aws=Mock(return_value=SimpleNamespace(returncode=0))
    )
    managed["api_adapters"] = {"dispatcher": producer_role.dispatcher(managed)}
    installer.json = Mock(
        side_effect=[
            {
                "Role": {
                    "Arn": producer_role.expected_arn(managed),
                    "AssumeRolePolicyDocument": {"Statement": []},
                }
            },
            {"PolicyNames": []},
            {"AttachedPolicies": []},
        ]
    )
    with pytest.raises(Refusal, match="dedicated workload trust"):
        adapter_staging.role_identity(
            installer,
            {"identity": {"oidc": {"issuer": "https://issuer"}}},
            allow_missing=True,
        )


@pytest.mark.parametrize("mismatch", ["arn", "role_id"])
def test_postapply_identity_must_match_output(managed, tmp_path, monkeypatch, mismatch):
    expected = {"arn": producer_role.expected_arn(managed), "role_id": "AROATEST"}
    cluster = {
        "arn": f"arn:aws:eks:{managed['region']}:{managed['account_id']}:cluster/{managed['cluster']}",
        "identity": {"oidc": {"issuer": "https://issuer"}},
    }
    installer = SimpleNamespace(
        env=managed,
        directory=tmp_path,
        commands=SimpleNamespace(call=Mock()),
        aws=Mock(),
        json=Mock(side_effect=[expected, {"cluster": cluster}]),
        receipt={
            "api_producer_role_preflight": {
                "oidc": "https://issuer",
                "role_missing": True,
            }
        },
        save=Mock(),
    )
    live = {"role_arn": expected["arn"], "role_id": expected["role_id"]}
    live["role_arn" if mismatch == "arn" else "role_id"] = "substituted"
    monkeypatch.setattr(adapter_staging, "role_identity", lambda *a, **k: live)
    with pytest.raises(Refusal, match="differs from live identity"):
        producer_role.verify_applied(installer)
    installer.save.assert_not_called()


def test_failed_applied_role_verification_precedes_any_foundations(
    tmp_path, managed, release, monkeypatch
):
    from .test_complete_command import setup

    installer, tools = setup(tmp_path, managed, release, monkeypatch)
    installer.plan()
    monkeypatch.setattr(
        installer, "preflight", lambda: installer.receipt.update(plan_sha256="approved")
    )
    foundations = Mock()
    monkeypatch.setattr(installer, "foundations", foundations)

    def refuse(_):
        raise Refusal("postapply role mismatch")

    monkeypatch.setattr(producer_role, "verify_applied", refuse)
    with pytest.raises(Refusal, match="postapply role mismatch"):
        installer.execute("approved", "verified-user")
    foundations.assert_not_called()
    assert any("apply" in args for args, _ in tools.calls)
    assert tools.route["enabled"] is False
