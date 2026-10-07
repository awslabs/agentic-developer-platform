"""Managed identity exceptions are bounded to a reviewed creation plan."""

import copy
import json
from pathlib import Path
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


def test_known_legacy_route_upgrade_keeps_role_identity_and_exact_new_policy(planned):
    installer, plan = planned
    evidence = installer.receipt["api_producer_role_preflight"]
    evidence.pop("role_missing")
    evidence.update(role_id="AROATEST", legacy_routes=True)
    change = plan["resource_changes"][0]["change"]
    change["actions"] = ["update"]
    change["after_unknown"] = {}
    change["after"].update(
        arn=producer_role.expected_arn(installer.env), unique_id="AROATEST"
    )
    producer_role.inspect_plan(installer, plan)
    change["after"]["unique_id"] = "REPLACED"
    with pytest.raises(Refusal, match="identity differs"):
        producer_role.inspect_plan(installer, plan)
    change["after"]["unique_id"] = "AROATEST"
    change["after"]["inline_policy"][0]["policy"] = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
        }
    )
    with pytest.raises(Refusal, match="inline policy differs"):
        producer_role.inspect_plan(installer, plan)


def test_identity_preflight_has_exact_readiness_invoke_route(managed):
    _, policy = producer_role.documents(
        managed, "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
    )
    resources = policy["Statement"][0]["Resource"]
    assert {resource.split("/internal/v1/", 1)[1] for resource in resources} == {
        "controller-execution/producer-readiness",
        "controller-execution/verify-run",
        "controller-execution/dispatch",
        "controller-execution/binding-proof",
        "controller-execution/current-identity",
        "controller-execution/current-identity/readiness",
        "credential-evidence",
    }
    assert all("*" not in resource for resource in resources)


@pytest.fixture
def provider_create_plan(planned):
    installer, _ = planned
    installer.env["account_id"] = "111111111111"
    installer.receipt["api_producer_role_preflight"]["role_arn"] = (
        producer_role.expected_arn(installer.env)
    )
    plan = json.loads(
        (
            Path(__file__).parent / "fixtures/producer-role-create-aws-6.67.json"
        ).read_text()
    )
    return installer, plan


def test_real_provider_create_plan_preserves_literal_empty_authority(
    provider_create_plan,
):
    producer_role.inspect_plan(*provider_create_plan)


@pytest.mark.parametrize(
    "expression",
    [
        None,
        {},
        {"constant_value": None},
        {"references": ["var.managed_policy_arns"]},
        {"constant_value": ["arn:aws:iam::aws:policy/AdministratorAccess"]},
        {"constant_value": [], "references": ["var.managed_policy_arns"]},
    ],
)
def test_computed_attachments_require_literal_empty_configuration(
    provider_create_plan, expression
):
    installer, plan = provider_create_plan
    expressions = plan["configuration"]["root_module"]["resources"][0]["expressions"]
    if expression is None:
        expressions.pop("managed_policy_arns")
    else:
        expressions["managed_policy_arns"] = expression
    with pytest.raises(Refusal, match="unknown authority"):
        producer_role.inspect_plan(installer, plan)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", True),
        ("path", True),
        ("assume_role_policy", True),
        ("inline_policy", [{"policy": True}]),
        ("inline_policy", [{"name": True}]),
        ("permissions_boundary", True),
        ("managed_policy_arns", [True]),
        ("name_prefix", {"nested": True}),
    ],
)
def test_provider_computed_exception_does_not_hide_unknown_authority(
    provider_create_plan, field, value
):
    installer, plan = provider_create_plan
    plan["resource_changes"][0]["change"]["after_unknown"][field] = value
    with pytest.raises(Refusal, match="unknown authority"):
        producer_role.inspect_plan(installer, plan)


@pytest.mark.parametrize("field", ["name", "name_prefix"])
def test_computed_prefix_cannot_substitute_an_exact_name(provider_create_plan, field):
    installer, plan = provider_create_plan
    plan["resource_changes"][0]["change"]["after"][field] = "another-role"
    with pytest.raises(Refusal):
        producer_role.inspect_plan(installer, plan)


