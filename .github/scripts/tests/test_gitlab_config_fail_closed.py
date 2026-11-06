"""Missing check permissions must fail configuration before live tests can skip."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('failed_service', ['ssm', 'secretsmanager', 'none'])
def test_configuration_errors_are_not_hidden_as_empty_fixtures(tmp_path, failed_service):
    aws = tmp_path / 'aws'
    aws.write_text('''#!/bin/bash
if [ "$1" = "$FAILED_SERVICE" ]; then
  echo "AccessDenied: test permission missing" >&2
  exit 23
fi
if [ "$1" = secretsmanager ]; then echo private-test-placeholder; else echo config-placeholder; fi
''')
    aws.chmod(0o700)
    output = tmp_path / 'github-env'
    action = yaml.safe_load((ROOT / '.github/actions/gitlab-e2e-config/action.yml').read_text())
    result = subprocess.run(['bash', '-c', action['runs']['steps'][0]['run']], env={
        **os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
        'FAILED_SERVICE': failed_service, 'GITHUB_ENV': str(output),
    }, capture_output=True, text=True)
    if failed_service == 'none':
        assert result.returncode == 0
        assert '::add-mask::private-test-placeholder' in result.stdout
        assert 'WEBHOOK_ENDPOINT=config-placeholder' in output.read_text()
    else:
        assert result.returncode != 0
        assert 'AccessDenied' in result.stderr
        assert not output.exists()
