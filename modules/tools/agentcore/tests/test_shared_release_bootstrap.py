import runpy
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[4]
validate = runpy.run_path(str(ROOT / "modules/tools/agentcore/infra/validate_private_config.py"))["validate"]


def test_protected_bindings_reuse_exact_state_and_target(tmp_path):
    backend = tmp_path / "backend.hcl"
    tfvars = tmp_path / "private.tfvars"
    backend.write_text('bucket = "example-state"\nkey = "approved/tools/agentcore/current.tfstate"\nregion = "us-east-1"\nencrypt = true\nuse_lockfile = true\n')
    tfvars.write_text('enabled = false\naws_account_id = "123456789012"\naws_region = "us-east-1"\n')
    settings = dict(bucket="example-state", state_key="approved/tools/agentcore/current.tfstate",
                    account="123456789012", region="us-east-1")
    validate(backend, tfvars, **settings)
    for invalid in ({**settings, "state_key": "approved/tools/agentcore/other.tfstate"},
                    {**settings, "bucket": "different-state"},
                    {**settings, "account": "999999999999"},
                    {**settings, "state_key": "approved/tools/agentcore/../../other.tfstate"}):
        with pytest.raises(ValueError):
            validate(backend, tfvars, **invalid)
    backend.write_text(backend.read_text() + 'key = "approved/tools/agentcore/other.tfstate"\n')
    with pytest.raises(ValueError, match="duplicate"):
        validate(backend, tfvars, **settings)


def test_protected_cleanup_bindings_are_exact_and_not_optional(tmp_path):
    backend = tmp_path / "backend.hcl"
    tfvars = tmp_path / "private.tfvars"
    backend.write_text('bucket = "example-state"\nkey = "approved/tools/agentcore/current.tfstate"\nregion = "us-east-1"\nuse_lockfile = true\n')
    base = 'aws_account_id = "123456789012"\naws_region = "us-east-1"\n'
    key = 'tools_reconciler_key_arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"\n'
    role = 'tools_reconciler_role_arn = "arn:aws:iam::123456789012:role/adp-dev-role-tools-reconciler"\n'
    settings = dict(bucket="example-state", state_key="approved/tools/agentcore/current.tfstate",
                    account="123456789012", region="us-east-1", environment="dev")
    tfvars.write_text(base + key + role)
    validate(backend, tfvars, **settings)
    for invalid in (base, base + key, base + role, base + key + role + key,
                    base + key.replace("123456789012", "999999999999") + role,
                    base + key.replace("us-east-1", "us-west-2") + role,
                    base + key.replace("key/", "alias/") + role,
                    base + key + role.replace("adp-dev-", "adp-prod-")):
        tfvars.write_text(invalid)
        with pytest.raises(ValueError):
            validate(backend, tfvars, **settings)
    tfvars.write_text(base + key + role)
    with pytest.raises(ValueError):
        validate(backend, tfvars, **{**settings, "environment": "staging"})


def test_workflow_requires_separate_protected_role_and_private_config():
    workflow = yaml.load((ROOT / ".github/workflows/shared-agentcore-tools-deploy.yml").read_text(),
                         Loader=yaml.BaseLoader)
    assert "push" not in workflow["on"]
    assert "backend_config" not in workflow["on"]["workflow_dispatch"]["inputs"]
    assert "tfvars" not in workflow["on"]["workflow_dispatch"]["inputs"]
    assert workflow["jobs"]["tools"]["environment"] == "adp-shared-tools-deploy-${{ inputs.target_environment }}"
    steps = workflow["jobs"]["tools"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Verify reviewed source before reading protected bindings") < names.index("Materialize protected existing state and reviewed variables")
    assert any(step.get("with", {}).get("role_arn") == "${{ vars.ADP_SHARED_TOOLS_DEPLOY_ROLE_ARN }}" for step in steps)
    assert any("ADP_SHARED_TOOLS_BACKEND_HCL" in str(step) and "ADP_SHARED_TOOLS_TFVARS_HCL" in str(step)
               for step in steps)
    materialize = next(step for step in steps if step.get("name") == "Materialize protected existing state and reviewed variables")
    assert materialize["env"]["TARGET_ENVIRONMENT"] == "${{ inputs.target_environment }}"
    assert "validate_private_config.py" in materialize["run"]
    assert "ADP_TOOLS_ENVIRONMENT" not in str(workflow)
    assert "ssm:GetParameter" not in (ROOT / "platform/automation-infra/shared-tools-deployment.tf").read_text()
    assert workflow["on"]["workflow_dispatch"]["inputs"]["mode"]["default"] == "plan"


def test_durable_build_and_browser_authority_are_separate_from_platform_role():
    bootstrap = (ROOT / "modules/tools/platform/bootstrap/shared-tools-build.tf").read_text()
    authority = (ROOT / "platform/automation-infra/shared-tools-deployment.tf").read_text()
    browser = (ROOT / "platform/infra/shared-tools-release-rbac.tf").read_text()
    existing = (ROOT / "platform/automation-infra/tools-deployment.tf").read_text()
    assert 'resource "aws_codebuild_project" "shared_build"' in bootstrap
    assert 'Resource = local.shared_repositories' in bootstrap
    assert 'codebuild/artifacts/shared-agentcore-tools/*' in bootstrap
    assert 'resource "aws_iam_role" "shared_tools_deployment"' in authority
    assert 'repo:${var.repository}:environment:adp-shared-tools-deploy-${var.environment}' in authority
    assert 'var.shared_tools_state_key' in authority
    assert 'kubernetes_groups = ["adp:shared-tools-release-${var.environment}"]' in authority
    assert 'name      = "adp:shared-tools-release-${var.environment}"' in browser
    assert 'namespace = var.shared_tools_browser_namespace' in browser
    assert 'resource "aws_eks_access_policy_association" "shared_tools_deployment"' not in authority
    assert 'DenySharedToolsWrites' in existing


def test_existing_lock_is_required_and_mutually_exclusive(tmp_path):
    backend = tmp_path / "backend.hcl"
    tfvars = tmp_path / "review.tfvars"
    tfvars.write_text('aws_account_id = "123456789012"\naws_region = "us-east-1"\n')
    base = 'bucket = "example-state"\nkey = "approved/tools/agentcore/current.tfstate"\nregion = "us-east-1"\n'
    settings = dict(bucket="example-state", state_key="approved/tools/agentcore/current.tfstate",
                    account="123456789012", region="us-east-1")
    for configuration in (base, base + "use_lockfile = false\n",
                          base + 'dynamodb_table = "example-lock"\nuse_lockfile = true\n'):
        backend.write_text(configuration)
        with pytest.raises(ValueError):
            validate(backend, tfvars, **settings)
    backend.write_text(base + 'dynamodb_table = "example-lock"\n')
    validate(backend, tfvars, lock_table="example-lock", **settings)
    with pytest.raises(ValueError, match="approved binding"):
        validate(backend, tfvars, lock_table="unrelated-lock", **settings)
    with pytest.raises(ValueError, match="approved binding"):
        validate(backend, tfvars, **settings)
