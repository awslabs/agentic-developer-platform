"""
Backend state-locking regression tests.

Verifies that every supported terraform init entry point configures state
locking (via -backend-config referencing the dynamodb_table), and that
deploy.sh rejects a missing backend-config file rather than falling through
to an unlocked backend.

Covers CKV_TF_3 (checkov) and issue #6101.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import ClassVar

import pytest

from ..config import MODULE_ROOT, TERRAFORM_DIR

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = MODULE_ROOT.parent.parent  # workspace root
BACKEND_TFVARS = REPO_ROOT / "environments" / "dev" / "modules" / "agent-context-backend.tfvars"
DEPLOY_SCRIPT = MODULE_ROOT / "deploy.sh"


# ---------------------------------------------------------------------------
# 1. backend.tf contains dynamodb_table inline
# ---------------------------------------------------------------------------


class TestBackendTfLocking:
    """backend.tf must declare the locking table so CKV_TF_3 is satisfied and
    any init — even without -backend-config — includes the lock setting."""

    def test_dynamodb_table_in_backend_tf(self):
        backend_tf = TERRAFORM_DIR / "backend.tf"
        assert backend_tf.exists(), "backend.tf missing"
        content = backend_tf.read_text()
        assert "dynamodb_table" in content, (
            "backend.tf must contain dynamodb_table for state locking"
        )
        assert "adp-terraform-locks" in content, (
            "backend.tf dynamodb_table must reference adp-terraform-locks"
        )


# ---------------------------------------------------------------------------
# 2. Backend tfvars contains dynamodb_table
# ---------------------------------------------------------------------------


class TestBackendTfvarsLocking:
    """The environment backend-config file must specify the lock table."""

    def test_tfvars_contains_dynamodb_table(self):
        if not BACKEND_TFVARS.exists():
            pytest.skip("Backend tfvars not found at expected path")
        content = BACKEND_TFVARS.read_text()
        assert "dynamodb_table" in content, (
            "agent-context-backend.tfvars must contain dynamodb_table"
        )
        assert "adp-terraform-locks" in content, (
            "agent-context-backend.tfvars dynamodb_table must reference adp-terraform-locks"
        )


# ---------------------------------------------------------------------------
# 3. deploy.sh passes -backend-config on every terraform init
# ---------------------------------------------------------------------------


class TestDeployShBackendConfig:
    """deploy.sh must pass -backend-config to every terraform init call."""

    def test_deploy_sh_exists(self):
        assert DEPLOY_SCRIPT.exists(), "deploy.sh not found"

    def test_no_bare_terraform_init(self):
        """Every 'terraform init' in deploy.sh must include -backend-config."""
        content = DEPLOY_SCRIPT.read_text()
        # Find all terraform init lines (ignoring comments)
        init_lines = []
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"\bterraform\s+init\b", stripped):
                init_lines.append((i, stripped))

        assert init_lines, "No terraform init calls found in deploy.sh"

        for lineno, line in init_lines:
            assert "-backend-config" in line, (
                f"deploy.sh line {lineno}: terraform init without -backend-config: {line}"
            )

    def test_backend_config_uses_input_false(self):
        """Every terraform init should also use -input=false for non-interactive use."""
        content = DEPLOY_SCRIPT.read_text()
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"\bterraform\s+init\b", stripped):
                assert "-input=false" in stripped, (
                    f"deploy.sh line {i}: terraform init without -input=false: {stripped}"
                )


# ---------------------------------------------------------------------------
# 4. deploy.sh rejects missing backend-config file
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_deploy(tmp_path):
    """Run the unmodified deploy entry point with only external effects stubbed."""
    import shutil

    module = tmp_path / "repo" / "modules" / "agent-context"
    module.mkdir(parents=True)
    for name in ("deploy.sh", "config.env"):
        shutil.copy2(MODULE_ROOT / name, module / name)
    (module / "terraform").mkdir()
    scripts = module / "scripts"
    scripts.mkdir()
    shutil.copy2(MODULE_ROOT / "scripts/ensure-zoekt-auth.py", scripts)
    (scripts / "_common.sh").write_text(
        "template_file() { printf 'test-manifest\\n'; }\n"
        "resolve_acl_config() { :; }\n"
    )
    (scripts / "deploy-litellm-proxy.sh").write_text("exit 0\n")
    backend = tmp_path / "repo/environments/dev/modules/agent-context-backend.tfvars"
    backend.parent.mkdir(parents=True)
    backend.write_text('dynamodb_table = "adp-terraform-locks"\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "commands.log"
    for tool in ("aws", "kubectl", "terraform", "gh"):
        executable = bindir / tool
        executable.write_text(
            "#!/bin/bash\n"
            f"printf '%s\\n' '{tool}'\" $*\" >> \"$COMMAND_LOG\"\n"
            # Return a synthetic existing key while exercising the real helper.
            'if [[ "' + tool + '" == kubectl && "$*" == "get secret zoekt-backend-auth "* ]]; then\n'
            '  printf \'%s\\n\' \'{"data":{"api-key":"dGVzdC16b2VrdC1rZXktZm9yLWRlcGxveW1lbnQtdGVzdHM="}}\'\n'
            'fi\n'
            # Consume piped manifests so a producer cannot get SIGPIPE.
            'if [[ "$*" == *"-f -"* ]]; then cat >/dev/null; fi\n'
            # A failed init must stop deployment rather than reaching apply.
            'if [[ "' + tool + '" == terraform && "$1" == init ]]; then\n'
            '  exit "${INIT_STATUS:-0}"\n'
            'fi\n'
        )
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:/usr/bin:/bin",
        "COMMAND_LOG": str(log),
        "ENVIRONMENT": "dev",
        "S3_FILES_ENABLED": "false",
        "S3_VECTORS_ENABLED": "false",
        "DEEPWIKI_ENABLED": "false",
        "INGESTION_REFRESH_ENABLED": "false",
        "SYNTHESIS_ENABLED": "false",
        "VULN_SCAN_ENABLED": "false",
        "GRAPHRAG_ENABLED": "false",
        "PERSONAL_CONTEXT_ONLY": "false",
        "CONTEXT_MCP_IMAGE": "example.invalid/context@sha256:" + "a" * 64,
    }

    def run(*args, graphrag=False, init_status=0):
        result = subprocess.run(
            ["bash", str(module / "deploy.sh"), "--skip-validate", *args],
            cwd=tmp_path, capture_output=True, text=True, timeout=10,
            env={**env, "GRAPHRAG_ENABLED": str(graphrag).lower(),
                 "INIT_STATUS": str(init_status)},
        )
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    return run, backend


@pytest.mark.parametrize("personal", [False, True])
def test_actual_deploy_initializes_selected_locked_backend(isolated_deploy, personal):
    run, backend = isolated_deploy
    result, calls = run(*(["--personal-context-only"] if personal else []), graphrag=personal)
    assert result.returncode == 0, result.stdout + result.stderr
    inits = [line for line in calls if line.startswith("terraform init ")]
    assert len(inits) == 1
    assert "-input=false" in inits[0]
    backend_arg = next(arg for arg in inits[0].split() if arg.startswith("-backend-config="))
    from pathlib import Path
    assert Path(backend_arg.split("=", 1)[1]).resolve() == backend
    assert any(line.startswith("terraform apply ") for line in calls)


@pytest.mark.parametrize("personal", [False, True])
def test_missing_backend_stops_before_any_external_command(isolated_deploy, personal):
    run, backend = isolated_deploy
    backend.unlink()
    result, calls = run(*(["--personal-context-only"] if personal else []), graphrag=personal)
    assert result.returncode != 0
    assert "Backend config not found" in result.stderr
    assert calls == []


@pytest.mark.parametrize("args,graphrag", [
    (["--skip-terraform"], False),
    (["--personal-context-only", "--skip-terraform"], True),
    (["--personal-context-only"], False),
])
def test_modes_without_init_do_not_require_backend(isolated_deploy, args, graphrag):
    run, backend = isolated_deploy
    backend.unlink()
    result, calls = run(*args, graphrag=graphrag)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(line.startswith(("terraform init ", "terraform apply ")) for line in calls)


def test_failed_init_never_applies(isolated_deploy):
    run, _ = isolated_deploy
    result, calls = run(init_status=73)
    assert result.returncode == 73
    assert not any(line.startswith("terraform apply ") for line in calls)


# ---------------------------------------------------------------------------
# 5. CI workflows pass -backend-config to terraform init
# ---------------------------------------------------------------------------


class TestWorkflowBackendConfig:
    """All agent-context-infra-*.yml workflows must pass -backend-config."""

    WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
    WORKFLOW_PATTERNS: ClassVar[list[str]] = [
        "agent-context-infra-plan.yml",
        "agent-context-infra-apply.yml",
        "agent-context-infra-destroy.yml",
    ]

    def test_workflows_use_backend_config(self):
        for name in self.WORKFLOW_PATTERNS:
            wf = self.WORKFLOW_DIR / name
            if not wf.exists():
                pytest.skip(f"Workflow {name} not found")
            content = wf.read_text()
            # Find terraform init lines (in run: blocks, not comments)
            init_lines = [
                (i, line.strip())
                for i, line in enumerate(content.splitlines(), 1)
                if re.search(r"\bterraform\s+init\b", line.strip())
                and not line.strip().startswith("#")
            ]
            # The validate-pr-config job uses -backend=false which is fine
            for lineno, line in init_lines:
                if "-backend=false" in line:
                    continue  # validation-only init, no state needed
                assert "-backend-config" in line, (
                    f"{name} line {lineno}: terraform init without -backend-config: {line}"
                )
