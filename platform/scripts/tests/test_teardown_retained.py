"""Retained-resource teardown refuses changed owners before any deletion."""

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / 'modules/domain-apps/superplane/scripts'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.syspath_prepend(str(APP))
    return load('retained_storage_tests', APP / 'teardown_storage.py')


def storage_fixture():
    pvc = {'metadata': {'name': 'state', 'namespace': 'app', 'uid': 'original',
                        'labels': {'adp.aws-e.io/installation': 'install'}},
           'spec': {'volumeName': 'pv-original'}}
    pv = {'metadata': {'name': 'pv-original'}, 'spec': {
        'claimRef': {'name': 'state', 'namespace': 'app', 'uid': 'original'},
        'persistentVolumeReclaimPolicy': 'Retain', 'storageClassName': 'superplane-lifecycle-install',
        'csi': {'driver': 'ebs.csi.eks.amazonaws.com', 'volumeHandle': 'vol-original'}}}
    tags = {'eks:eks-cluster-name': 'cluster', 'kubernetes.io/created-for/pv/name': 'pv-original',
            'kubernetes.io/created-for/pvc/name': 'state', 'kubernetes.io/created-for/pvc/namespace': 'app'}
    volume = {'VolumeId': 'vol-original', 'State': 'available', 'Attachments': [],
              'Tags': [{'Key': k, 'Value': v} for k, v in tags.items()]}
    return pvc, pv, volume


@pytest.mark.parametrize('changed', ['claim', 'installation', 'volume', 'cluster', 'attached'])
def test_storage_refuses_changed_binding(storage, changed):
    pvc, pv, volume = storage_fixture()
    storage.validate(pvc, pv, volume, 'install', 'app', 'state', 'cluster')
    if changed == 'claim':
        pv['spec']['claimRef']['uid'] = 'replacement'
    elif changed == 'installation':
        pvc['metadata']['labels']['adp.aws-e.io/installation'] = 'other'
    elif changed == 'volume':
        pv['spec']['csi']['volumeHandle'] = 'vol-other'
    elif changed == 'cluster':
        volume['Tags'][0]['Value'] = 'other'
    else:
        volume['Attachments'] = [{'InstanceId': 'in-use'}]
    with pytest.raises(ValueError):
        storage.validate(pvc, pv, volume, 'install', 'app', 'state', 'cluster')


def test_storage_delete_refuses_replaced_object_before_call(storage, monkeypatch, tmp_path):
    pvc, _, _ = storage_fixture()
    live = copy.deepcopy(pvc)
    live['metadata']['uid'] = 'replacement'
    monkeypatch.setattr(storage, 'get', lambda *a: live)
    monkeypatch.setattr(storage, 'kube', lambda *a: pytest.fail('mutation attempted'))
    with pytest.raises(ValueError, match='replaced'):
        storage.delete_object(SimpleNamespace(), tmp_path, pvc, 'pvc', 'persistentvolumeclaims', 'app')


def test_table_replacement_refused_before_protection_change(monkeypatch, tmp_path):
    module = load('authority_teardown_tests', APP / 'teardown_provider_authority.py')
    address = module.PREFIX + 'aws_dynamodb_table.authority'
    (tmp_path / 'provider-authority-tables.json').write_text(json.dumps({address: {'table_id': 'original'}}))
    monkeypatch.setattr(module, 'table_identity', lambda *a: {'table_id': 'replacement'})
    shared = SimpleNamespace(aws=lambda *a, **kw: pytest.fail('mutation attempted'))
    with pytest.raises(ValueError, match='replaced'):
        module.before_apply(SimpleNamespace(directory=tmp_path), [{'address': address,
            'type': 'aws_dynamodb_table', 'change': {'actions': ['delete'], 'before': {}}}], shared)


def test_partial_webhook_retry_keeps_decommission_root():
    module = load('authority_retry_tests', APP / 'teardown_provider_authority.py')
    shared = SimpleNamespace(instances=lambda state: iter(()))
    result = module.prepare(SimpleNamespace(), {}, shared)
    assert result['module']['superplane_provider_authority']['source'].endswith('/provider-authority/decommission')


def test_fixture_refuses_unknown_state_and_recreated_role(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'platform/scripts'))
    module = load('fixture_teardown_tests', ROOT / 'platform/scripts/teardown_evaluator_fixtures.py')
    attrs = {'id': 'role', 'unique_id': 'original'}
    state = {'resources': [{'mode': 'managed', 'type': 'aws_iam_role', 'name': 'browser',
                           'instances': [{'attributes': attrs}]}]}
    plan = {'resource_changes': [{'address': 'aws_iam_role.browser', 'type': 'aws_iam_role',
                                 'change': {'actions': ['delete'], 'before': dict(attrs, unique_id='replacement')}}]}
    with pytest.raises(ValueError, match='identity changed'):
        module.validate(state, plan, '123456789012', 'test')
    state['resources'][0]['name'] = 'unrelated'
    with pytest.raises(ValueError, match='unknown'):
        module.validate(state, plan, '123456789012', 'test')
