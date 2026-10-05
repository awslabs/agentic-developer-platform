"""Developer jobs must refuse ambient shared identity before reading App secrets."""
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/agent-developer.yml').read_text())
JOB = WORKFLOW['jobs']['work']
GUARD = next(s for s in JOB['steps'] if s.get('name') == 'Verify dedicated developer identity')


def test_workflow_uses_org_pool_and_checks_identity_before_credentials():
    assert JOB['runs-on'] == 'arc-runner-org'
    assert 'id-token' not in JOB['permissions']
    assert JOB['steps'].index(GUARD) < next(
        i for i, s in enumerate(JOB['steps']) if s.get('name') == 'Get App Credentials from AWS Secrets Manager'
    )
    assert 'if' not in GUARD
    assert not GUARD.get('continue-on-error')
    assert GUARD['env'] == {'EXPECTED_AGENT_ROLE_ARN': '${{ vars.ADP_AGENT_WORKFLOW_ROLE_ARN }}'}
    preflight = next(s for s in JOB['steps'] if s.get('name') == 'Verify gateway model authority before the harness')
    assert preflight['working-directory'] == './adp-agent/modules/agent-factory/agent'


@pytest.mark.parametrize('expected,actual,accepted', [
    ('arn:aws:iam::123456789012:role/adp-test-agent-workflow',
     'arn:aws:sts::123456789012:assumed-role/adp-test-agent-workflow/runner', True),
    ('arn:aws:iam::123456789012:role/adp-test-agent-workflow',
     'arn:aws:sts::123456789012:assumed-role/shared-runner/runner', False),
    ('arn:aws:iam::123456789012:role/adp-test-agent-workflow',
     'arn:aws:sts::999999999999:assumed-role/adp-test-agent-workflow/runner', False),
    ('', 'arn:aws:sts::123456789012:assumed-role/shared-runner/runner', False),
    ('arn:aws:iam::123456789012:role/shared-runner',
     'arn:aws:sts::123456789012:assumed-role/shared-runner/runner', False),
])
def test_real_guard_accepts_only_the_configured_dedicated_identity(tmp_path, expected, actual, accepted):
    aws = tmp_path / 'aws'
    aws.write_text('#!/bin/sh\n[ "$1 $2" = "sts get-caller-identity" ] || exit 98\nprintf "%s\\n" "$FIXTURE_ARN"\n')
    aws.chmod(0o755)
    result = subprocess.run(['bash', '-c', GUARD['run']], capture_output=True, text=True,
                            env={'PATH': f'{tmp_path}:/usr/bin:/bin', 'EXPECTED_AGENT_ROLE_ARN': expected,
                                 'FIXTURE_ARN': actual})
    assert (result.returncode == 0) is accepted, result.stderr


def test_both_infrastructure_owners_use_the_same_policy_module():
    for relative in ('infra/agent-workflow-runner.tf', 'runner-infra/infrastructure/agent-workflow.tf'):
        source = (ROOT / 'modules/agent-factory' / relative).read_text()
        assert 'modules/agent-workflow-iam"' in source
        assert 'gateway_execution_arns' in source
    ci = (ROOT / '.github/workflows/automation-trust-ci.yml').read_text()
    assert 'modules/agent-factory/infra/modules/agent-workflow-iam' in ci
    assert 'modules/agent-factory/infra/modules/agent-workflow-pool' in ci
