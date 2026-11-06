"""A core reset must not strand separately managed workspace infrastructure."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


teardown = load('teardown_auxiliary_test_engine', ROOT / 'platform/scripts/teardown.py')
inventory = teardown.Run.superplane_inventory()
ACCOUNT = '123456789012'
BUCKET = 'adp-terraform-state-' + ACCOUNT
KEY = 'dev/modules/superplane-workspaces/v2/org/workspace/terraform.tfstate'


def discover(tmp_path, documents):
    calls = []

    def aws(*args, **kwargs):
        calls.append(args)
        assert args[:2] in [('s3api', 'list-objects-v2'), ('s3api', 'get-object')]
        assert args[args.index('--expected-bucket-owner') + 1] == ACCOUNT
        assert args[args.index('--bucket') + 1] == BUCKET
        if args[1] == 'list-objects-v2':
            assert args[args.index('--prefix') + 1] == 'dev/modules/superplane-'
            return {'Contents': [{'Key': key} for key in documents]}
        key = args[args.index('--key') + 1]
        if documents[key] is None:
            return None
        Path(args[-1]).write_text(json.dumps(documents[key]))
        return {}

    result = inventory.discover(call=aws, account=ACCOUNT, bucket=BUCKET,
                                environment='dev', directory=tmp_path / 'inventory')
    return result, calls


def managed():
    return {'lineage': 'original', 'resources': [
        {'mode': 'managed', 'type': 'aws_eks_cluster', 'name': 'workspace',
         'instances': [{'attributes': {'id': 'owned-workspace'}}]}]}


def test_discovery_is_read_only_and_keeps_private_state(tmp_path):
    states, calls = discover(tmp_path, {KEY: managed()})
    assert len(calls) == 2
    assert states[KEY]['resource_count'] == 1
    assert states[KEY]['lineage'] == 'original'
    path = Path(states[KEY]['state_file'])
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(path.read_text()) == managed()


def test_unknown_app_state_blocks_but_other_environment_and_backups_are_not_selected(tmp_path):
    unknown = 'dev/modules/superplane-new-owner/custom.tfstate'
    states, calls = discover(tmp_path, {
        unknown: managed(),
        KEY.replace('dev/', 'prod/', 1): managed(),
        'dev/modules/superplane/terraform.tfstate': managed(),
        KEY + '.backup': managed(),
    })
    assert list(states) == [unknown]
    assert len(calls) == 2


def test_empty_retired_state_and_data_sources_do_not_block(tmp_path):
    states, _ = discover(tmp_path, {
        KEY: {'resources': []},
        'dev/modules/superplane-domain-runtime/terraform.tfstate': {
            'resources': [{'mode': 'data', 'instances': [{'attributes': {'id': 'shared'}}]}]},
    })
    assert states == {}


@pytest.mark.parametrize('document', [None, {}, [], {'resources': 'invalid'}])
def test_missing_or_malformed_state_is_not_mistaken_for_absence(tmp_path, document):
    with pytest.raises((ValueError, TypeError)):
        discover(tmp_path, {KEY: document})


@pytest.mark.parametrize('phase', ['superplane', 'webhook_ingress', 'gateway', 'platform'])
def test_core_dependency_selection_refuses_before_planning(tmp_path, phase):
    run = teardown.Run(tmp_path, ACCOUNT, 'us-east-1', 'dev')
    run.states = {phase: {} for phase in teardown.ORDER}
    run.superplane_auxiliary_states = {KEY: {'resource_count': 1}}
    with pytest.raises(teardown.TeardownError, match='app-owned lifecycle scripts'):
        run.check_selection([phase])


def test_unrelated_module_maintenance_is_still_available(tmp_path):
    run = teardown.Run(tmp_path, ACCOUNT, 'us-east-1', 'dev')
    run.states = {phase: {} for phase in teardown.ORDER}
    run.superplane_auxiliary_states = {KEY: {'resource_count': 1}}
    run.check_selection(['insights'])


def test_inventory_permission_error_is_not_mistaken_for_absence(tmp_path):
    def denied(*args, **kwargs):
        raise teardown.TeardownError('AccessDenied')

    with pytest.raises(teardown.TeardownError, match='AccessDenied'):
        inventory.discover(call=denied, account=ACCOUNT, bucket=BUCKET,
                           environment='dev', directory=tmp_path)
