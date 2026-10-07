"""Exercise cutover ordering, refusal, and recovery without touching a cluster."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'platform/scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = load('upgrade-workers')
policy = load('upgrade-plan-policy')
state = load('upgrade-state')
ACCOUNT = '123456789012'
REGION = 'us-east-1'
CLUSTER = f'arn:aws:eks:{REGION}:{ACCOUNT}:cluster/adp-dev-eks-cluster'
GATEWAY = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-gateway@sha256:' + 'a' * 64
WORKER = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-agent-runtime@sha256:' + 'b' * 64


@pytest.fixture
def proof(tmp_path):
    report = tmp_path / 'report.txt'
    report.write_text('Synthetic test fixture. This is not live qualification evidence.')
    value = dict(schema=1, account=ACCOUNT, region=REGION, environment='dev', source_sha='c' * 40,
                 gateway_image=GATEWAY, worker_image=WORKER, reviewed_by='fixture-reviewer', clusters=[CLUSTER],
                 checks={k: 'passed' for k in migration.CHECKS},
                 report={'file': 'report.txt', 'sha256': hashlib.sha256(report.read_bytes()).hexdigest()})
    path = tmp_path / 'qualification.json'
    path.write_text(json.dumps(value))
    return path, value


def qualified(path):
    with patch.object(migration, 'command', return_value='c' * 40), patch.dict(migration.os.environ, {}, clear=True):
        return migration.qualification(path, ACCOUNT, REGION, 'dev')


def test_qualification_binds_report_target_source_and_images(proof):
    path, original = proof
    assert qualified(path) == original
    for field, value in [('account', '999999999999'), ('source_sha', 'd' * 40),
                         ('gateway_image', GATEWAY.split('@')[0] + ':latest'),
                         ('worker_image', WORKER.replace(ACCOUNT, '999999999999')),
                         ('reviewed_by', ''), ('checks', {'coding': 'passed'})]:
        bad = dict(original, **{field: value})
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            qualified(path)
    path.write_text(json.dumps(original))
    path.with_name('report.txt').write_text('Changed report')
    with pytest.raises(ValueError, match='report is missing or changed'):
        qualified(path)


def test_selected_artifacts_cannot_differ_from_qualification(proof):
    path, _ = proof
    with patch.object(migration, 'command', return_value='c' * 40), \
            patch.dict(migration.os.environ, ADP_RELEASE_GATEWAY_IMAGE=GATEWAY.replace('a' * 64, 'e' * 64)):
        with pytest.raises(ValueError, match='selected release artifacts'):
            migration.qualification(path, ACCOUNT, REGION, 'dev')


@pytest.fixture
def cutover(tmp_path):
    value = dict(account=ACCOUNT, region=REGION, environment='dev', source_sha='c' * 40,
                 gateway_image=GATEWAY, worker_image=WORKER, clusters=[CLUSTER], phase='validated')
    migration.save(tmp_path, value)
    for name in ('platform', 'gateway', 'webhook-ingress'):
        (tmp_path / (name + '.tfvars.json')).write_text(json.dumps({'retained': 'keep'}))
    return tmp_path


def job(paused=False):
    return {'metadata': {'uid': 'original-uid', 'resourceVersion': '7',
                          'annotations': {'operator.example/setting': 'keep', **({migration.PAUSE: 'true'} if paused else {})}},
            'status': {'conditions': [{'type': 'Paused', 'status': 'True' if paused else 'False'}]}}


def test_pause_preserves_annotations_jobs_and_orders_activation(cutover):
    events = []
    def kube(*args, **kwargs):
        events.append(args[0])
        if args[0] == 'patch':
            operations = json.loads(args[-1])
            assert operations[0] == {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '7'}
            assert operations[1]['value']['operator.example/setting'] == 'keep'
            return {}
        return {'data': {'AGENT_AUTHORITY_ENABLED': 'false'}}
    with patch.object(migration, 'scaledjob', side_effect=[job(), job(True), job(True)]), \
            patch.object(migration, 'kube', side_effect=kube), \
            patch.object(migration.subprocess, 'run', side_effect=lambda *a, **kw: events.append('quiesce')), \
            patch.object(migration.time, 'monotonic', side_effect=[0, 1, 62]), patch.object(migration.time, 'sleep'), \
            patch.object(migration, 'check_source_isolation', side_effect=lambda _: events.append('isolation')), \
            patch.object(migration, 'check_queue', side_effect=lambda _: events.append('queue')), \
            patch.object(migration, 'check_drained', side_effect=lambda: events.append('drain')):
        migration.pause(cutover)
    assert events[:6] == ['isolation', 'queue', 'quiesce', 'patch', 'drain', 'queue']
    assert migration.read_record(cutover)['original_paused'] is False
    config = json.loads((cutover / 'webhook-ingress.tfvars.json').read_text())
    assert config['retained'] == 'keep' and config['agent_worker_admission_paused'] is True
    assert config['agent_authority_enabled'] is True and config['agent_image'] == WORKER
    assert json.loads((cutover / 'gateway.tfvars.json').read_text())['orchestration_agent_authority_enabled'] is False
    with patch.object(migration, 'scaledjob', return_value=job(True)):
        migration.tick(cutover)
    assert json.loads((cutover / 'gateway.tfvars.json').read_text())['orchestration_agent_authority_enabled'] is True


def test_pending_queue_refuses_without_pausing(cutover):
    with patch.object(migration, 'scaledjob', return_value=job()), \
            patch.object(migration, 'check_source_isolation'), \
            patch.object(migration, 'check_queue', side_effect=ValueError('queued work')), \
            patch.object(migration, 'kube') as kube:
        with patch.object(migration.subprocess, 'run') as run:
            with pytest.raises(ValueError, match='queued work'):
                migration.pause(cutover)
            run.assert_not_called()
        migration.fail_closed(cutover)
        kube.assert_not_called()
    assert json.loads((cutover / 'webhook-ingress.tfvars.json').read_text()) == {'retained': 'keep'}


def test_active_jobs_never_set_readiness_and_keep_pause(cutover):
    with patch.object(migration, 'scaledjob', side_effect=[job(), job(True)]), \
            patch.object(migration, 'check_source_isolation'), patch.object(migration, 'check_queue'), \
            patch.object(migration, 'kube'), patch.object(migration.time, 'monotonic', side_effect=[0, 601]), \
            patch.object(migration.subprocess, 'run'), \
            patch.object(migration, 'check_drained', side_effect=ValueError('active Jobs')):
        with pytest.raises(ValueError, match='admission remains paused'):
            migration.pause(cutover)
    assert migration.read_record(cutover)['pause_started'] is True
    assert json.loads((cutover / 'webhook-ingress.tfvars.json').read_text()) == {'retained': 'keep'}


def test_resume_preserves_original_pause_and_refuses_replaced_job(cutover):
    record = migration.read_record(cutover)
    record.update(original_paused=False, scaledjob_uid='original-uid', phase='failed_paused')
    migration.save(cutover, record)
    with patch.object(migration, 'scaledjob', return_value=job(True)), \
            patch.object(migration, 'check_source_isolation'), patch.object(migration, 'check_queue'), \
            patch.object(migration.time, 'monotonic', side_effect=[0, 1, 62]), patch.object(migration.time, 'sleep'), \
            patch.object(migration.subprocess, 'run'), \
            patch.object(migration, 'check_drained'), patch.object(migration, 'kube', return_value={'data': {}}):
        migration.pause(cutover)
    assert migration.read_record(cutover)['original_paused'] is False
    replaced = job(True)
    replaced['metadata']['uid'] = 'different-job'
    with patch.object(migration, 'scaledjob', return_value=replaced), patch.object(migration, 'kube') as kube:
        with pytest.raises(ValueError, match='replaced outside'):
            migration.pause(cutover)
        kube.assert_not_called()


def test_operator_pause_is_never_reopened(cutover):
    record = migration.read_record(cutover)
    record['original_paused'] = True
    migration.save(cutover, record)
    with patch.object(migration, 'verify'):
        with pytest.raises(ValueError, match='operator-paused'):
            migration.admit(cutover)
    assert json.loads((cutover / 'webhook-ingress.tfvars.json').read_text()) == {'retained': 'keep'}


def test_unfinished_or_unlabeled_jobs_and_legacy_pods_block_drain():
    for jobs, pods in [([{'metadata': {'name': 'unlabeled'}}], []),
                       ([], [{'spec': {'serviceAccountName': 'agent-scaledjob-sa'}, 'status': {'phase': 'Running'}}])]:
        with patch.object(migration, 'kube', side_effect=[{'items': jobs}, {'items': pods}]):
            with pytest.raises(ValueError):
                migration.check_drained()


def test_all_cluster_inventory_and_visible_grants_are_rechecked():
    record = dict(account=ACCOUNT, environment='dev', region=REGION, clusters=[CLUSTER])
    replies = [{'Regions': [{'RegionName': REGION}]}, {'clusters': ['adp-dev-eks-cluster']},
               {'cluster': {'arn': CLUSTER, 'accessConfig': {'authenticationMode': 'API'}}},
               {'accessEntries': [f'arn:aws:iam::{ACCOUNT}:role/adp-dev-agent-scaledjob-role']}]
    with patch.object(migration, 'aws', side_effect=replies):
        with pytest.raises(ValueError, match='EKS access entry'):
            migration.check_source_isolation(record)
    with patch.object(migration, 'aws', side_effect=[replies[0], {'clusters': ['adp-dev-eks-cluster', 'another-cluster']}]):
        with pytest.raises(ValueError, match='inventory differs'):
            migration.check_source_isolation(record)


def test_migration_plan_permission_is_narrow(cutover):
    record = migration.read_record(cutover)
    record['phase'] = 'drained'
    values = dict(environment='dev', aws_region=REGION, eks_cluster_name='adp-dev-eks-cluster',
                  gateway_namespace='adp-gateway', agent_image=WORKER, agent_worker_admission_paused=True,
                  agent_authority_worker_image_digests=['sha256:' + 'b' * 64])
    for key in ('agent_authority_prepared', 'agent_authority_enabled', 'agent_authority_runtime_ready',
                'agent_authority_legacy_workers_drained', 'agent_task_source_isolation_confirmed',
                'agent_legacy_worker_admin_retired'):
        values[key] = True
    change = {'address': 'terraform_data.worker_security_rollout', 'type': 'terraform_data',
              'change': {'actions': ['update'], 'before': {'input': {'active': False, 'paused': False}},
                         'after': {'input': {'active': True, 'paused': True}}}}
    plan = {'resource_changes': [change], 'variables': {k: {'value': v} for k, v in values.items()}}
    assert policy.evaluate(plan, 'webhook-ingress', ACCOUNT)['protected']
    assert not policy.evaluate(plan, 'webhook-ingress', ACCOUNT, record)['protected']
    for key in ('account', 'region', 'environment', 'phase', 'worker_image'):
        bad = dict(record, **{key: 'wrong'})
        assert policy.evaluate(plan, 'webhook-ingress', ACCOUNT, bad)['protected']
    for key in ('agent_authority_runtime_ready', 'agent_legacy_worker_admin_retired', 'agent_worker_admission_paused'):
        bad = copy.deepcopy(plan)
        bad['variables'][key]['value'] = False
        assert policy.evaluate(bad, 'webhook-ingress', ACCOUNT, record)['protected']
    bad = copy.deepcopy(plan)
    bad['resource_changes'][0]['change']['before']['input']['active'] = True
    bad['resource_changes'][0]['change']['after']['input']['active'] = False
    assert policy.evaluate(bad, 'webhook-ingress', ACCOUNT, record)['protected']


def test_security_configuration_survives_later_source_upgrades(monkeypatch):
    monkeypatch.delenv('ADP_PORTABLE_RELEASE_CONFIG', raising=False)
    saved = dict(agent_authority_enabled=True, agent_worker_admission_paused=False,
                 agent_legacy_worker_admin_retired=True, unrelated='operator-owned')
    retained = state.release_settings({'outputs': {'release_configuration': {'value': saved}}}, 'webhook-ingress')
    assert retained == {k: v for k, v in saved.items() if k != 'unrelated'}


def test_failure_after_admission_repauses_without_deleting_work(cutover):
    record = migration.read_record(cutover)
    record.update(phase='admitted', pause_started=True, scaledjob_uid='original-uid', original_paused=False)
    migration.save(cutover, record)
    with patch.object(migration, 'scaledjob', return_value=job()), patch.object(migration, 'kube') as kube, \
            patch.object(migration.subprocess, 'run') as run:
        migration.fail_closed(cutover)
    assert kube.call_args.args[:3] == ('patch', 'scaledjob', 'agent-scaledjob')
    assert json.loads(kube.call_args.args[-1])[-1]['value'][migration.PAUSE] == 'true'
    assert '--quiesce' in run.call_args.args[0]
    assert migration.read_record(cutover)['phase'] == 'failed_paused'
    assert json.loads((cutover / 'webhook-ingress.tfvars.json').read_text())['agent_worker_admission_paused'] is True


def test_saved_plan_keeps_tick_held_until_explicit_admission_completion(tmp_path):
    calls = tmp_path / 'calls'
    script = '''set -euo pipefail
ok() { :; }
fail() { exit 1; }
terraform() { printf '%s\\n' "$*" >> "$CALLS"; }
source "$1"
terraform_update_apply gateway-worker-authority unused.tfvars
terraform_update_apply gateway-final unused.tfvars
ADP_WORKER_MIGRATION_EVIDENCE= terraform_update_apply gateway-final unused.tfvars
'''
    env = dict(os.environ, ADP_WORKER_MIGRATION_EVIDENCE='/private/reviewed.json', CALLS=str(calls), TMPDIR=str(tmp_path))
    result = subprocess.run(['bash', '-c', script, 'test', str(ROOT / 'platform/scripts/terraform-update.sh')],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    commands = calls.read_text().splitlines()
    assert len(commands) == 3
    assert 'orchestration_tick_upgrade_hold=true' in commands[0]
    assert 'orchestration_tick_upgrade_hold=true' in commands[1]
    assert 'orchestration_tick_upgrade_hold=false' in commands[2]
