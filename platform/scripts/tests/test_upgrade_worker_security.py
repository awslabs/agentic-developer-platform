"""Security migrations must not become implicit worker outages during upgrades."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / 'platform/scripts'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = load('worker_preflight', SCRIPTS / 'upgrade-preflight.py')
policy = load('worker_policy', SCRIPTS / 'upgrade-plan-policy.py')
sys.path.insert(0, str(SCRIPTS / 'release'))
import acceptance

ACCOUNT = '123456789012'


def state(active=False, paused=False, retired=False, qualified=False):
    return {'outputs': {
        'worker_security_rollout': {'value': {'active': active, 'admission_paused': paused,
                                             'legacy_admin_retired': retired}},
        'release_configuration': {'value': {key: qualified for key in (
            'agent_task_source_isolation_confirmed', 'agent_authority_runtime_ready',
            'agent_authority_legacy_workers_drained')}},
    }}


def change(address, kind, before, after, actions):
    return {'address': address, 'type': kind,
            'change': {'before': before, 'after': after, 'actions': actions}}


def installed_legacy():
    arn = f'arn:aws:iam::{ACCOUNT}:role/adp-dev-agent-scaledjob-role'
    installed = state()
    installed['outputs']['worker_security_rollout']['value'].update(
        service_account='agent-scaledjob-sa', worker_role_arn=arn)
    installed['outputs']['release_configuration']['value'].update(
        agent_authority_enabled=False, agent_worker_admission_paused=False,
        agent_legacy_worker_admin_retired=False)
    installed['resources'] = [
        {'type': 'aws_iam_role', 'name': 'agent_scaledjob', 'mode': 'managed',
         'instances': [{'attributes': {'arn': arn, 'permissions_boundary': ''}}]},
        {'type': 'kubernetes_service_account', 'name': 'agent_scaledjob_sa', 'mode': 'managed',
         'instances': [{'attributes': {'metadata': [{'name': 'agent-scaledjob-sa', 'namespace': 'adp-agents',
             'annotations': {'eks.amazonaws.com/role-arn': arn}}]}}]},
    ]
    return installed


class WorkerPreflightTests(unittest.TestCase):
    def test_older_state_requires_recorded_gateway_flags_when_inputs_are_absent(self):
        installed = installed_legacy()
        del installed['outputs']['release_configuration']
        with self.assertRaises(ValueError):
            preflight.check_worker_security(installed, release=True)
        installed['resources'].append({'type': 'kubernetes_config_map', 'mode': 'managed', 'name': 'worker_gateway',
            'instances': [{'attributes': {'data': {'AGENT_AUTHORITY_ENABLED': 'false',
                                                  'AGENT_TASK_SOURCE_ISOLATION_CONFIRMED': 'false'}}}]})
        preflight.check_worker_security(installed, release=True)
        installed['resources'][-1]['instances'][0]['attributes']['data']['AGENT_AUTHORITY_ENABLED'] = 'true'
        with self.assertRaises(ValueError):
            preflight.check_worker_security(installed, release=True)

    def test_serving_legacy_upgrade_requires_matching_installed_identity(self):
        installed = installed_legacy()
        preflight.check_worker_security(installed, release=True)
        for mutate in (
            lambda s: s.pop('resources'),
            lambda s: s['resources'][0]['instances'][0]['attributes'].update(permissions_boundary='existing-boundary'),
            lambda s: s['outputs']['worker_security_rollout']['value'].update(active=True),
            lambda s: s['outputs']['worker_security_rollout']['value'].update(admission_paused=True),
            lambda s: s['outputs']['worker_security_rollout']['value'].update(service_account='protected-worker'),
            lambda s: s['outputs']['release_configuration']['value'].update(agent_task_source_isolation_confirmed=True),
        ):
            bad = copy.deepcopy(installed)
            mutate(bad)
            with self.assertRaises(ValueError):
                preflight.check_worker_security(bad, release=True)

    def test_live_operator_pause_and_changed_identity_refuse_before_apply(self):
        settings = preflight.state_tools.legacy_worker_settings(installed_legacy())
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, 'webhook-ingress.tfvars.json').write_text(json.dumps(settings))
            job = {'metadata': {}, 'spec': {'jobTargetRef': {'template': {'spec': {'serviceAccountName': 'agent-scaledjob-sa'}}}}}
            sa = {'metadata': {'annotations': {'eks.amazonaws.com/role-arn': settings['agent_legacy_upgrade_role_arn']}}}
            config = {'data': {'AGENT_AUTHORITY_ENABLED': 'false'}}
            with patch.object(preflight.subprocess, 'check_output', side_effect=map(json.dumps, [job, sa, config])):
                preflight.verify_live_workers(temporary)
            for mutated in (
                dict(job, metadata={'annotations': {'autoscaling.keda.sh/paused': 'false'}}),
                dict(job, spec={'jobTargetRef': {'template': {'spec': {'serviceAccountName': 'protected-worker'}}}}),
            ):
                with patch.object(preflight.subprocess, 'check_output', return_value=json.dumps(mutated)), self.assertRaises(ValueError):
                    preflight.verify_live_workers(temporary)
    def test_legacy_and_unknown_deployments_require_migration(self):
        for installed in (state(), {}, state(active=True), state(active=True, retired=True)):
            with self.subTest(installed=installed), self.assertRaisesRegex(ValueError, 'migration required'):
                preflight.check_worker_security(installed)

    def test_paused_maintenance_does_not_qualify_as_release(self):
        preflight.check_worker_security(state(paused=True))
        with self.assertRaisesRegex(ValueError, 'migration required'):
            preflight.check_worker_security(state(paused=True), release=True)
        with self.assertRaisesRegex(ValueError, 'admission enabled'):
            preflight.check_worker_security(state(True, True, True, True), release=True)

    def test_migrated_state_requires_every_assertion_and_explicit_admission(self):
        installed = state(True, False, True, True)
        preflight.check_worker_security(installed, release=True)
        for key in installed['outputs']['release_configuration']['value']:
            bad = copy.deepcopy(installed)
            bad['outputs']['release_configuration']['value'][key] = 'true'
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'migration required'):
                preflight.check_worker_security(bad, release=True)
        del installed['outputs']['worker_security_rollout']['value']['admission_paused']
        with self.assertRaisesRegex(ValueError, 'admission enabled'):
            preflight.check_worker_security(installed, release=True)

    def test_resume_reloads_current_state_and_refuses_before_other_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            # Even a qualified original snapshot cannot hide a current regression.
            (directory / 'webhook-ingress-before.tfstate').write_text(json.dumps(state(True, False, True, True)))
            calls = []

            def read(region, service, operation, *args):
                calls.append((service, operation))
                if service == 'sts':
                    return {'Account': ACCOUNT}
                self.assertEqual(args[3], 'dev/modules/webhook-ingress/terraform.tfstate')
                Path(args[-1]).write_text(json.dumps(state()))
                return {}

            with patch.object(preflight, 'aws', side_effect=read), self.assertRaisesRegex(ValueError, 'migration required'):
                preflight.prepare(directory, ACCOUNT, 'us-east-1', 'dev')
            self.assertEqual(calls, [('sts', 'get-caller-identity'), ('s3api', 'get-object')])
            self.assertFalse((directory / 'engine-before.json').exists())


class WorkerPlanTests(unittest.TestCase):
    def test_legacy_retention_cannot_create_identity_or_remove_boundary(self):
        plan = self.mirror_plan()
        settings = preflight.state_tools.legacy_worker_settings(installed_legacy())
        plan['variables'].update({k: {'value': v} for k, v in settings.items()})
        role = {'arn': settings['agent_legacy_upgrade_role_arn'], 'permissions_boundary': None}
        rollout = {'input': {'active': False, 'paused': False}}
        plan['resource_changes'] += [
            change('aws_iam_role.agent_scaledjob', 'aws_iam_role', role, role, ['no-op']),
            change('terraform_data.worker_security_rollout', 'terraform_data', rollout, rollout, ['no-op']),
        ]
        self.assertFalse(self.evaluate(plan)['blocked'])
        self.assertFalse(self.evaluate(plan)['protected'])
        for mutate in (
            lambda p: p['resource_changes'][1]['change'].update(before=None, actions=['create']),
            lambda p: p['resource_changes'][1]['change'].update(before=dict(role, permissions_boundary='protected-boundary')),
            lambda p: p['resource_changes'][2]['change'].update(before={'input': {'active': True, 'paused': False}}),
            lambda p: p['variables']['agent_legacy_upgrade_role_arn'].update(value='foreign-role'),
        ):
            bad = copy.deepcopy(plan)
            mutate(bad)
            self.assertTrue(self.evaluate(bad)['protected'])
    def mirror_plan(self):
        name = '/adp/dev/gateway/internal-api-key'
        row = change('aws_ssm_parameter.gateway_internal_api_key[0]', 'aws_ssm_parameter', {
            'id': name, 'name': name, 'arn': f'arn:aws:ssm:us-east-1:{ACCOUNT}:parameter{name}',
            'type': 'SecureString', 'value': 'never-print-this',
            'tags': {'Purpose': 'adversarial-e2e', 'Source': 'secrets-manager-mirror',
                     'Issue': '3377', 'Component': 'credential-binding'},
        }, None, ['delete'])
        return {'resource_changes': [row], 'variables': {k: {'value': v} for k, v in {
            'environment': 'dev', 'aws_region': 'us-east-1', 'agent_worker_admission_paused': True,
        }.items()}}

    def evaluate(self, plan, module='webhook-ingress', account=ACCOUNT):
        return policy.evaluate(plan, module, account)

    def test_only_exact_retired_mirror_is_allowed(self):
        plan = self.mirror_plan()
        self.assertEqual(self.evaluate(plan)['routine'], [plan['resource_changes'][0]['address']])
        for field, value in (('id', 'other'), ('name', 'other'), ('arn', 'foreign'),
                             ('type', 'String'), ('tags', {})):
            bad = copy.deepcopy(plan)
            bad['resource_changes'][0]['change']['before'][field] = value
            with self.subTest(field=field):
                self.assertTrue(self.evaluate(bad)['blocked'])
        for module, account in (('gateway', ACCOUNT), ('webhook-ingress', '999999999999'),
                                ('webhook-ingress', '.*')):
            self.assertTrue(self.evaluate(plan, module, account)['blocked'])
        for mutate in (
            lambda p: p['variables']['agent_worker_admission_paused'].update(value=False),
            lambda p: p['variables']['environment'].update(value='prod'),
            lambda p: p['resource_changes'][0].update(address='aws_ssm_parameter.other[0]'),
            lambda p: p['resource_changes'][0]['change'].update(actions=['delete', 'create'], after={}),
            lambda p: p['resource_changes'][0]['change'].update(actions=['forget']),
        ):
            bad = copy.deepcopy(plan)
            mutate(bad)
            self.assertTrue(self.evaluate(bad)['blocked'])

    def test_mirror_retirement_with_protected_admission_needs_all_assertions(self):
        plan = self.mirror_plan()
        plan['variables']['agent_worker_admission_paused']['value'] = False
        keys = ('agent_authority_enabled', 'agent_authority_runtime_ready',
                'agent_authority_legacy_workers_drained', 'agent_legacy_worker_admin_retired',
                'agent_task_source_isolation_confirmed')
        for key in keys:
            plan['variables'][key] = {'value': True}
        self.assertFalse(self.evaluate(plan)['blocked'])
        for key in keys:
            bad = copy.deepcopy(plan)
            del bad['variables'][key]
            self.assertTrue(self.evaluate(bad)['blocked'])

    def test_mirror_exception_does_not_allow_source_secret_or_unrelated_deletion(self):
        plan = self.mirror_plan()
        source = change('aws_secretsmanager_secret.gateway_internal_api_key', 'aws_secretsmanager_secret',
                        {'name': 'adp/dev/gateway/internal-api-key'}, None, ['delete'])
        unrelated = change('aws_ssm_parameter.other', 'aws_ssm_parameter', {'name': '/other'}, None, ['delete'])
        plan['resource_changes'].extend([source, unrelated])
        result = self.evaluate(plan)
        self.assertEqual(result['blocked'], [source['address'], unrelated['address']])
        self.assertEqual(result['protected'], [source['address']])

    def test_script_only_marker_migration_is_exact(self):
        old_hash, new_hash = policy.WORKER_ROLLOUT_IDENTITY_MIGRATION
        old = {'configuration': 'a' * 64, 'marker_version': 'disabled', 'rollout_script': old_hash}
        row = change('terraform_data.worker_gateway_rollout[0]', 'terraform_data',
                     {'triggers_replace': old}, {'triggers_replace': dict(old, rollout_script=new_hash)},
                     ['create', 'delete'])
        row['action_reason'] = 'replace_because_cannot_update'
        plan = {'resource_changes': [row], 'variables': {k: {'value': v} for k, v in {
            'agent_authority_enabled': False, 'environment': 'dev', 'aws_region': 'us-east-1',
            'gateway_namespace': 'adp-gateway', 'eks_cluster_name': 'adp-dev-eks-cluster',
        }.items()}}
        self.assertFalse(self.evaluate(plan)['blocked'])
        for mutate in (
            lambda p: p['resource_changes'][0]['change']['after']['triggers_replace'].update(rollout_script='f' * 64),
            lambda p: p['resource_changes'][0]['change']['before']['triggers_replace'].update(rollout_script='f' * 64),
            lambda p: p['variables']['agent_authority_enabled'].update(value=True),
            lambda p: p['variables']['eks_cluster_name'].update(value='other'),
            lambda p: p['resource_changes'][0]['change'].update(actions=['delete', 'create']),
            lambda p: p['resource_changes'][0].update(action_reason='replace_by_request'),
        ):
            bad = copy.deepcopy(plan)
            mutate(bad)
            self.assertTrue(self.evaluate(bad)['blocked'])

    def test_worker_pause_and_identity_downgrade_are_protected_updates(self):
        for before, after in (({'paused': False}, {'paused': True}),
                              ({'paused': False}, {}), ({'active': True}, {'active': False})):
            row = change('terraform_data.worker_security_rollout', 'terraform_data',
                         {'input': before}, {'input': after}, ['update'])
            self.assertEqual(self.evaluate({'resource_changes': [row]})['protected'], [row['address']])


class AdmissionAcceptanceTests(unittest.TestCase):
    def test_paused_annotation_cannot_pass_even_when_keda_ready(self):
        for key in ('autoscaling.keda.sh/paused', 'autoscaling.keda.sh/paused-replicas'):
            for value in ('true', 'false', '0'):
                scaled = {'metadata': {'annotations': {key: value}},
                          'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, 'paused'):
                    acceptance.check_worker_admission(scaled, 'worker')
        with self.assertRaisesRegex(ValueError, 'paused'):
            acceptance.check_worker_admission({'status': {'conditions': [{'type': 'Paused', 'status': 'True'}]}}, 'worker')
        acceptance.check_worker_admission({'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}, 'worker')


if __name__ == '__main__':
    unittest.main()
