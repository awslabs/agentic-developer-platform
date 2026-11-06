"""Setup refusal paths and environment isolation; no cloud or GitHub calls."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.cli_uplift import cleanup, config, stages

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "evaluator_setup", ROOT / "platform/scripts/setup-evaluator-credentials.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def inputs():
    account = "111122223333"
    args = SimpleNamespace(
        account=account,
        repository="example/adp",
        environment="staging",
        state_bucket="private-state",
    )
    bindings = json.loads(config.EXAMPLE_PATH.read_text())
    bindings.pop("_comment", None)
    bindings.update(
        platform_account=account,
        destination_account="444455556666",
        gateway_deployment="staging",
        credential_secret_name="adp/dev/gateway/test-admin-credentials",
        instance_security_group_id="sg-0000000000000003a",
    )
    bindings.pop("instance_profile")
    catalog = {
        "staging": {
            "account": account,
            "region": "us-east-1",
            "gateway_url": bindings["gateway_url"],
            "cluster": "adp-dev-eks-cluster",
            "cluster_arn": f"arn:aws:eks:us-east-1:{account}:cluster/adp-dev-eks-cluster",
            "namespace": "adp-gateway",
            "deployment": "bedrockgateway",
            "service": "bedrockgateway",
            "container": "bedrockgateway",
            "selector": {"app": "bedrockgateway"},
            "images": {},
            "image_repository": f"{account}.dkr.ecr.us-east-1.amazonaws.com/adp-gateway",
        }
    }
    return args, bindings, catalog


def test_generates_repeatable_distinct_names_and_complete_private_bindings(inputs):
    args, bindings, catalog = inputs
    cfg, selected, name, roles = setup.validate_inputs(args, bindings, catalog)
    assert cfg["evaluation_target"] == name
    assert cfg["instance_profile"] == name + "-instance"
    assert cfg["state_bucket"].endswith(args.account)
    assert len(cfg["state_bucket"]) <= 63
    assert setup.validate_inputs(args, cfg, catalog)[0] == cfg
    args.environment = "another-environment"
    assert setup.validate_inputs(args, bindings, catalog)[2] != name
    args.environment = "a" * 63
    assert len(setup.validate_inputs(args, bindings, catalog)[0]["state_bucket"]) <= 63
    assert selected == catalog["staging"] and roles == []


def test_core_setup_needs_no_destination_and_clears_example_account(inputs):
    args, bindings, catalog = inputs
    bindings.pop("destination_account")
    cfg, _, _, roles = setup.validate_inputs(args, bindings, catalog)
    effective = config.from_environment(
        {"CLI_UPLIFT_EVAL_BINDINGS_JSON": json.dumps(cfg)}
    )
    assert effective["destination_account"] is None
    assert roles == []
    from tests.regression import environment_smoke

    config.require_bindings(
        effective, environment_smoke.selection()["cli_scope"].split(",")
    )
    with pytest.raises(config.ConfigError, match="bindings"):
        config.require_bindings(effective, ["case:E08"])


@pytest.mark.parametrize(
    "field",
    ["destination_role_arn", "provisioner_role_arn", "second_destination_account"],
)
def test_destination_fixtures_still_require_explicit_account(inputs, field):
    args, bindings, catalog = inputs
    bindings.pop("destination_account")
    bindings[field] = (
        "444455556666"
        if field == "second_destination_account"
        else "arn:aws:iam::444455556666:role/test"
    )
    with pytest.raises(config.ConfigError, match="requires destination_account"):
        setup.validate_inputs(args, bindings, catalog)


@pytest.mark.parametrize(
    "field,value",
    [
        ("platform_account", "999900001111"),
        ("admin_password", "do-not-print"),
        ("destination_role_arn", "arn:aws:iam::444455556666:role/*"),
        ("instance_profile", "administrator"),
        ("state_bucket", "another-target"),
        ("evaluation_target", "bad\ntag"),
        ("state_kms_key_id", "alias/old"),
    ],
)
def test_refuses_wrong_or_unsafe_bindings(inputs, field, value):
    args, bindings, catalog = inputs
    bindings[field] = value
    with pytest.raises((setup.SetupError, config.ConfigError)):
        setup.validate_inputs(args, bindings, catalog)


@pytest.mark.parametrize("field", config.REQUIRED)
def test_does_not_fill_missing_private_identity_from_public_examples(inputs, field):
    args, bindings, catalog = inputs
    del bindings[field]
    with pytest.raises(setup.SetupError, match="explicitly"):
        setup.validate_inputs(args, bindings, catalog)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account", "999900001111"),
        ("cluster_arn", "arn:aws:eks:us-east-1:999900001111:cluster/other"),
        ("gateway_url", "https://other.example/api"),
        ("namespace", "../secrets"),
        ("images", {"unreviewed": {}}),
    ],
)
def test_refuses_foreign_or_unusable_catalog(inputs, field, value):
    args, bindings, catalog = inputs
    catalog["staging"][field] = value
    with pytest.raises(setup.SetupError):
        setup.validate_inputs(args, bindings, catalog)


def test_account_mismatch_precedes_every_resource_read_or_mutation(inputs):
    args, bindings, catalog = inputs
    cfg, selected, name, _ = setup.validate_inputs(args, bindings, catalog)
    session = Mock()
    session.client.return_value.get_caller_identity.return_value = {
        "Account": "999900001111"
    }
    with pytest.raises(setup.SetupError, match="Active AWS identity"):
        setup.discover(session, args, cfg, selected, name)
    session.client.assert_called_once_with("sts")


def protected_response():
    return {
        "environments": [
            {
                "name": "staging",
                "deployment_branch_policy": {
                    "custom_branch_policies": True,
                    "protected_branches": False,
                },
                "protection_rules": [{"type": "required_reviewers"}],
            }
        ]
    }


def test_existing_environment_keeps_reviewers_and_other_protections(monkeypatch):
    api = Mock(
        side_effect=[
            protected_response(),
            {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]},
        ]
    )
    monkeypatch.setattr(setup, "gh_api", api)
    setup.protect_environment("example/adp", "staging")
    assert all("method" not in call.kwargs for call in api.call_args_list)


@pytest.mark.parametrize(
    "policies",
    [
        [],
        [{"name": "*", "type": "branch"}],
        [{"name": "main", "type": "tag"}],
        [{"name": "main", "type": "branch"}, {"name": "other", "type": "branch"}],
    ],
)
def test_refuses_unrestricted_environment_without_changing_it(monkeypatch, policies):
    api = Mock(
        side_effect=[
            protected_response(),
            {"total_count": len(policies), "branch_policies": policies},
        ]
    )
    monkeypatch.setattr(setup, "gh_api", api)
    with pytest.raises(setup.SetupError, match="exactly the main"):
        setup.protect_environment("example/adp", "staging")
    assert api.call_count == 2


def test_missing_environment_is_protected_before_use(monkeypatch):
    api = Mock(
        side_effect=[
            {"environments": []},
            {},
            {},
            protected_response(),
            {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]},
        ]
    )
    monkeypatch.setattr(setup, "gh_api", api)
    setup.protect_environment("example/adp", "staging")
    assert api.call_args_list[1].kwargs["body"]["deployment_branch_policy"] == {
        "protected_branches": False,
        "custom_branch_policies": True,
    }
    assert api.call_args_list[2].kwargs["body"] == {"name": "main", "type": "branch"}


@pytest.mark.parametrize(
    "actions", [["delete"], ["delete", "create"], ["create", "delete"]]
)
def test_never_applies_destructive_plan(actions):
    with pytest.raises(setup.SetupError, match="delete or replace"):
        setup.reject_destructive_plan(
            {"resource_changes": [{"change": {"actions": actions}}]}
        )
    setup.reject_destructive_plan(
        {"resource_changes": [{"change": {"actions": ["update"]}}]}
    )


def test_secret_values_only_enter_stdin_and_role_is_published_last(monkeypatch):
    command = Mock(return_value="")
    monkeypatch.setattr(setup, "run", command)
    setup.publish(
        "example/adp",
        "staging",
        {"private": "sentinel-private-value"},
        {},
        "role-sentinel",
    )
    assert len(command.call_args_list) == 3
    assert command.call_args_list[-1].args[0][3] == "AWS_CLI_UPLIFT_EVAL_ROLE_ARN"
    assert all("sentinel" not in str(call.args) for call in command.call_args_list)
    assert "sentinel-private-value" in command.call_args_list[0].kwargs["data"]


def test_subprocess_failure_does_not_expose_private_output(monkeypatch):
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        Mock(
            return_value=SimpleNamespace(
                returncode=1, stderr="private-secret", stdout="private-secret"
            )
        ),
    )
    with pytest.raises(setup.SetupError) as error:
        setup.run(["gh", "secret", "set"], data="private-secret")
    assert "private-secret" not in str(error.value)


def test_observer_cannot_read_secrets_list_pods_or_mutate_objects(inputs):
    args, bindings, catalog = inputs
    _, selected, name, _ = setup.validate_inputs(args, bindings, catalog)
    role, binding = setup.observer_objects(selected, name)["items"]
    assert [
        (r["resources"], r["verbs"], r["resourceNames"]) for r in role["rules"]
    ] == [
        (["deployments"], ["get"], ["bedrockgateway"]),
        (["services"], ["get"], ["bedrockgateway"]),
    ]
    assert binding["roleRef"]["kind"] == "Role"
    assert binding["subjects"][0]["name"] == "adp:evaluator:" + name


def test_ec2_launch_carries_environment_ownership(inputs):
    args, bindings, catalog = inputs
    cfg, *_ = setup.validate_inputs(args, bindings, catalog)
    aws = Mock()
    aws.call.side_effect = [
        {"Parameter": {"Value": "ami-00000000000000000"}},
        {"Instances": [{"InstanceId": "i-00000000000000000"}]},
    ]
    ctx = {
        "attempt_id": "adp-e2e-20261008-000000-abcdef-a1",
        "evaluation_id": "adp-e2e-20261008-000000-abcdef",
        "manifest": Mock(),
        "document": {},
        "correlation": {},
        "fault": "",
    }
    stages.ec2_stage(
        cfg,
        {
            "aws": aws,
            "wait_online": lambda _: True,
            "install_bundle": lambda *_: {"purposes": ["login"]},
        },
    )(ctx)
    launch = next(
        c.kwargs
        for c in aws.call.call_args_list
        if c.args[:2] == ("ec2", "run_instances")
    )
    tags = {t["Key"]: t["Value"] for t in launch["TagSpecifications"][0]["Tags"]}
    assert tags["adp:evaluator-target"] == cfg["evaluation_target"]
    assert tags[cleanup.OWNER_TAG] == ctx["evaluation_id"]


def test_recovery_never_selects_another_environments_expired_instances():
    def instance(identity, target):
        return {
            "InstanceId": identity,
            "State": {"Name": "running"},
            "LaunchTime": 1,
            "Tags": [
                {"Key": cleanup.OWNER_TAG, "Value": "adp-e2e-20261008-000000-abcdef"},
                {"Key": "adp:evaluator-target", "Value": target},
            ],
        }

    rows = [
        instance("i-own", "staging"),
        instance("i-foreign", "another"),
        instance("i-legacy", ""),
    ]
    found = cleanup.recoverable_instances(
        rows, now=100000, ttl_minutes=240, target="staging"
    )
    assert [row["id"] for row in found] == ["i-own"]
    assert len(cleanup.recoverable_instances(rows, now=100000, ttl_minutes=240)) == 3


def test_conflicting_repository_variable_cannot_redirect_new_target(monkeypatch):
    api = Mock(
        return_value={
            "variables": [
                {"name": "CLI_UPLIFT_EVAL_STATE_BUCKET", "value": "wrong-bucket"}
            ]
        }
    )
    monkeypatch.setattr(setup, "gh_api", api)
    with pytest.raises(
        setup.SetupError, match="Conflicting CLI_UPLIFT_EVAL_STATE_BUCKET"
    ):
        setup.check_variable_overrides(
            "example/adp", "staging", False, {"state_bucket": "correct-bucket"}
        )


def test_environment_variable_takes_precedence_over_repository_variable(monkeypatch):
    monkeypatch.setattr(
        setup,
        "gh_api",
        Mock(
            side_effect=[
                {
                    "variables": [
                        {"name": "CLI_UPLIFT_EVAL_STATE_BUCKET", "value": "wrong"}
                    ]
                },
                {
                    "variables": [
                        {"name": "CLI_UPLIFT_EVAL_STATE_BUCKET", "value": "correct"}
                    ]
                },
            ]
        ),
    )
    setup.check_variable_overrides(
        "example/adp", "staging", True, {"state_bucket": "correct"}
    )


@pytest.mark.parametrize(
    "apply,destructive", [(False, False), (True, False), (True, True)]
)
@pytest.mark.parametrize("resource_namespace", [None, "dev"])
def test_complete_setup_orders_access_before_secret_publication(
    inputs, tmp_path, monkeypatch, apply, destructive, resource_namespace
):
    import sys

    args, bindings, catalog = inputs
    cfg, selected, name, _ = setup.validate_inputs(args, bindings, catalog)
    bindings_path, catalog_path = tmp_path / "input.json", tmp_path / "catalog.json"
    bindings_path.write_text(json.dumps(bindings))
    catalog_path.write_text(json.dumps(catalog))
    out = tmp_path / "output"
    commands = []
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=Mock()))
    monkeypatch.setattr(
        setup,
        "discover",
        Mock(
            return_value=(
                "arn:aws:secretsmanager:us-east-1:111122223333:secret:fixture-AbCdEf",
                "",
                False,
                {},
            )
        ),
    )
    monkeypatch.setattr(setup, "environment_status", Mock(return_value=True))
    monkeypatch.setattr(setup, "check_variable_overrides", Mock())
    monkeypatch.setattr(
        setup, "gh_api", Mock(return_value={"name": "receipt", "updated_at": "now"})
    )
    monkeypatch.setattr(setup, "check_kubernetes", Mock(return_value=["kubectl"]))
    monkeypatch.setattr(
        setup, "protect_environment", lambda *_: commands.append(["protect"])
    )
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    def command(argv, *, data=None):
        commands.append(argv)
        if "show" in argv:
            return json.dumps(
                {
                    "resource_changes": [
                        {
                            "change": {
                                "actions": ["delete"] if destructive else ["create"]
                            }
                        }
                    ]
                }
            )
        if "output" in argv:
            return json.dumps(
                {
                    "role_arn": {
                        "value": f"arn:aws:iam::{args.account}:role/{name}-oidc"
                    },
                    "instance_profile": {"value": cfg["instance_profile"]},
                    "state_bucket": {"value": cfg["state_bucket"]},
                    "state_kms_key_id": {"value": cfg["state_kms_key_id"]},
                }
            )
        return ""

    monkeypatch.setattr(setup, "run", command)
    argv = [
        "--account",
        args.account,
        "--repository",
        args.repository,
        "--environment",
        args.environment,
        "--bindings",
        str(bindings_path),
        "--catalog",
        str(catalog_path),
        "--state-bucket",
        args.state_bucket,
        "--output-dir",
        str(out),
    ]
    if apply:
        argv += ["--apply", "--kubeconfig", str(tmp_path / "kubeconfig")]
    if resource_namespace:
        argv += ["--resource-environment", resource_namespace]
    previous_umask = __import__("os").umask(0o077)
    try:
        if destructive:
            with pytest.raises(setup.SetupError, match="delete or replace"):
                setup.main(argv)
            assert not any(
                "apply" in c or "secret" in c or c == ["protect"] for c in commands
            )
            return
        assert setup.main(argv) == 0
    finally:
        __import__("os").umask(previous_umask)
    assert (out / "bindings.json").stat().st_mode & 0o777 == 0o600
    assert out.stat().st_mode & 0o777 == 0o700
    variables = json.loads((out / "terraform.tfvars.json").read_text())["config"]
    assert variables["target"] == args.environment
    assert variables["resource_environment"] == (resource_namespace or args.environment)
    if not apply:
        assert commands == []
        return
    tf_apply = next(
        i for i, c in enumerate(commands) if c[0] == "terraform" and "apply" in c
    )
    rbac = next(i for i, c in enumerate(commands) if c[0] == "kubectl")
    secret = next(i for i, c in enumerate(commands) if c[:2] == ["gh", "secret"])
    assert commands.index(["protect"]) < tf_apply < rbac < secret
    assert "setup.tfplan" in commands[tf_apply]
    assert commands[-1][3] == "AWS_CLI_UPLIFT_EVAL_ROLE_ARN"


def test_output_directory_cannot_retarget_or_load_unrelated_terraform(tmp_path):
    target = {"account": "111122223333", "environment": "staging"}
    setup.validate_output_directory(tmp_path, target)
    (tmp_path / "unrelated.tf").write_text('resource "aws_iam_role" "unrelated" {}')
    with pytest.raises(setup.SetupError, match="no setup marker"):
        setup.validate_output_directory(tmp_path, target)
    (tmp_path / "setup-target.json").write_text(json.dumps(target))
    with pytest.raises(setup.SetupError, match="unrelated Terraform"):
        setup.validate_output_directory(tmp_path, target)
    (tmp_path / "unrelated.tf").unlink()
    setup.validate_output_directory(tmp_path, target)
    with pytest.raises(setup.SetupError, match="another setup"):
        setup.validate_output_directory(tmp_path, {**target, "account": "999900001111"})
    setup.validate_output_directory(
        tmp_path, {**target, "resource_environment": "staging"}
    )
    with pytest.raises(setup.SetupError, match="another setup"):
        setup.validate_output_directory(
            tmp_path, {**target, "resource_environment": "dev"}
        )


@pytest.mark.parametrize("namespace", ["../dev", "/dev", "dev/gateway", "Prod"])
def test_rejects_invalid_resource_namespace(inputs, namespace):
    args, bindings, catalog = inputs
    args.resource_environment = namespace
    with pytest.raises(setup.SetupError, match="Resource environment"):
        setup.validate_inputs(args, bindings, catalog)


def test_tightens_unrestricted_environment_without_removing_reviewers(monkeypatch):
    unrestricted = {
        "environments": [
            {
                "name": "staging",
                "deployment_branch_policy": None,
                "can_admins_bypass": False,
                "protection_rules": [
                    {"type": "wait_timer", "wait_timer": 20},
                    {
                        "type": "required_reviewers",
                        "prevent_self_review": True,
                        "reviewers": [{"type": "Team", "reviewer": {"id": 123}}],
                    },
                ],
            }
        ]
    }
    api = Mock(
        side_effect=[
            unrestricted,
            {},
            {},
            protected_response(),
            {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]},
        ]
    )
    monkeypatch.setattr(setup, "gh_api", api)
    setup.protect_environment("example/adp", "staging")
    body = api.call_args_list[1].kwargs["body"]
    assert body["reviewers"] == [{"type": "Team", "id": 123}]
    assert body["wait_timer"] == 20
    assert body["prevent_self_review"] is True
    assert body["can_admins_bypass"] is False


@pytest.mark.parametrize("provider_state", ["missing", "owned", "shared"])
def test_discovery_checks_metadata_without_reading_credentials_and_preserves_oidc_ownership(
    inputs, provider_state
):
    args, bindings, catalog = inputs
    cfg, selected, name, _ = setup.validate_inputs(args, bindings, catalog)
    clients = {
        key: Mock()
        for key in (
            "sts",
            "s3",
            "ec2",
            "secretsmanager",
            "cognito-idp",
            "eks",
            "ecr",
            "iam",
        )
    }
    session = SimpleNamespace(client=lambda key: clients[key])
    clients["sts"].get_caller_identity.return_value = {"Account": args.account}
    clients["s3"].head_bucket.return_value = {"BucketRegion": cfg["region"]}
    clients["ec2"].describe_subnets.return_value = {
        "Subnets": [
            {
                "OwnerId": args.account,
                "VpcId": cfg["vpc_id"],
                "MapPublicIpOnLaunch": False,
            }
        ]
    }
    clients["ec2"].describe_security_groups.return_value = {
        "SecurityGroups": [
            {"OwnerId": args.account, "VpcId": cfg["vpc_id"], "IpPermissions": []}
        ]
    }
    fixture = f"arn:aws:secretsmanager:us-east-1:{args.account}:secret:{cfg['credential_secret_name']}-AbCdEf"
    clients["secretsmanager"].describe_secret.return_value = {
        "Name": cfg["credential_secret_name"],
        "ARN": fixture,
    }
    clients["cognito-idp"].describe_user_pool.return_value = {
        "UserPool": {
            "Arn": f"arn:aws:cognito-idp:us-east-1:{args.account}:userpool/{cfg['cognito_user_pool_id']}"
        }
    }
    cluster = {
        "arn": selected["cluster_arn"],
        "status": "ACTIVE",
        "accessConfig": {"authenticationMode": "API"},
    }
    clients["eks"].describe_cluster.return_value = {"cluster": cluster}
    clients["ecr"].describe_repositories.return_value = {
        "repositories": [
            {
                "repositoryUri": selected["image_repository"],
                "imageTagMutability": "IMMUTABLE",
            }
        ]
    }

    class NoSuchEntity(Exception):
        pass

    clients["iam"].exceptions.NoSuchEntityException = NoSuchEntity
    if provider_state == "missing":
        clients["iam"].get_open_id_connect_provider.side_effect = NoSuchEntity()
    else:
        clients["iam"].get_open_id_connect_provider.return_value = {
            "Url": "token.actions.githubusercontent.com",
            "ClientIDList": ["sts.amazonaws.com"],
            "Tags": [
                {
                    "Key": "adp:evaluator-owner",
                    "Value": name if provider_state == "owned" else "another-target",
                }
            ],
        }
    result = setup.discover(session, args, cfg, selected, name)
    assert result == (fixture, "", provider_state != "shared", cluster)
    clients["secretsmanager"].get_secret_value.assert_not_called()
    clients["iam"].create_open_id_connect_provider.assert_not_called()
    clients["s3"].head_bucket.assert_called_once_with(
        Bucket=args.state_bucket, ExpectedBucketOwner=args.account
    )


def test_manual_login_can_select_new_target_without_a_new_binding_file():
    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/eval-cli-uplift.yml").read_text()
    )
    triggers = workflow.get("on", workflow.get(True))
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["target_environment"]["type"] == "environment"
    assert (
        "default" not in inputs["target_environment"]
    )  # Preserve legacy environment callers.
    assert inputs["environment"]["default"] == "dev"
    for name in ("evaluate", "recover"):
        job = workflow["jobs"][name]
        assert "inputs.target_environment || inputs.environment" in job["environment"]
        assert "inputs.environment || 'dev'" in job["env"]["CLI_UPLIFT_EVAL_BINDINGS"]


def test_generated_bindings_satisfy_the_existing_encrypted_recovery_write_contract(
    inputs,
):
    from tests.e2e.cli_uplift.statestore import S3StateStore

    args, bindings, catalog = inputs
    cfg, _, name, _ = setup.validate_inputs(args, bindings, catalog)
    aws = Mock()
    store = S3StateStore(aws, cfg["state_bucket"], kms_key_id=cfg["state_kms_key_id"])
    store.save("adp-e2e-20261008-000000-abcdef", {"status": "starting"})
    write = aws.call.call_args
    assert write.args == ("s3", "put_object")
    assert write.kwargs["ServerSideEncryption"] == "aws:kms"
    assert write.kwargs["SSEKMSKeyId"] == "alias/" + name + "-state"
    assert write.kwargs["ACL"] == "bucket-owner-full-control"


def secret_metadata(name, updated="2026-01-01T00:00:00Z"):
    return {"name": name, "created_at": "2026-01-01T00:00:00Z", "updated_at": updated}


def test_publish_records_each_secret_for_partial_failure_recovery(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(setup, "run", Mock(return_value=""))
    monkeypatch.setattr(
        setup, "gh_api", lambda path: secret_metadata(path.rsplit("/", 1)[-1])
    )
    setup.publish("example/adp", "staging", {}, {}, "role", tmp_path)
    receipt = setup.read_json(tmp_path / "published-secrets.json")
    assert set(receipt) == set(setup.SECRET_NAMES)
    assert all(receipt[key] == secret_metadata(key) for key in setup.SECRET_NAMES)


@pytest.mark.parametrize("changed", [False, True, "missing"])
def test_secret_delete_requires_unmodified_publication_receipt(
    tmp_path, monkeypatch, changed
):
    name = setup.SECRET_NAMES[0]
    if changed != "missing":
        (tmp_path / "published-secrets.json").write_text(
            json.dumps({name: secret_metadata(name)})
        )
    monkeypatch.setattr(
        setup,
        "gh_api",
        Mock(
            return_value={
                "secrets": [
                    secret_metadata(
                        name, "later" if changed is True else "2026-01-01T00:00:00Z"
                    )
                ]
            }
        ),
    )
    if changed:
        with pytest.raises(setup.SetupError, match="publication receipt"):
            setup.owned_secrets("example/adp", "staging", tmp_path)
    else:
        assert setup.owned_secrets("example/adp", "staging", tmp_path) == [name]


def test_missing_secrets_allow_repeated_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "gh_api", Mock(return_value={"secrets": []}))
    assert setup.owned_secrets("example/adp", "staging", tmp_path) == []


@pytest.mark.parametrize(
    "busy_filter", [None, "tag:adp:evaluator-target", "iam-instance-profile.arn"]
)
def test_delete_refuses_instances_by_tag_or_profile_including_stopped(busy_filter):
    session = Mock()
    calls = []

    def pages(*, Filters):
        calls.append(Filters)
        return (
            [{"Reservations": [{"Instances": [{"State": {"Name": "stopped"}}]}]}]
            if Filters[0]["Name"] == busy_filter
            else [{"Reservations": []}]
        )

    session.client.return_value.get_paginator.return_value.paginate.side_effect = pages
    if busy_filter:
        with pytest.raises(setup.SetupError, match="instances remain"):
            setup.check_idle(session, "111122223333", "adp-eval-staging-test")
    else:
        setup.check_idle(session, "111122223333", "adp-eval-staging-test")
        assert len(calls) == 2
    assert all("stopped" in filters[1]["Values"] for filters in calls)


def deletion_resource(inputs):
    args, bindings, catalog = inputs
    _, selected, name, _ = setup.validate_inputs(args, bindings, catalog)
    resource = {
        "address": "aws_iam_role.evaluator[0]",
        "type": "aws_iam_role",
        "change": {
            "actions": ["delete"],
            "before": {
                "name": name + "-oidc",
                "arn": f"arn:aws:iam::{args.account}:role/{name}-oidc",
                "tags_all": {
                    "adp:evaluator-owner": name,
                    "adp:managed-by": "evaluator-setup",
                },
            },
        },
    }
    return resource, args.account, name, selected


@pytest.mark.parametrize(
    "mutation",
    [None, "foreign_name", "foreign_owner", "replace", "bucket", "update", "create"],
)
def test_delete_plan_only_removes_owned_credentials(inputs, mutation):
    resource, account, name, selected = deletion_resource(inputs)
    if mutation == "foreign_name":
        resource["change"]["before"]["name"] = "other"
    elif mutation == "foreign_owner":
        resource["change"]["before"]["tags_all"]["adp:evaluator-owner"] = "other"
    elif mutation == "bucket":
        resource["address"] = "aws_s3_bucket.state"
    elif mutation:
        resource["change"]["actions"] = (
            ["delete", "create"] if mutation == "replace" else [mutation]
        )
    plan = {"resource_changes": [resource]}
    if mutation:
        with pytest.raises(setup.SetupError):
            setup.check_delete_plan(plan, account, name, selected)
    else:
        setup.check_delete_plan(plan, account, name, selected)
        setup.check_delete_plan({"resource_changes": []}, account, name, selected)


@pytest.mark.parametrize("mutation", [None, "label", "rules", "subject", "endpoint"])
def test_delete_observer_requires_matching_endpoint_ownership_and_access(
    inputs, monkeypatch, mutation
):
    _, bindings, catalog = inputs
    args = inputs[0]
    _, selected, name, _ = setup.validate_inputs(args, bindings, catalog)
    objects = setup.observer_objects(selected, name)["items"]
    if mutation == "label":
        objects[0]["metadata"]["labels"].clear()
    if mutation == "rules":
        objects[0]["rules"][0]["verbs"] = ["*"]
    if mutation == "subject":
        objects[1]["subjects"][0]["name"] = "another-group"
    commands = []

    def command(argv):
        commands.append(argv)
        if "config" in argv:
            return json.dumps(
                {
                    "clusters": [
                        {
                            "cluster": {
                                "server": "wrong"
                                if mutation == "endpoint"
                                else "https://cluster.example.com"
                            }
                        }
                    ]
                }
            )
        if "get" in argv:
            return json.dumps(next(o for o in objects if o["kind"] in argv))
        return "yes"

    monkeypatch.setattr(setup, "run", command)
    if mutation:
        with pytest.raises(setup.SetupError):
            setup.delete_observer_command(
                "private-kubeconfig",
                {"endpoint": "https://cluster.example.com"},
                selected,
                name,
            )
    else:
        setup.delete_observer_command(
            "private-kubeconfig",
            {"endpoint": "https://cluster.example.com"},
            selected,
            name,
        )
    assert all("delete" not in c or "can-i" in c for c in commands)


@pytest.mark.parametrize(
    "apply,fail_apply", [(False, False), (True, False), (True, True)]
)
def test_delete_preview_apply_and_interrupted_retry(
    inputs, tmp_path, monkeypatch, apply, fail_apply
):
    args, bindings, catalog = inputs
    cfg, selected, name, _ = setup.validate_inputs(args, bindings, catalog)
    args.apply, args.kubeconfig, args.profile = apply, "private-kubeconfig", None
    target = {"state_bucket": args.state_bucket}
    (tmp_path / "setup-target.json").write_text(json.dumps(target))
    original = {
        "account": args.account,
        "repository": args.repository,
        "target": args.environment,
        "region": cfg["region"],
        "prefix": name,
        "bucket": cfg["state_bucket"],
        "cluster": selected["cluster"],
        "namespace": selected["namespace"],
    }
    (tmp_path / "terraform.tfvars.json").write_text(json.dumps({"config": original}))
    (tmp_path / "observer.json").write_text(
        json.dumps(setup.observer_objects(selected, name))
    )
    remaining = {k: secret_metadata(k) for k in setup.SECRET_NAMES}
    (tmp_path / "published-secrets.json").write_text(json.dumps(remaining))
    session = Mock()
    session.client.return_value.get_caller_identity.return_value = {
        "Account": args.account
    }
    session.client.return_value.describe_cluster.return_value = {
        "cluster": {"arn": selected["cluster_arn"]}
    }
    idle = Mock()
    monkeypatch.setattr(setup, "check_idle", idle)
    monkeypatch.setattr(
        setup, "delete_observer_command", Mock(return_value=["kubectl"])
    )
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    operations = []

    def api(path, *, method="GET"):
        if method == "DELETE":
            key = path.rsplit("/", 1)[-1]
            operations.append(["secret-delete", key])
            del remaining[key]
            return {}
        return {"secrets": list(remaining.values())}

    def command(argv):
        operations.append(argv)
        if "show" in argv:
            resource, *_ = deletion_resource(inputs)
            return json.dumps({"resource_changes": [resource]})
        if "apply" in argv and fail_apply:
            raise setup.SetupError("interrupted apply")
        return ""

    monkeypatch.setattr(setup, "gh_api", api)
    monkeypatch.setattr(setup, "run", command)
    if fail_apply:
        with pytest.raises(setup.SetupError, match="interrupted"):
            setup.delete_credentials(
                args, session, cfg, selected, name, tmp_path, target
            )
        assert setup.SECRET_NAMES[0] not in remaining
        assert len(remaining) == 2
        assert not any(c[0] == "kubectl" for c in operations)
        fail_apply = False
        operations.clear()
    assert (
        setup.delete_credentials(args, session, cfg, selected, name, tmp_path, target)
        == 0
    )
    if not apply:
        assert len(remaining) == 3
        assert all(
            "apply" not in c and c[0] not in {"kubectl", "secret-delete"}
            for c in operations
        )
        assert any("-lock=false" in c for c in operations)
    else:
        assert remaining == {}
        tf_apply = next(i for i, c in enumerate(operations) if "apply" in c)
        assert "delete.tfplan" in operations[tf_apply]
        assert all(i > tf_apply for i, c in enumerate(operations) if c[0] == "kubectl")
        assert operations[-1] == ["secret-delete", setup.SECRET_NAMES[-1]]
        operations.clear()
        assert (
            setup.delete_credentials(
                args, session, cfg, selected, name, tmp_path, target
            )
            == 0
        )
        assert not any(c[0] == "secret-delete" for c in operations)


@pytest.mark.parametrize(
    "reason",
    [
        "missing_directory",
        "wrong_account",
        "wrong_saved_target",
        "missing_state",
        "unsafe_plan",
    ],
)
def test_delete_failures_precede_every_remote_mutation(
    inputs, tmp_path, monkeypatch, reason
):
    args, bindings, catalog = inputs
    cfg, selected, name, _ = setup.validate_inputs(args, bindings, catalog)
    args.apply, args.kubeconfig, args.profile = True, "private-kubeconfig", None
    target = {"state_bucket": args.state_bucket}
    if reason != "missing_directory":
        (tmp_path / "setup-target.json").write_text(json.dumps(target))
    original = {
        "account": args.account,
        "repository": args.repository,
        "target": args.environment,
        "region": cfg["region"],
        "prefix": name,
        "bucket": cfg["state_bucket"],
        "cluster": selected["cluster"],
        "namespace": selected["namespace"],
    }
    if reason == "wrong_saved_target":
        original["cluster"] = "other"
    (tmp_path / "terraform.tfvars.json").write_text(json.dumps({"config": original}))
    (tmp_path / "observer.json").write_text(
        json.dumps(setup.observer_objects(selected, name))
    )
    session = Mock()
    session.client.return_value.get_caller_identity.return_value = {
        "Account": "999900001111" if reason == "wrong_account" else args.account
    }
    session.client.return_value.describe_cluster.return_value = {
        "cluster": {"arn": selected["cluster_arn"]}
    }
    if reason == "missing_state":
        session.client.return_value.head_object.side_effect = setup.SetupError(
            "missing backend state"
        )
    monkeypatch.setattr(setup, "check_idle", Mock())
    api = Mock(return_value={"secrets": []})
    monkeypatch.setattr(setup, "gh_api", api)
    monkeypatch.setattr(
        setup, "delete_observer_command", Mock(return_value=["kubectl"])
    )
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    commands = []

    def command(argv):
        commands.append(argv)
        if "show" in argv:
            return json.dumps(
                {
                    "resource_changes": [
                        {
                            "address": "aws_kms_key.state",
                            "change": {"actions": ["delete"]},
                        }
                    ]
                }
            )
        return ""

    monkeypatch.setattr(setup, "run", command)
    with pytest.raises(setup.SetupError):
        setup.delete_credentials(args, session, cfg, selected, name, tmp_path, target)
    assert all("apply" not in c and "delete" not in c for c in commands)
    assert all(c.kwargs.get("method", "GET") == "GET" for c in api.call_args_list)
    if reason == "wrong_account":
        session.client.assert_called_once_with("sts")
