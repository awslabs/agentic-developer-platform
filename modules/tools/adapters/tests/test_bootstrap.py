"""Saved-plan bootstrap refuses target drift and destructive foundation changes."""

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("adapter_foundation_bootstrap", ROOT / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.fixture
def config():
    return {
        "backend_bucket": "example-state-bucket",
        "variables": {
            "aws_account_id": "123456789012",
            "aws_region": "us-east-1",
            "environment": "dev",
            "state_bucket": "example-state-bucket",
            "cluster_name": "adp-dev-eks-cluster",
            "namespace": "adp-tools-adapters",
            "oidc_provider_arn": "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE",
            "runtime_role_name": "adp-dev-tool-adapters",
            "runtime_verifier_role_arn": "arn:aws:iam::123456789012:role/path/ADP-Agent-dev-platform",
            "runtime_verifier_group": "adp:runtime-verifier-dev",
        },
    }


@pytest.fixture
def driver(tmp_path, config, monkeypatch):
    root = tmp_path / "source"
    for name, content in bootstrap.sources().items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    instance = bootstrap.Bootstrap(
        config,
        tmp_path / "plan",
        source_root=root,
        env={
            "AWS_PROFILE": "customer",
            "KUBECONFIG": "/unrelated",
            "KUBE_CONFIG_PATH": "/unrelated",
            "KUBE_CONFIG_PATHS": "/unrelated",
            "TF_VAR_aws_account_id": "999999999999",
            "TF_CLI_ARGS_plan": "-destroy",
            "TF_WORKSPACE": "another-customer",
            "TF_CLI_CONFIG_FILE": "/unrelated",
            "TF_DATA_DIR": "/unrelated",
        },
    )
    instance.calls = []
    instance.account = "123456789012"
    instance.versioned = True
    instance.private = True
    instance.invalid_outputs = False
    instance.missing_output = None
    instance.plan_value = {
        "format_version": "1.2",
        "complete": True,
        "errored": False,
        "variables": {name: {"value": value} for name, value in instance.config["variables"].items()},
        "resource_changes": [
            {"address": "module.publication.aws_ecr_repository.adapter", "change": {"actions": ["create"]}}
        ],
    }

    def run(args, **kwargs):
        instance.calls.append(args)
        if args[:3] == ["aws", "sts", "get-caller-identity"]:
            return bootstrap.encoded({"Account": instance.account})
        if args[:2] == ["aws", "s3api"]:
            assert (
                "--expected-bucket-owner" in args and args[args.index("--expected-bucket-owner") + 1] == "123456789012"
            )
            if args[2] == "get-bucket-versioning":
                return bootstrap.encoded({"Status": "Enabled" if instance.versioned else "Suspended"})
            if args[2] == "get-public-access-block":
                return bootstrap.encoded(
                    {
                        "PublicAccessBlockConfiguration": {
                            key: instance.private
                            for key in (
                                "BlockPublicAcls",
                                "IgnorePublicAcls",
                                "BlockPublicPolicy",
                                "RestrictPublicBuckets",
                            )
                        }
                    }
                )
            assert args[2] == "head-bucket"
            return b""
        if args == ["terraform", "version", "-json"]:
            return b'{"terraform_version":"1.14.6"}'
        assert args[0] == "terraform"
        action = args[2]
        if action == "plan":
            assert "-input=false" in args and "-lock-timeout=120s" in args
            (instance.work / "foundation.tfplan").write_bytes(b"saved-binary-plan")
        elif action == "show":
            return bootstrap.encoded(instance.plan_value)
        elif action == "apply":
            assert args[-1] == str(instance.work / "foundation.tfplan")
            assert not any(arg.startswith("-var") for arg in args)
        elif action == "output":
            values = {
                "repository_name": "adp-tools-dev-adapters",
                "build_project_name": "adp-tools-dev-adapters-build",
                "foundation_check_project_name": "adp-tools-dev-adapters-foundation-check",
                "foundation_check_role_arn": "arn:aws:iam::123456789012:role/adp-tools-dev-adapters-foundation-check",
                "foundation_check_source_prefix": "codebuild/src/adp-tools-dev-adapters-foundation-check/",
                "foundation_check_artifact_prefix": "codebuild/artifacts/adp-tools-dev-adapters-foundation-check/",
                "namespace": "adp-tools-adapters",
                "runtime_role_arn": "arn:aws:iam::123456789012:role/adp-dev-tool-adapters",
                "runtime_verifier_role_arn": config["variables"]["runtime_verifier_role_arn"],
                "runtime_verifier_group": "adp:runtime-verifier-dev",
                "verified_cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/adp-dev-eks-cluster",
            }
            if instance.invalid_outputs:
                values["namespace"] = "wrong-namespace"
            if instance.missing_output:
                values.pop(instance.missing_output)
            return bootstrap.encoded({key: {"value": value} for key, value in values.items()})
        else:
            assert action in {"init", "validate"}
        return b""

    monkeypatch.setattr(instance, "run", run)
    return instance


def test_plan_and_apply_bind_sources_target_backend_and_binary(driver):
    assert driver.plan()["phase"] == "planned"
    assert driver.calls[0][:3] == ["aws", "sts", "get-caller-identity"]
    assert driver.env["AWS_PROFILE"] == "customer"
    assert (
        not {
            "KUBECONFIG",
            "KUBE_CONFIG_PATH",
            "KUBE_CONFIG_PATHS",
            "TF_VAR_aws_account_id",
            "TF_CLI_ARGS_plan",
            "TF_WORKSPACE",
        }
        & driver.env.keys()
    )
    backend = bootstrap.read_json(driver.work / "backend.json")
    assert backend == {
        "bucket": "example-state-bucket",
        "key": "tools/adapters/dev.tfstate",
        "region": "us-east-1",
        "encrypt": True,
        "use_lockfile": True,
        "allowed_account_ids": ["123456789012"],
    }
    manifest = bootstrap.read_json(driver.work / "manifest.json")
    assert manifest["source_sha256"] == bootstrap.source_digest(bootstrap.sources(driver.source_root))
    for name in ("manifest.json", "backend.json", "variables.tfvars.json", "plan.json", "commands.log"):
        assert (driver.work / name).stat().st_mode & 0o077 == 0
    driver.calls.clear()
    result = driver.apply()
    assert driver.calls[0][:3] == ["aws", "sts", "get-caller-identity"]
    assert result["phase"] == "foundation_applied" and result["runtime_verified"] is False
    assert (driver.work / "outputs.json").is_file()


def existing_edge_plan(driver, monkeypatch, *, namespace="adp-tools-adapters"):
    """Model Terraform's refreshed existing ingress and its boolean count."""
    original_run = driver.run

    def run(args, **kwargs):
        if args[0] == "terraform" and args[2] == "plan":
            variables = bootstrap.read_json(driver.work / "variables.tfvars.json")
            driver.plan_value["variables"] = {key: {"value": value} for key, value in variables.items()}
            existing = {"metadata": [{"name": "tool-adapters-private", "namespace": namespace}]}
            driver.plan_value["resource_changes"] = [
                {
                    "address": "module.foundation.kubernetes_ingress_v1.adapter_private[0]",
                    "mode": "managed",
                    "change": {
                        "actions": ["no-op"] if variables["private_edge_enabled"] else ["delete"],
                        "before": existing,
                        "after": existing if variables["private_edge_enabled"] else None,
                    },
                }
            ]
        return original_run(args, **kwargs)

    monkeypatch.setattr(driver, "run", run)


def test_existing_edge_survives_omitted_flag_and_fresh_process_apply(driver, config, monkeypatch):
    existing_edge_plan(driver, monkeypatch)
    assert driver.plan()["changes"] == []
    assert sum(args[0] == "terraform" and args[2] == "plan" for args in driver.calls) == 2
    assert bootstrap.read_json(driver.work / "variables.tfvars.json")["private_edge_enabled"] is True
    assert "private_edge_enabled" not in config["variables"]
    resumed = bootstrap.Bootstrap(config, driver.work, source_root=driver.source_root, env=driver.env)
    monkeypatch.setattr(resumed, "run", driver.run)
    assert resumed.apply()["phase"] == "foundation_applied"
    assert resumed.config["variables"]["private_edge_enabled"] is True


def test_new_foundation_keeps_edge_disabled_without_an_opt_in(driver):
    driver.plan()
    assert bootstrap.read_json(driver.work / "variables.tfvars.json")["private_edge_enabled"] is False
    assert sum(args[0] == "terraform" and args[2] == "plan" for args in driver.calls) == 1


def test_explicit_disable_still_cannot_delete_an_existing_edge(driver, config, monkeypatch):
    existing_edge_plan(driver, monkeypatch)
    config["variables"]["private_edge_enabled"] = False
    explicit = bootstrap.Bootstrap(config, driver.work, source_root=driver.source_root, env=driver.env)
    monkeypatch.setattr(explicit, "run", driver.run)
    with pytest.raises(bootstrap.BootstrapError, match="destructive_plan_refused"):
        explicit.plan()
    assert not (driver.work / "manifest.json").exists()
    assert not any(args[0] == "terraform" and args[2] == "apply" for args in driver.calls)


def test_retained_edge_must_belong_to_selected_namespace(driver, monkeypatch):
    existing_edge_plan(driver, monkeypatch, namespace="another-tenant")
    with pytest.raises(bootstrap.BootstrapError, match="private_edge_state_target_mismatch"):
        driver.plan()
    assert not (driver.work / "manifest.json").exists()


@pytest.mark.parametrize("flag", ["true", "false", 1, 0, None])
def test_private_edge_flag_must_be_boolean(config, flag):
    config["variables"]["private_edge_enabled"] = flag
    with pytest.raises(bootstrap.BootstrapError, match="private_edge_activation_must_be_boolean"):
        bootstrap.configuration(config)


def test_private_edge_can_be_explicitly_enabled(config):
    config["variables"]["private_edge_enabled"] = True
    assert bootstrap.configuration(config)["variables"]["private_edge_enabled"] is True


@pytest.mark.parametrize("case", ["account", "versioning", "public"])
def test_prerequisite_refusal_precedes_terraform(driver, case):
    if case == "account":
        driver.account = "999999999999"
    elif case == "versioning":
        driver.versioned = False
    else:
        driver.private = False
    with pytest.raises(bootstrap.BootstrapError):
        driver.plan()
    assert not any(args[0] == "terraform" for args in driver.calls)


@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"], ["create", "delete"], ["forget"]])
def test_destructive_changes_never_get_an_apply_manifest(driver, actions):
    driver.plan_value["resource_changes"][0]["change"]["actions"] = actions
    with pytest.raises(bootstrap.BootstrapError, match="destructive_plan_refused"):
        driver.plan()
    assert not (driver.work / "manifest.json").exists()
    assert not any("apply" in args for args in driver.calls)


@pytest.mark.parametrize("case", ["binary", "source", "snapshot", "backend", "variables", "config"])
def test_changed_plan_context_refuses_before_any_provider_call(driver, case):
    driver.plan()
    if case == "binary":
        (driver.work / "foundation.tfplan").write_bytes(b"different-plan")
    elif case == "source":
        (driver.source_root / "deployment-infra/main.tf").write_text("# changed source")
    elif case == "snapshot":
        (driver.work / "source/deployment-infra/main.tf").write_text("# changed snapshot")
    elif case in {"backend", "variables"}:
        name = "backend.json" if case == "backend" else "variables.tfvars.json"
        (driver.work / name).write_text("{}")
    else:
        driver.config["variables"]["namespace"] = "another-namespace"
    driver.calls.clear()
    with pytest.raises(bootstrap.BootstrapError):
        driver.apply()
    assert driver.calls == []


def test_apply_rechecks_saved_plan_semantics(driver):
    driver.plan()
    driver.plan_value["variables"]["aws_account_id"]["value"] = "999999999999"
    with pytest.raises(bootstrap.BootstrapError, match="saved_plan_target_mismatch"):
        driver.apply()
    assert not any("apply" in args for args in driver.calls)


def test_incorrect_outputs_do_not_report_foundation_success(driver):
    driver.plan()
    driver.invalid_outputs = True
    with pytest.raises(bootstrap.BootstrapError, match="foundation_outputs_mismatch"):
        driver.apply()
    assert not (driver.work / "outputs.json").exists()


@pytest.mark.parametrize(
    "output",
    [
        "foundation_check_project_name",
        "foundation_check_role_arn",
        "foundation_check_source_prefix",
        "foundation_check_artifact_prefix",
    ],
)
def test_missing_qualifier_cannot_report_bootstrap_success(driver, output):
    driver.plan()
    driver.missing_output = output
    with pytest.raises(bootstrap.BootstrapError, match="foundation_outputs_mismatch"):
        driver.apply()
    assert not (driver.work / "outputs.json").exists()


def test_account_change_between_plan_and_apply_stops_before_init(driver):
    driver.plan()
    driver.account = "999999999999"
    driver.calls.clear()
    with pytest.raises(bootstrap.BootstrapError, match="account_mismatch"):
        driver.apply()
    assert not any(args[0] == "terraform" for args in driver.calls)


def test_terraform_json_override_cannot_be_silently_omitted(driver):
    (driver.source_root / "deployment-infra/override.tf.json").write_text("{}")
    with pytest.raises(bootstrap.BootstrapError, match="terraform_json_override_requires_review"):
        driver.plan()
    assert not any(args[0] == "terraform" for args in driver.calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("aws_account_id", "999999999999"),
        ("runtime_verifier_group", "system:masters"),
        ("namespace", "kube-system"),
        ("runtime_verifier_role_arn", "arn:aws:iam::123456789012:role/adp-dev-tool-adapters"),
        ("source_kms_key_arn", "arn:aws:kms:us-west-2:123456789012:key/different"),
    ],
)
def test_cross_account_or_unsafe_inputs_refused(config, field, value):
    config["variables"][field] = value
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.configuration(config)


