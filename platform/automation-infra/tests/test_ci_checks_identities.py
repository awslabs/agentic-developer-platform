"""Publishing cannot bypass readiness or reuse its provider credentials."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_model_readiness_is_required_before_build_authority():
    jobs = yaml.safe_load((ROOT / '.github/workflows/agent-worker-image.yml').read_text())['jobs']
    assert jobs['build']['needs'] == 'model-readiness'
    check = jobs['model-readiness']
    assert check['environment'].startswith('adp-model-checks-')
    assert 'refs/heads/main' in check['if']
    assert not check.get('continue-on-error', False)
    verify = check['steps'][-1]
    assert '--verify' in verify['run'] and '--prepare' not in verify['run']
    assert not verify.get('continue-on-error', False)
    assert not verify.get('if')
    assert all('enable-bedrock-models.sh' not in s.get('run', '') for s in jobs['build']['steps'])


def test_browser_suites_cannot_fall_back_to_ambient_aws():
    for filename in ['e2e-chat-playwright.yml', 'e2e-new-ui-playwright.yml']:
        workflow = yaml.safe_load((ROOT / '.github/workflows' / filename).read_text())
        job = next(iter(workflow['jobs'].values()))
        assert job['environment'].startswith('adp-browser-checks-')
        identity = next(i for i, step in enumerate(job['steps']) if step.get('uses') == './.github/actions/trusted-checks')
        discovery = next(i for i, step in enumerate(job['steps']) if step.get('name') == 'Resolve dashboard URL')
        assert identity < discovery
        assert job['steps'][identity]['with']['role_arn'] == '${{ vars.ADP_CHECKS_ROLE_ARN }}'
        assert not any('configure-aws-credentials' in s.get('uses', '') for s in job['steps'])
