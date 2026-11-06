"""The manual gateway release retains Atlassian pins across plans and applies."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
APPLY = yaml.safe_load((ROOT / ".github/workflows/gateway-infra-apply.yml").read_text())
PLAN = yaml.safe_load((ROOT / ".github/workflows/gateway-infra-plan.yml").read_text())
SCRIPT = ROOT / "scripts/atlassian-mcp-gateway-pins.sh"


def test_manual_gateway_apply_keeps_reviewed_order_and_identical_convergence_inputs():
    workflow = APPLY
    steps = workflow["jobs"]["apply"]["steps"]
    labels = [step.get("name") for step in steps]
    assert "workflow_dispatch" in workflow.get("on", workflow.get(True))
    assert "workflow_run" not in workflow.get("on", workflow.get(True))
    assert workflow["concurrency"]["group"].startswith("gateway-release-")
    assert workflow["on" if "on" in workflow else True]["workflow_dispatch"]["inputs"]["publish_atlassian_mcp_plan"]["default"] is False
    assert workflow["on" if "on" in workflow else True]["workflow_dispatch"]["inputs"]["enable_atlassian_setup_observer_roles"]["default"] is False
    assert labels.index("Terraform Init") < labels.index("Retain native credential owner input")
    assert labels.index("Retain native credential owner input") < labels.index("Retain protected Atlassian MCP provider pins")
    assert labels.index("Retain protected Atlassian MCP provider pins") < labels.index("Terraform Plan (destroy-safety check)")
    assert labels.index("Terraform Plan (destroy-safety check)") < labels.index("Destroy-safety gate") < labels.index("Terraform Apply")
    bootstrap = steps[labels.index("Retain protected Atlassian MCP provider pins")]
    assert bootstrap["env"]["GATEWAY_URL"] == "${{ vars.ADP_FOUNDATION_GATEWAY_URL }}"
    assert bootstrap["env"]["ENABLE_ATLASSIAN_SETUP_OBSERVER_ROLES"] == "${{ inputs.enable_atlassian_setup_observer_roles }}"
    assert "scripts/atlassian-mcp-gateway-pins.sh publish" in bootstrap["run"]
    for label in ("Terraform Plan (destroy-safety check)", "Convergence guard (post-apply plan must be empty)"):
        command = steps[labels.index(label)]["run"]
        assert command.index('"${PIN_ARGS[@]}"') > command.index('-var-file="$ADP_NATIVE_INPUTS"')
        assert "ADP_ATLASSIAN_PINS" in command


def test_gateway_plan_reads_retained_pins_without_publishing():
    steps = PLAN["jobs"]["plan"]["steps"]
    labels = [step.get("name") for step in steps]
    assert labels.index("Terraform Init") < labels.index("Retain protected Atlassian MCP provider pins") < labels.index("Terraform Plan")
    assert "scripts/atlassian-mcp-gateway-pins.sh reuse" in steps[labels.index("Retain protected Atlassian MCP provider pins")]["run"]
    command = steps[labels.index("Terraform Plan")]["run"]
    assert command.index('"${PIN_ARGS[@]}"') > command.index('-var-file="$ADP_NATIVE_INPUTS"')


@pytest.mark.parametrize(
    "mode,state,gateway_url,domain,exit_code,option",
    [
        ("reuse", "missing", None, "d123.cloudfront.net", 0, None),
        ("publish", "missing", None, "d123.cloudfront.net", 2, None),
        ("reuse", "present", None, "d123.cloudfront.net", 0, "--use-retained"),
        ("publish", "present", None, "d123.cloudfront.net", 0, "--publish"),
        ("publish", "present", "https://gateway.example.test", "d123.cloudfront.net", 0, "--publish"),
        ("publish", "present", None, "invalid.example.test", 2, None),
    ],
)
def test_private_pins_require_existing_containers_and_explicit_publication(tmp_path, mode, state, gateway_url, domain, exit_code, option):
    binary = tmp_path / "bin"
    binary.mkdir()
    terraform = binary / "terraform"
    terraform.write_text(
        '#!/bin/sh\nif [ "$1" = state ]; then\n'
        '  if [ "$3" = "aws_iam_role.atlassian_setup_observer[\\"core\\"]" ]; then test "${OBSERVER_STATE:-missing}" = present;\n'
        '  elif [ "$3" = "aws_iam_role.atlassian_setup_observer[\\"adapter\\"]" ]; then test "${OBSERVER_STATE:-missing}" = present;\n'
        '  else test "$TF_STATE_PRESENT" = present; fi\n'
        'elif [ "$1" = output ] && [ "$2" = -raw ] && [ "$3" = frontend_cloudfront_domain_name ]; then\n'
        '  printf "%s\\n" "$TF_FRONTEND_DOMAIN"\n'
        "else\n  exit 1\nfi\n"
    )
    terraform.chmod(0o700)
    python = binary / "python3"
    python.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$CAPTURE_ARGS"\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == --output-dir ]]; then\n'
        '    mkdir -p "$2"\n'
        '    printf "{}" > "$2/atlassian-mcp.tfvars.json"\n'
        "    break\n"
        "  fi\n"
        "  shift\n"
        "done\n"
    )
    python.chmod(0o700)
    github_env = tmp_path / "github-env"
    github_env.touch()
    args = tmp_path / "args"
    env = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "TF_STATE_PRESENT": state,
        "OBSERVER_STATE": "missing",
        "TF_FRONTEND_DOMAIN": domain,
        "CAPTURE_ARGS": str(args),
        "GITHUB_ENV": str(github_env),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": "a" * 40,
        "ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "TF_VAR_environment": "dev",
    }
    env.pop("GATEWAY_URL", None)
    if gateway_url is not None:
        env["GATEWAY_URL"] = gateway_url
    result = subprocess.run(["bash", str(SCRIPT), mode], cwd=ROOT / "modules/gateway/infra", env=env, capture_output=True, text=True, check=False)
    assert result.returncode == exit_code
    if option is None:
        assert not args.exists() and github_env.read_text() == ""
    else:
        passed = args.read_text().splitlines()
        assert option in passed
        assert passed[passed.index("--gateway-url") + 1] == (gateway_url or "https://d123.cloudfront.net")
        assert github_env.read_text().startswith("ADP_ATLASSIAN_PINS=")
        pins = Path(github_env.read_text().strip().split("=", 1)[1])
        assert pins.parent.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "present,opt_in,expect_pin",
    [
        (False, "false", False),
        (False, "true", True),
        (True, "false", True),
    ],
)
def test_observer_roles_are_opted_in_once_and_then_retained(tmp_path, present, opt_in, expect_pin):
    binary = tmp_path / "terraform"
    binary.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == state && "$3" == aws_iam_role.atlassian_setup_observer* ]]; then\n'
        f"  {'exit 0' if present else 'exit 1'}\n"
        "fi\nexit 1\n"
    )
    binary.chmod(0o700)
    scripts = tmp_path / "bin"
    scripts.mkdir()
    (scripts / "terraform").symlink_to(binary)
    target = tmp_path / "github-env"
    result = subprocess.run(
        ["bash", str(SCRIPT), "reuse"],
        cwd=ROOT / "modules/gateway/infra",
        env={**os.environ, "PATH": f"{scripts}:{os.environ['PATH']}", "GITHUB_ENV": str(target), "ENABLE_ATLASSIAN_SETUP_OBSERVER_ROLES": opt_in},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert (target.exists() and "TF_VAR_atlassian_mcp_setup_observer_roles_enabled=true" in target.read_text()) == expect_pin


def test_observer_role_partial_state_refuses_a_destructive_plan(tmp_path):
    scripts = tmp_path / "bin"
    scripts.mkdir()
    terraform = scripts / "terraform"
    terraform.write_text('#!/bin/bash\nif [[ "$1" == state && "$3" == *"[\\"core\\"]" ]]; then exit 0; fi\nexit 1\n')
    terraform.chmod(0o700)
    target = tmp_path / "github-env"
    result = subprocess.run(
        ["bash", str(SCRIPT), "reuse"],
        cwd=ROOT / "modules/gateway/infra",
        env={**os.environ, "PATH": f"{scripts}:{os.environ['PATH']}", "GITHUB_ENV": str(target)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "incomplete Terraform state" in result.stderr
    assert not target.exists()
