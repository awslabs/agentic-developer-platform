"""Installer refuses foreign state, unrelated IAM changes and partial plans."""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from adapters import installer  # noqa: E402


@pytest.fixture
def config():
    return {
        "foundation": {
            "backend_bucket": "adp-terraform-state-123456789012",
            "variables": {
                "aws_account_id": "123456789012",
                "aws_region": "us-east-1",
                "environment": "dev",
                "state_bucket": "adp-terraform-state-123456789012",
                "cluster_name": "adp-dev-eks-cluster",
                "namespace": "adp-tools-adapters",
                "runtime_role_name": "adp-dev-tool-adapters",
                "oidc_provider_arn": (
                    "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
                ),
                "runtime_verifier_role_arn": "arn:aws:iam::123456789012:role/ADP-Agent-dev-platform",
                "runtime_verifier_group": "adp:runtime-verifier-dev",
            },
        },
        "automation": {
            "backend": {
                "bucket": "adp-terraform-state-123456789012",
                "key": "dev/trusted-automation/terraform.tfstate",
                "region": "us-east-1",
                "encrypt": True,
                "dynamodb_table": "retained-lock-table",
            },
            "variables": {
                "name_prefix": "adp-dev",
                "environment": "dev",
                "aws_region": "us-east-1",
                "cluster_name": "adp-dev-eks-cluster",
                "repository": "example/platform",
                "enable_tools_deployment": True,
            },
        },
    }


def plans(config):
    variables = installer.configuration(config)["automation"]["variables"]
    off_policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Deny", "NotAction": ["ecr:DescribeImages"], "Resource": "*"}],
        }
    )
    on_policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Deny", "NotAction": ["ecr:DescribeImages", "ecr:PutImage"], "Resource": "*"}],
        }
    )
    old = {"role": "adp-dev-tools-trusted-deployment", "name": "tools-platform-release", "policy": off_policy}
    new = {**old, "policy": on_policy}
    address = "aws_iam_role_policy.tools_deployment[0]"
    on = {
        "complete": True,
        "variables": {k: {"value": v} for k, v in variables.items()},
        "resource_changes": [{"address": address, "change": {"actions": ["update"], "before": old, "after": new}}],
    }
    off = copy.deepcopy(on)
    off["variables"]["enable_tools_adapters_publication"]["value"] = False
    off["resource_changes"][0]["change"] = copy.deepcopy({"actions": ["no-op"], "before": old, "after": old})
    for other in installer.EXISTING_POLICIES - {address}:
        on["resource_changes"].append({"address": other, "change": {"actions": ["no-op"], "before": old, "after": old}})
        off["resource_changes"].append(copy.deepcopy(on["resource_changes"][-1]))
    on["resource_changes"].extend(
        [
            {
                "address": installer.PUBLICATION_POLICY,
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"name": "adp-dev-tools-adapters-publication", "policy": on_policy},
                },
            },
            {
                "address": installer.PUBLICATION_ATTACHMENT,
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"role": "adp-dev-tools-trusted-deployment"},
                },
            },
        ]
    )
    on["resource_changes"].extend(
        [
            {
                "address": installer.FOUNDATION_POLICY,
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"name": "adp-dev-tools-adapters-foundation-check", "policy": on_policy},
                },
            },
            {
                "address": installer.FOUNDATION_ATTACHMENT,
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"role": "adp-dev-tools-trusted-deployment"},
                },
            },
        ]
    )
    return on, off


def test_retains_all_existing_automation_inputs_and_lock(config):
    config["automation"]["variables"]["tools_gateway_arn"] = "retained-reviewed-gateway"
    before = copy.deepcopy(config)
    result = installer.configuration(config)
    assert result["automation"]["variables"] == {
        **before["automation"]["variables"],
        "enable_tools_adapters_publication": True,
    }
    assert result["automation"]["backend"]["dynamodb_table"] == "retained-lock-table"
    assert result["automation"]["backend"]["allowed_account_ids"] == ["123456789012"]
    assert config == before


@pytest.mark.parametrize("flag", [True, False])
def test_installer_preserves_explicit_private_edge_input(config, flag, tmp_path):
    config["foundation"]["variables"]["private_edge_enabled"] = flag
    instance = installer.Installer(config, tmp_path / "installer")
    assert instance.foundation.config["variables"]["private_edge_enabled"] is flag
    assert instance.foundation.private_edge_explicit is True
    assert instance.config["foundation"]["variables"]["private_edge_enabled"] is flag


