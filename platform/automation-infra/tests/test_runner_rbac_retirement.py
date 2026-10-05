"""Source bindings and real-shell authorization-check regressions; no cluster calls."""
import os
from pathlib import Path
import re
from string import Template
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_no_literal_shared_runner_subject_in_terraform_bindings():
    # Walk every subject, so a new binding cannot escape a hand-maintained list.
    files = list((ROOT / 'platform').rglob('*.tf')) + list((ROOT / 'modules').rglob('*.tf'))
    failures = []
    for path in files:
        for subject in re.findall(r'\bsubject\s*\{([^{}]*)\}', path.read_text(), re.S):
            if 'github-runner-sa' in subject:
                failures.append(str(path.relative_to(ROOT)))
    assert failures == [], failures


def test_legacy_template_variables_cannot_restore_runner_binding():
    source = (ROOT / 'modules/agent-context/manifests/ingestion-rbac.yaml').read_text()
    rendered = Template(source).substitute(
        NAMESPACE='agent-context', RUNNER_NAMESPACE='arc-runners',
        RUNNER_SERVICE_ACCOUNT='github-runner-sa',
    )
    binding = next(x for x in yaml.safe_load_all(rendered) if x['kind'] == 'RoleBinding')
    assert binding['subjects'] == [{
        'kind': 'Group', 'name': 'adp:trusted-deployment',
        'apiGroup': 'rbac.authorization.k8s.io',
    }]


@pytest.mark.parametrize('mode,failures', [('correct', 0), ('retained', 3), ('api-error', 3), ('no-deployment', 3)])
def test_validator_distinguishes_denial_from_errors_and_retained_access(tmp_path, mode, failures):
    kubectl = tmp_path / 'kubectl'
    kubectl.write_text('''#!/bin/bash
case "$*" in
  *--as=adp-trusted-deployment-validation*)
    if [ "$MODE" = no-deployment ]; then echo no; exit 1; fi
    echo yes; exit 0 ;;
  *)
    case "$MODE" in
      retained) echo yes; exit 0 ;;
      api-error) echo 'Error from server (Forbidden): cannot impersonate'; exit 1 ;;
      *) echo no; exit 1 ;;
    esac ;;
esac
''')
    kubectl.chmod(0o755)
    source = (ROOT / 'modules/agent-context/scripts/validate.sh').read_text()
    section = source.split('# Authorization checks do not retrieve secrets', 1)[1].split('# ─── Check 11:', 1)[0]
    section = '# Authorization checks do not retrieve secrets' + section
    script = '''NAMESPACE=agent-context
RUNNER_NS=arc-runners
RUNNER_SA=github-runner-sa
check_pass() { echo PASS; }
check_fail() { echo FAIL; }
''' + section
    result = subprocess.run(['bash', '-c', script], env={**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH'], 'MODE': mode}, text=True, capture_output=True, check=True)
    assert result.stdout.splitlines().count('FAIL') == failures, result.stdout + result.stderr
    assert len(result.stdout.splitlines()) == 6
