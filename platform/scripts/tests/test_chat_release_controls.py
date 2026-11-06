"""Chat releases must verify existing controls without rewriting platform security."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('chat_controls', ROOT / 'platform/scripts/verify-chat-release-controls.py')
controls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controls)


def policy():
    return {'apiVersion': 'admissionregistration.k8s.io/v1', 'kind': 'ValidatingAdmissionPolicy',
            'metadata': {'name': 'test'}, 'spec': {'failurePolicy': 'Fail', 'matchConstraints': {'resourceRules': [{'resources': ['pods']}]}, 'validations': [{'expression': 'false'}]}}


def test_server_metadata_and_documented_defaults_do_not_fake_drift():
    expected = policy()
    live = copy.deepcopy(expected)
    live['metadata'].update(uid='server-id', resourceVersion='42', managedFields=[{}])
    live['status'] = {'typeChecking': {}}
    live['spec']['matchConstraints'].update(matchPolicy='Equivalent', namespaceSelector={}, objectSelector={})
    controls.verify(expected, live)


@pytest.mark.parametrize('mutation', ['allow', 'ignore', 'selector', 'missing'])
def test_weakened_or_missing_admission_is_refused(mutation):
    expected = policy()
    live = copy.deepcopy(expected)
    if mutation == 'allow': live['spec']['validations'][0]['expression'] = 'true'
    if mutation == 'ignore': live['spec']['failurePolicy'] = 'Ignore'
    if mutation == 'selector': live['spec']['matchConstraints']['namespaceSelector'] = {'matchLabels': {'bypass': 'yes'}}
    if mutation == 'missing': live = {'kind': 'List', 'items': []}
    with pytest.raises(RuntimeError, match='prerequisites differ'):
        controls.verify(expected, live)


def test_extra_rbac_authority_is_refused():
    expected = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRole', 'metadata': {'name': 'test'}, 'rules': [{'verbs': ['get'], 'resources': ['services']}]}
    live = copy.deepcopy(expected)
    live['rules'][0]['verbs'].append('patch')
    with pytest.raises(RuntimeError): controls.verify(expected, live)


def test_multidocument_kubectl_output_is_compared_without_mutation(monkeypatch):
    expected = policy()
    second = copy.deepcopy(expected)
    second['metadata']['name'] = 'other'
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        return json.dumps(expected) + '\n' + json.dumps(second)
    monkeypatch.setattr(controls.subprocess, 'check_output', command)
    controls.main(['controls.yaml'])
    assert calls == [
        ['kubectl', 'create', '--dry-run=client', '--validate=false', '-f', 'controls.yaml', '-o', 'json'],
        ['kubectl', 'get', '-f', 'controls.yaml', '-o', 'json'],
    ]


def test_read_binding_cannot_grant_mutation_or_open_ended_reads():
    documents = list(yaml.safe_load_all((ROOT / 'platform/automation-infra/chat-release-read-rbac.yaml').read_text()))
    for document in documents:
        for rule in document.get('rules', []):
            assert rule['verbs'] == ['get']
            assert rule['resourceNames'] and all('*' not in name for name in rule['resourceNames'])
            assert '*' not in rule['resources']
    workflow = yaml.safe_load((ROOT / '.github/workflows/chat-agent-deploy.yml').read_text())
    assert workflow['env']['ADP_CHAT_VERIFY_EXISTING_CONTROLS'] == 'true'