def test_installer_preserves_omission_for_existing_state_resolution(config, tmp_path):
    instance = installer.Installer(config, tmp_path / "installer")
    assert "private_edge_enabled" not in instance.config["foundation"]["variables"]
    assert instance.foundation.private_edge_explicit is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("bucket", "another-account"),
        ("key", "tools/adapters/dev.tfstate"),
        ("key", "../foreign.tfstate"),
        ("region", "us-west-2"),
        ("encrypt", False),
        ("dynamodb_table", ""),
    ],
)
def test_backend_mismatch_or_missing_lock_refused(config, field, value):
    config["automation"]["backend"][field] = value
    with pytest.raises(installer.BootstrapError):
        installer.configuration(config)


@pytest.mark.parametrize(
    "field,value", [("environment", "prod"), ("cluster_name", "other-cluster"), ("enable_tools_deployment", False)]
)
def test_wrong_or_unprovisioned_publisher_refused(config, field, value):
    config["automation"]["variables"][field] = value
    with pytest.raises(installer.BootstrapError):
        installer.configuration(config)


def test_only_flag_delta_can_change_existing_authority(config):
    on, off = plans(config)
    assert len(installer.check_publication_plan(on, off, installer.configuration(config))) == 5
    on["resource_changes"][0]["change"]["before"]["policy"] = (
        '{"Statement":[{"Effect":"Allow","Action":"*","Resource":"*"}]}'
    )
    with pytest.raises(installer.BootstrapError, match="existing_authority_drift"):
        installer.check_publication_plan(on, off, installer.configuration(config))


@pytest.mark.parametrize(
    "change",
    [
        {"address": "aws_iam_role.tools_deployment[0]", "change": {"actions": ["update"], "after": {}}},
        {"address": installer.PUBLICATION_POLICY, "change": {"actions": ["delete", "create"], "after": {}}},
        {
            "address": installer.PUBLICATION_POLICY,
            "change": {"actions": ["create"], "after": {}, "importing": {"id": "foreign"}},
        },
        {"address": installer.PUBLICATION_POLICY, "change": {"actions": ["create"], "after": {"name": "other-policy"}}},
        {
            "address": installer.PUBLICATION_ATTACHMENT,
            "change": {"actions": ["create"], "after": {"role": "other-role"}},
        },
        {
            "address": installer.FOUNDATION_POLICY,
            "change": {"actions": ["create"], "after": {"name": "other-check-policy"}},
        },
        {
            "address": installer.FOUNDATION_ATTACHMENT,
            "change": {"actions": ["create"], "after": {"role": "other-role"}},
        },
        {"address": installer.FOUNDATION_POLICY, "change": {"actions": ["delete", "create"], "after": {}}},
    ],
)
def test_foreign_destructive_or_unrelated_change_refused(config, change):
    on, off = plans(config)
    on["resource_changes"].append(change)
    with pytest.raises(installer.BootstrapError):
        installer.check_publication_plan(on, off, installer.configuration(config))


def test_target_input_drift_refused(config):
    on, off = plans(config)
    on["variables"]["cluster_name"]["value"] = "other-cluster"
    with pytest.raises(installer.BootstrapError, match="inputs_changed"):
        installer.check_publication_plan(on, off, installer.configuration(config))


def test_scoped_plan_allowed_but_deferred_changes_refused(config):
    on, off = plans(config)
    on["complete"] = off["complete"] = False
    assert installer.check_publication_plan(on, off, installer.configuration(config))
    on["deferred_changes"] = [{"reason": "resource_config_unknown"}]
    with pytest.raises(installer.BootstrapError, match="plan_incomplete"):
        installer.check_publication_plan(on, off, installer.configuration(config))


def test_both_plans_pass_before_any_apply(config, tmp_path):
    instance = installer.Installer(config, tmp_path / "install")
    instance.publisher = Mock()
    instance.foundation = Mock()
    instance.foundation.plan.side_effect = installer.BootstrapError("foundation_precondition_failed")
    with pytest.raises(installer.BootstrapError):
        instance.plan()
    instance.publisher.apply.assert_not_called()
    instance.foundation.apply.assert_not_called()


def test_publisher_plan_tampering_prevents_foundation_apply(config, tmp_path):
    instance = installer.Installer(config, tmp_path / "install")
    instance.work.mkdir()
    (instance.work / "config.json").write_bytes(installer.encoded(instance.config))
    instance.publisher = Mock()
    instance.foundation = Mock()
    instance.publisher.verify_saved.side_effect = installer.BootstrapError("publisher_plan_changed")
    with pytest.raises(installer.BootstrapError):
        instance.apply()
    instance.foundation.apply.assert_not_called()