def test_configured_prefix_is_not_provider_computed_metadata(provider_create_plan):
    installer, plan = provider_create_plan
    plan["configuration"]["root_module"]["resources"][0]["expressions"][
        "name_prefix"
    ] = {"references": ["var.prefix"]}
    with pytest.raises(Refusal, match="unknown authority"):
        producer_role.inspect_plan(installer, plan)


@pytest.mark.parametrize("actions", [["update"], ["no-op"]])
@pytest.mark.parametrize("field", ["managed_policy_arns", "name_prefix"])
def test_computed_creation_exception_never_applies_to_existing_identity(
    provider_create_plan, actions, field
):
    installer, plan = provider_create_plan
    evidence = installer.receipt["api_producer_role_preflight"]
    evidence.pop("role_missing")
    evidence.update(role_id="AROATEST", legacy_routes=actions == ["update"])
    change = plan["resource_changes"][0]["change"]
    change["actions"] = actions
    change["after"].update(
        arn=producer_role.expected_arn(installer.env), unique_id="AROATEST"
    )
    change["after_unknown"] = {field: True}
    with pytest.raises(Refusal, match="unknown authority"):
        producer_role.inspect_plan(installer, plan)


def test_computed_creation_exception_requires_absent_prior_identity(
    provider_create_plan,
):
    installer, plan = provider_create_plan
    plan["resource_changes"][0]["change"]["before"] = {"name": "old-role"}
    with pytest.raises(Refusal, match="prior Terraform identity"):
        producer_role.inspect_plan(installer, plan)


@pytest.mark.parametrize("unknown_target", [False, True])
def test_computed_empty_set_never_permits_separate_attachment(
    provider_create_plan, unknown_target
):
    installer, plan = provider_create_plan
    plan["resource_changes"].append(
        {
            "address": "aws_iam_role_policy_attachment.extra",
            "change": {
                "actions": ["create"],
                "after": {}
                if unknown_target
                else {"role": producer_role.expected_name(installer.env)},
                "after_unknown": {"role": True} if unknown_target else {},
            },
        }
    )
    plan["configuration"]["root_module"]["resources"].append(
        {
            "address": "aws_iam_role_policy_attachment.extra",
            "expressions": {
                "role": {"references": ["aws_iam_role.api_producer[0].name"]}
            },
        }
    )
    with pytest.raises(Refusal, match="Terraform resource|Unknown IAM role target"):
        producer_role.inspect_plan(installer, plan)


@pytest.mark.parametrize("managed_role", [True, False])
def test_live_empty_attachment_contract_is_specific_to_managed_role(
    managed, managed_role
):
    env = copy.deepcopy(managed)
    env["api_adapters"] = {"dispatcher": producer_role.dispatcher(env)}
    if not managed_role:
        env.pop("api_producer_role")
        env["api_adapters"]["dispatcher"]["role_arn"] = (
            "arn:aws:iam::879318057152:role/external-api-producer"
        )
    issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
    trust, policy = producer_role.documents(managed, issuer)
    installer = SimpleNamespace(
        env=env,
        aws=Mock(return_value=SimpleNamespace(returncode=0)),
        json=Mock(
            side_effect=[
                {
                    "Role": {
                        "Arn": env["api_adapters"]["dispatcher"]["role_arn"],
                        "RoleId": "AROATEST",
                        "AssumeRolePolicyDocument": trust,
                    }
                },
                {"PolicyNames": ["exact-inline"]},
                {"PolicyDocument": policy},
                {
                    "AttachedPolicies": [
                        {"PolicyArn": "arn:aws:iam::879318057152:policy/same-routes"}
                    ]
                },
                {"Policy": {"DefaultVersionId": "v1"}},
                {"PolicyVersion": {"Document": policy}},
            ]
        ),
    )
    if managed_role:
        with pytest.raises(Refusal, match="has attached policies"):
            adapter_staging.role_identity(
                installer, {"identity": {"oidc": {"issuer": issuer}}}
            )
    else:
        assert (
            adapter_staging.role_identity(
                installer, {"identity": {"oidc": {"issuer": issuer}}}
            )["role_id"]
            == "AROATEST"
        )
