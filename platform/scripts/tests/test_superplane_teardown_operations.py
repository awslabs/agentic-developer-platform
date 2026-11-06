"""Installation destruction preserves state ownership and saved-plan identities."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / 'modules/domain-apps/superplane'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


t = load('superplane_destroy_test', APP / 'scripts/teardown_infrastructure.py')
q = load('superplane_quiesce_test', APP / 'scripts/quiesce_for_teardown.py')
shared = t.engine()
ACCOUNT = '123456789012'
ORG = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
WORKSPACE = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


def proposal(actions=None, unique='AROAORIGINAL', account=ACCOUNT):
    before = {'id': 'owned-role', 'arn': f'arn:aws:iam::{account}:role/owned-role', 'unique_id': unique}
    state = {'resources': [{'mode': 'managed', 'type': 'aws_iam_role', 'name': 'reader',
                           'instances': [{'attributes': dict(before, unique_id='AROAORIGINAL')}]}]}
    plan = {'resource_changes': [{'address': 'aws_iam_role.reader', 'type': 'aws_iam_role',
                                 'change': {'actions': actions or ['delete'], 'before': before}}]}
    return plan, state


def test_recreated_role_is_refused_even_when_name_and_arn_match():
    plan, state = proposal(unique='AROAREPLACEMENT')
    with pytest.raises(ValueError, match='identity changed'):
        t.validate_plan(plan, state, {'aws_iam_role.reader'}, ACCOUNT, shared)


@pytest.mark.parametrize('actions', [['create'], ['update'], ['delete', 'create']])
def test_decommissioning_never_creates_updates_or_replaces(actions):
    plan, state = proposal(actions)
    with pytest.raises(shared.TeardownError, match='Non-delete'):
        t.validate_plan(plan, state, {'aws_iam_role.reader'}, ACCOUNT, shared)


def test_another_account_and_unknown_address_are_refused():
    plan, state = proposal(account='222233334444')
    with pytest.raises(ValueError, match='another account'):
        t.validate_plan(plan, state, {'aws_iam_role.reader'}, ACCOUNT, shared)
    plan, state = proposal()
    with pytest.raises(ValueError, match='unfamiliar'):
        t.validate_plan(plan, state, set(), ACCOUNT, shared)


def test_input_target_matches_immutable_backend_key():
    values = {'account_id': ACCOUNT, 'aws_region': 'us-east-1', 'environment': 'dev',
              'org_id': ORG, 'workspace_id': WORKSPACE}
    assert t.check_variables('lifecycle-foundations', values, ACCOUNT, 'us-east-1', 'dev') == (
        f'dev/modules/superplane-lifecycle-foundations/v1/{ORG}/{WORKSPACE}/terraform.tfstate')
    with pytest.raises(ValueError, match='account/environment'):
        t.check_variables('lifecycle-foundations', values, '222233334444', 'us-east-1', 'dev')
    with pytest.raises(ValueError, match='region'):
        t.check_variables('lifecycle-foundations', values, ACCOUNT, 'us-west-2', 'dev')


def test_retained_owner_refuses_live_workspace_before_planning(tmp_path, monkeypatch):
    _, state = proposal()
    monkeypatch.setattr(t, 'state_at', lambda *a: state)
    with pytest.raises(ValueError, match='dependency still exists'):
        t.check_dependencies(shared, 'lifecycle-foundations', {'org_id': ORG, 'workspace_id': WORKSPACE},
                             'backend', ACCOUNT, 'dev', tmp_path)


def test_pause_patches_are_bound_to_uid_and_resource_version():
    item = {'kind': 'Deployment', 'metadata': {'uid': 'original', 'resourceVersion': '31'},
            'spec': {'replicas': 2}}
    assert q.patch_for(item) == [
        {'op': 'test', 'path': '/metadata/uid', 'value': 'original'},
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '31'},
        {'op': 'add', 'path': '/spec/replicas', 'value': 0},
    ]


def test_completed_jobs_and_already_paused_controllers_are_preserved():
    assert not q.patch_for({'kind': 'Job', 'status': {'conditions': [{'type': 'Complete', 'status': 'True'}]}})
    assert not q.patch_for({'kind': 'Deployment', 'spec': {'replicas': 0}})
    assert not q.patch_for({'kind': 'ScaledJob', 'metadata': {'annotations': {'autoscaling.keda.sh/paused': 'true'}}})


def test_keda_pause_preserves_existing_annotations():
    item = {'kind': 'ScaledObject', 'metadata': {'uid': 'original', 'resourceVersion': '31',
                                               'annotations': {'owner': 'retained'}}}
    patch = q.patch_for(item)
    assert patch[-1]['value'] == {'owner': 'retained', 'autoscaling.keda.sh/paused': 'true',
                                 'autoscaling.keda.sh/paused-replicas': '0'}


def test_foundation_decommission_refuses_retagged_actors_and_foreign_state():
    values = {'account_id': ACCOUNT, 'environment': 'dev', 'org_id': ORG, 'workspace_id': WORKSPACE,
              'actor_role_names': {'registrar': 'original-registrar'}, 'provider_role_arn': 'original-provider',
              'expected_provider_role_id': 'AROAORIGINAL'}
    tags = {'ManagedBy': 'superplane-lifecycle-foundations', 'Environment': 'dev',
            'SuperplaneFoundationOrgId': ORG, 'SuperplaneFoundationWorkspaceId': WORKSPACE,
            'Lifecycle': 'retained-independent-owner', 'BootstrapActor': 'registrar'}
    state = {'resources': [{'mode': 'managed', 'type': 'aws_iam_role', 'name': 'actor',
                           'instances': [{'index_key': 'registrar', 'attributes': {'name': 'original-registrar', 'tags': tags}}]}]}
    t.validate_foundation_owner(values, state, shared)
    tags['SuperplaneFoundationWorkspaceId'] = 'another-workspace'
    with pytest.raises(ValueError, match='ownership tags differ'):
        t.validate_foundation_owner(values, state, shared)
    tags['SuperplaneFoundationWorkspaceId'] = WORKSPACE
    state['resources'][0]['name'] = 'shared_platform_actor'
    with pytest.raises(ValueError, match='unfamiliar address'):
        t.validate_foundation_owner(values, state, shared)