def test_foundation_failure_never_enables_publisher(config, tmp_path):
    instance = installer.Installer(config, tmp_path / "install")
    instance.work.mkdir()
    (instance.work / "config.json").write_bytes(installer.encoded(instance.config))
    instance.publisher = Mock()
    instance.foundation = Mock()
    instance.foundation.apply.side_effect = installer.BootstrapError("foundation_outputs_mismatch")
    with pytest.raises(installer.BootstrapError):
        instance.apply()
    instance.publisher.apply.assert_not_called()


def test_snapshot_contains_current_terraform_dependency_closure():
    files = installer.sources()
    assert all(not name.endswith(".tfvars") for name in files)
    for path in installer.SOURCE_PATHS:
        assert any(name.startswith(path) for name in files)


@pytest.fixture
def publisher(config, tmp_path, monkeypatch):
    instance = installer.Publisher(installer.configuration(config), tmp_path / "publisher")
    instance.state_owned = True
    instance.calls = []
    on, off = plans(config)
    monkeypatch.setattr(instance, "target", lambda: None)
    monkeypatch.setattr(instance, "initialize", lambda: None)
    monkeypatch.setattr(installer, "sources", lambda *args: {installer.AUTOMATION + "/main.tf": b"# reviewed source\n"})

    def tf(*args):
        instance.calls.append(args)
        if args == ("state", "pull"):
            resources = (
                [
                    {
                        "type": "aws_iam_role",
                        "name": "tools_deployment",
                        "instances": [
                            {
                                "index_key": 0,
                                "attributes": {
                                    "arn": "arn:aws:iam::123456789012:role/adp-dev-tools-trusted-deployment"
                                },
                            }
                        ],
                    }
                ]
                if instance.state_owned
                else []
            )
            return installer.encoded({"resources": resources})
        if args[0] == "plan":
            assert not any(arg.startswith("-var=") for arg in args)
            path = Path(next(arg.removeprefix("-var-file=") for arg in args if arg.startswith("-var-file=")))
            assert type(installer.read_json(path)["enable_tools_adapters_publication"]) is bool
            Path(next(arg.removeprefix("-out=") for arg in args if arg.startswith("-out="))).write_bytes(
                b"immutable saved plan"
            )
            return b""
        assert args[:2] == ("show", "-json")
        return installer.encoded(off if Path(args[-1]).name == "disabled.tfplan" else on)

    monkeypatch.setattr(instance, "tf", tf)
    return instance


def test_empty_or_wrong_backend_never_reaches_plan(publisher):
    publisher.state_owned = False
    with pytest.raises(installer.BootstrapError, match="does_not_own_publisher"):
        publisher.plan()
    assert publisher.calls == [("state", "pull")]


def test_publisher_saves_typed_flag_plans_and_rejects_tampering(publisher):
    assert publisher.plan()["phase"] == "publisher_planned"
    publisher.verify_saved()
    (publisher.work / "enabled.tfplan").write_bytes(b"changed")
    with pytest.raises(installer.BootstrapError, match="publisher_plan_changed"):
        publisher.verify_saved()


def test_added_snapshot_override_is_refused(publisher):
    publisher.plan()
    (publisher.work / "source" / installer.AUTOMATION / "override.tf").write_text("# unexpected provider override")
    with pytest.raises(installer.BootstrapError, match="publisher_snapshot_override"):
        publisher.verify_saved()


def test_missing_publication_resources_is_not_a_noop_success(config):
    on, off = plans(config)
    on["resource_changes"] = []
    with pytest.raises(installer.BootstrapError, match="publisher_plan_resources_missing"):
        installer.check_publication_plan(on, off, installer.configuration(config))


def test_checkpoint_hash_binds_private_installer_inputs(tmp_path, monkeypatch):
    path = installer.REPO / "platform/scripts/deploy-checkpoints.py"
    spec = importlib.util.spec_from_file_location("adapter_install_checkpoints", path)
    checkpoints = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checkpoints)
    monkeypatch.setattr(checkpoints.subprocess, "check_output", lambda *args: b"")
    config = tmp_path / "private.json"
    config.write_text('{"before":true}')
    monkeypatch.setenv("ADP_TOOLS_ADAPTERS_CONFIG", str(config))
    before = checkpoints.fingerprint(tmp_path, "dev")
    config.write_text('{"after":true}')
    assert checkpoints.fingerprint(tmp_path, "dev") != before
