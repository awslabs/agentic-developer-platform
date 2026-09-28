"""Execute deployment guards against a fake CLI, without cloud mutations."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / 'modules/tools/agentcore/infra/deploy.sh'


@pytest.mark.parametrize('actions,allowed', [(['no-op'], True), (['update'], True), (['delete'], False), (['delete', 'create'], False)])
def test_saved_plan_guard_and_built_images_override_tfvars(tmp_path, actions, allowed):
    account = '123456789012'
    image = account + '.dkr.ecr.us-east-1.amazonaws.com/tools@sha256:' + 'a' * 64
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'aws').write_text('#!/bin/sh\necho 123456789012\n')
    (bin_dir / 'terraform').write_text('''#!/usr/bin/env python3
import json,os,sys
with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
if 'show' in sys.argv: print(os.environ['PLAN_JSON'])
''')
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    backend = tmp_path / 'backend.hcl'
    backend.write_text('key = "tools/agentcore/test.tfstate"\n')
    tfvars = tmp_path / 'review.tfvars'
    tfvars.write_text('image_uri = "old-image"\n')
    saved = tmp_path / 'saved.plan'
    saved.touch()
    calls = tmp_path / 'calls'
    env = {**os.environ, 'PATH': str(bin_dir)+':'+os.environ['PATH'], 'EXPECTED_AWS_ACCOUNT_ID': account,
           'AWS_REGION': 'us-east-1', 'TF_VAR_image_uri': image, 'TF_VAR_browser_service_image': image, 'CALLS': str(calls),
           'PLAN_JSON': json.dumps({'resource_changes': [{'change': {'actions': actions}}]})}
    result = subprocess.run(['bash', str(SCRIPT), 'plan', str(backend), str(tfvars), str(saved)], env=env, capture_output=True)
    assert (result.returncode == 0) == allowed
    plan = next(json.loads(line) for line in calls.read_text().splitlines() if '"plan"' in line)
    assert '-var=aws_account_id='+account in plan and '-var=aws_region=us-east-1' in plan
    assert plan.index('-var=image_uri='+image) > plan.index('-var-file='+str(tfvars))
    result = subprocess.run(['bash', str(SCRIPT), 'apply', str(backend), str(saved)], env=env, capture_output=True)
    assert (result.returncode == 0) == allowed
    applied = any('apply' in json.loads(line) for line in calls.read_text().splitlines())
    assert applied == allowed