def test_distinct_environment_backend_and_no_credential_injection(config):
    production = copy.deepcopy(config)
    production["variables"].update(environment="prod", runtime_verifier_group="adp:runtime-verifier-prod")
    assert bootstrap.configuration(config)["backend"]["key"] != bootstrap.configuration(production)["backend"]["key"]
    config["backend_access_key"] = "credential"
    with pytest.raises(bootstrap.BootstrapError, match="config_fields_invalid"):
        bootstrap.configuration(config)


def test_adapter_identity_grants_require_explicit_boolean_activation(config):
    assert bootstrap.configuration(config)["variables"]["atlassian_mcp_identity_enabled"] is False
    config["variables"]["atlassian_mcp_identity_enabled"] = True
    assert bootstrap.configuration(config)["variables"]["atlassian_mcp_identity_enabled"] is True
    for value in ("true", 1, None):
        config["variables"]["atlassian_mcp_identity_enabled"] = value
        with pytest.raises(bootstrap.BootstrapError):
            bootstrap.configuration(config)


@pytest.mark.parametrize(
    "external_key", [None, "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"]
)
def test_observation_optional_key_survives_saved_plan_and_fresh_apply(driver, config, monkeypatch, external_key):
    config["variables"]["observation_runtime"] = {"core_role_arn": "arn:aws:iam::123456789012:role/core"}
    if external_key is not None:
        config["variables"]["observation_runtime"]["assertion_key_arn"] = external_key
    original = copy.deepcopy(config)
    driver.config = bootstrap.configuration(config)
    # Independent representation of Terraform's optional object normalization.
    values = copy.deepcopy(driver.config["variables"])
    values["observation_runtime"] = {
        "core_role_arn": "arn:aws:iam::123456789012:role/core",
        "assertion_key_arn": external_key,
        "release": None,
    }
    driver.plan_value["variables"] = {name: {"value": value} for name, value in values.items()}
    driver.plan()
    resumed = bootstrap.Bootstrap(config, driver.work, source_root=driver.source_root, env=driver.env)
    monkeypatch.setattr(resumed, "run", driver.run)
    assert resumed.apply()["phase"] == "foundation_applied"
    assert config == original
