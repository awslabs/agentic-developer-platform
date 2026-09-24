"""Execute dispatch shell with a fake AWS CLI; role/buildspec remain single argv."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[5]
ACTION = yaml.safe_load((ROOT / '.github/actions/codebuild-run/action.yml').read_text())


@pytest.mark.parametrize('pr', [False, True])
def test_start_preserves_release_defaults_and_pins_pr_role(tmp_path, pr):
    cli = tmp_path / 'aws'
    cli.write_text('#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\nPath(os.environ["ARGS_FILE"]).write_text(json.dumps(sys.argv[1:]))\nprint("existing-project:build-id")\n')
    cli.chmod(0o755)
    output = tmp_path / 'output'
    args = tmp_path / 'args'
    env = {**os.environ, 'PATH':str(tmp_path)+':'+os.environ['PATH'],
        'ARGS_FILE':str(args), 'GITHUB_OUTPUT':str(output),
        'STATE_BUCKET':'state', 'AWS_REGION':'us-east-1', 'PROJECT_NAME':'existing-project',
        'SOURCE_KEY':'codebuild/src/existing-project-pr/commit.zip' if pr else 'codebuild/src/existing-project/commit.zip',
        'SOURCE_REVISION':'a'*40, 'ENV_VARS':'', 'CHILD_STATE_URI':'',
        'SERVICE_ROLE_ARN':'arn:aws:iam::123456789012:role/nonpublishing' if pr else '',
        'BUILDSPEC':'path with spaces/ci.yml' if pr else '',
    }
    script = next(s['run'] for s in ACTION['runs']['steps'] if s.get('id') == 'start')
    subprocess.run(['bash','-c',script],env=env,check=True,capture_output=True,text=True)
    command = json.loads(args.read_text())
    assert command[:2] == ['codebuild','start-build']
    assert command[command.index('--source-location-override')+1] == 'state/'+env['SOURCE_KEY']
    assert '--idempotency-token' in command
    if pr:
        assert command[command.index('--service-role-override')+1] == env['SERVICE_ROLE_ARN']
        assert command[command.index('--buildspec-override')+1] == env['BUILDSPEC']
    else:
        assert '--service-role-override' not in command
        assert '--buildspec-override' not in command
