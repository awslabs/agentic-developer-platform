"""Static check: no workflow dispatch input is interpolated inside a run: block.

GitHub Actions evaluates ${{ github.event.inputs.* }} and ${{ inputs.* }}
expressions BEFORE passing the result to the shell.  A crafted dispatch value
can therefore inject arbitrary commands.  The safe pattern is to assign the
expression to a step-level env: variable and reference the env var in the shell.

This test covers the 24 workflow files scoped by #6117.  It reads each YAML
file, extracts every run: block, and asserts none contain a bare input
expression.  Expressions in env:, with:, if:, name:, concurrency:, or
run-name: are safe and excluded.

References:
  - https://securitylab.github.com/research/github-actions-untrusted-input/
  - semgrep rule: run-shell-injection
  - Issue #6117, parent #5599
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The 24 workflow files scoped by #6117.
SCOPED_WORKFLOWS = [
    "_deploy-eks.yml",
    "agent-context-deploy.yml",
    "agent-context-infra-apply.yml",
    "agent-context-infra-destroy.yml",
    "agent-context-verb-ops.yml",
    "agent-factory-infra-destroy.yml",
    "credential-binding-adversarial-e2e.yml",
    "credential-binding-flip.yml",
    "e2e-chat-playwright.yml",
    "e2e-new-ui-playwright.yml",
    "engine-bridge-smoke-seed.yml",
    "eval-cli-uplift.yml",
    "gateway-deploy.yml",
    "gateway-infra-apply.yml",
    "gateway-infra-destroy.yml",
    "gbrain-infra-destroy.yml",
    "gitlab-infra-destroy.yml",
    "orchestration-live-tests.yml",
    "platform-deploy-mgmt-infra-apply.yml",
    "platform-infra-apply.yml",
    "platform-infra-destroy.yml",
    "seed-hosted-tenant.yml",
    "undeploy.yml",
    "webhook-ingress-destroy.yml",
]

# Matches ${{ github.event.inputs.ANYTHING }} or ${{ inputs.ANYTHING }}
INPUT_EXPR = re.compile(
    r"\$\{\{\s*(?:github\.event\.inputs|inputs)\.\w+",
)


def _extract_run_blocks(content: str) -> list[tuple[int, str]]:
    """Parse actual YAML so both `run:` and `- run:` steps are covered."""
    import yaml

    document = yaml.safe_load(content)
    return [
        (index, str(step["run"]))
        for job in document.get("jobs", {}).values()
        for index, step in enumerate(job.get("steps", []), 1)
        if "run" in step
    ]


def _workflow_paths() -> list[Path]:
    """Return the list of existing in-scope workflow paths."""
    wf_dir = REPO_ROOT / ".github" / "workflows"
    return [wf_dir / name for name in SCOPED_WORKFLOWS if (wf_dir / name).exists()]


@pytest.mark.parametrize(
    "workflow",
    _workflow_paths(),
    ids=lambda p: p.name,
)
def test_no_input_interpolation_in_run_blocks(workflow: Path) -> None:
    """Assert that no run: block contains a bare input expression."""
    content = workflow.read_text()
    violations: list[str] = []
    for line_num, block_text in _extract_run_blocks(content):
        for match in INPUT_EXPR.finditer(block_text):
            # The match is inside a run: block — that is the vulnerability.
            violations.append(
                f"  {workflow.name}:{line_num}: {match.group(0)}"
            )

    assert not violations, (
        "Dispatch input expressions found inside run: blocks (shell injection risk).\n"
        "Move each expression to a step-level env: block and reference the env var.\n"
        + "\n".join(violations)
    )


def _step_script(workflow: str, name: str) -> str:
    import yaml
    document = yaml.safe_load((REPO_ROOT / '.github/workflows' / workflow).read_text())
    return next(step['run'] for job in document['jobs'].values()
                for step in job.get('steps', []) if step.get('name') == name)


def _execute_step(tmp_path, workflow, name, values, stub=None):
    import os
    import subprocess
    import sys
    script = _step_script(workflow, name)
    # Keep the workflow's fixed scratch path inside the isolated test directory.
    script = script.replace('/tmp/seed.sql', str(tmp_path / 'seed.sql'))
    env = dict(os.environ, GITHUB_ENV=str(tmp_path / 'github-env'),
               GITHUB_OUTPUT=str(tmp_path / 'github-output'), **values)
    if stub:
        command = tmp_path / stub
        command.write_text(f'#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ["CAPTURE_ARGS"]).write_text(json.dumps(sys.argv[1:]))\n')
        command.chmod(0o755)
        env['PATH'] = str(tmp_path) + ':' + env['PATH']
        env['CAPTURE_ARGS'] = str(tmp_path / 'arguments.json')
    return subprocess.run(['/bin/bash', '-c', script], cwd=tmp_path,
                          env=env, capture_output=True, text=True)


@pytest.mark.parametrize('value', ['dev\nINJECTED=value', 'dev\rINJECTED=value', '$(touch injected)'])
def test_environment_rejects_control_or_shell_text(tmp_path, value):
    result = _execute_step(tmp_path, 'e2e-new-ui-playwright.yml', 'Resolve environment',
                           {'INPUT_ENVIRONMENT': value})
    assert result.returncode != 0
    assert not (tmp_path / 'github-env').exists()
    assert not (tmp_path / 'injected').exists()


def test_normal_environment_exports_one_value(tmp_path):
    result = _execute_step(tmp_path, 'e2e-new-ui-playwright.yml', 'Resolve environment',
                           {'INPUT_ENVIRONMENT': 'dev'})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'github-env').read_text() == 'ENVIRONMENT=dev\n'


@pytest.mark.parametrize('workflow', ['e2e-new-ui-playwright.yml', 'e2e-chat-playwright.yml'])
@pytest.mark.parametrize('suffix', ['\nINJECTED=value', '\rINJECTED=value'])
def test_url_cannot_inject_environment_entries(tmp_path, workflow, suffix):
    result = _execute_step(tmp_path, workflow, 'Resolve dashboard URL',
                           {'INPUT_CLOUDFRONT_URL': 'https://example.com/' + suffix})
    assert result.returncode != 0
    assert not (tmp_path / 'github-env').exists()


@pytest.mark.parametrize('workflow', ['e2e-new-ui-playwright.yml', 'e2e-chat-playwright.yml'])
def test_shell_characters_in_url_remain_data(tmp_path, workflow):
    url = 'https://example.com/$(touch injected)'
    result = _execute_step(tmp_path, workflow, 'Resolve dashboard URL',
                           {'INPUT_CLOUDFRONT_URL': url})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'github-env').read_text() == f'E2E_CLOUDFRONT_URL={url}\n'
    assert not (tmp_path / 'injected').exists()


@pytest.mark.parametrize('mode', ['resume', 'cleanup'])
def test_qualification_id_is_one_argument(tmp_path, mode):
    import json
    value = 'id --run $(touch injected) *'
    result = _execute_step(tmp_path, 'orchestration-live-tests.yml', 'Run qualification',
                           {'INPUT_MODE': mode, 'INPUT_QUALIFICATION_ID': value,
                            'INPUT_CONFIG_PATH': 'config.json'}, stub='python')
    assert result.returncode == 0, result.stderr
    args = json.loads((tmp_path / 'arguments.json').read_text())
    assert f'--{mode}={value}' in args
    assert '--run' not in args
    assert not (tmp_path / 'injected').exists()


def test_seed_sql_uses_quoted_psql_values(tmp_path):
    import json
    result = _execute_step(tmp_path, 'seed-hosted-tenant.yml', 'Build seed SQL', {})
    assert result.returncode == 0, result.stderr
    sql = (tmp_path / 'seed.sql').read_text()
    assert ":'org_name'" in sql and ":'tenant_id'" in sql
    name = "O'Reilly'); DROP TABLE organizations; -- $(touch injected)"
    result = _execute_step(tmp_path, 'seed-hosted-tenant.yml', 'Execute seed',
                           {'SEED_ORG_NAME': name, 'SEED_TENANT_ID': 'test-org',
                            'SEED_INSTALLATION_ID': '123'}, stub='psql')
    assert result.returncode == 0, result.stderr
    args = json.loads((tmp_path / 'arguments.json').read_text())
    assert f'org_name={name}' in args
    assert 'tenant_id=test-org' in args
    assert name not in sql
    assert not (tmp_path / 'injected').exists()


def test_backfill_repository_is_one_argument(tmp_path):
    import json
    script = tmp_path / 'modules/agent-context/scripts/backfill_neptune_from_s3.py'
    script.parent.mkdir(parents=True)
    script.write_text('# input fixture\n')
    repository = 'owner/repo --apply $(touch injected)'
    result = _execute_step(tmp_path, 'agent-context-verb-ops.yml', 'Neptune backfill',
                           {'ADP_INPUT_C27AA2207B': 'dry-run', 'ADP_INPUT_5360141334': repository,
                            'INGESTION_IMAGE': 'fixture', 'S3_FILES_BUCKET': 'fixture',
                            'NEPTUNE_ENDPOINT': 'fixture', 'NS': 'fixture'}, stub='kubectl')
    assert result.returncode == 0, result.stderr
    args = json.loads((tmp_path / 'arguments.json').read_text())
    assert f'--repo={repository}' in args
    assert '--apply' not in args
    assert not (tmp_path / 'injected').exists()
