"""Keep failed installations actionable without publishing private error text.

Pin, state and no-deletion coverage uses the current grouped campaign in
 test_modular_infra.py; these retain the repair's diagnostics requirement.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location('install_diagnostics', ROOT / 'modules/L5-services/scripts/deploy-infra.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_denied_install_retains_diagnosis_without_private_values(tmp_path, monkeypatch):
    private = 'arn:aws:sts::123456789012:assumed-role/private-deployer/private-session'
    error = f'Error: {private} is not authorized to perform: ssm:DescribeParameters on a private parameter'
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=1, stdout='', stderr=error)
    monkeypatch.setattr(MODULE.subprocess, 'run', run)
    environment = {'TF_DATA_DIR': str(tmp_path / '.terraform'), 'TF_IN_AUTOMATION': 'true'}
    with pytest.raises(RuntimeError, match='ssm:DescribeParameters') as failure:
        MODULE.terraform('security', ['plan', '-input=false'], tmp_path, environment)
    assert private not in str(failure.value)
    assert 'permissions boundary' in str(failure.value)
    assert calls[0][1]['env'] == environment
    receipt = json.loads((tmp_path / 'failure-receipt.json').read_text())
    assert receipt['result'] == 'failed' and receipt['qualification'] == 'not_run'
    assert receipt['stage'] == 'plan' and receipt['category'] == 'access_denied'
    assert receipt['actions'] == ['ssm:DescribeParameters']
    assert private not in json.dumps(receipt)
    assert error in (tmp_path / 'terraform.log').read_text()


@pytest.mark.parametrize('text,category', [
    ('Error: resource has lifecycle.prevent_destroy', 'retained_resource'),
    ('Error acquiring the state lock', 'state_locked'),
    ('AccessDeniedException: private details', 'access_denied'),
    ('Error: private-bucket.example.invalid unexpected response', 'terraform_failed'),
])
def test_failure_classification_uses_only_safe_fixed_text(text, category):
    diagnosis = MODULE.failure_diagnosis(text)
    assert diagnosis['category'] == category
    assert diagnosis['next_action']
    assert text not in json.dumps(diagnosis)


def test_success_does_not_create_a_failure_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='{}', stderr=''))
    assert MODULE.terraform('security', ['output', '-json'], tmp_path, {}) == '{}'
    assert not (tmp_path / 'failure-receipt.json').exists()
