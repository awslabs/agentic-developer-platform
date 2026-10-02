"""Legacy and partially applied upgrade regressions; no AWS credentials required."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / 'platform/scripts'
spec = importlib.util.spec_from_file_location('legacy_preflight', SCRIPTS / 'upgrade-preflight.py')
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)
spec = importlib.util.spec_from_file_location('legacy_policy', SCRIPTS / 'upgrade-plan-policy.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
ACCOUNT = '123456789012'
ROLE = f'arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/region/Administrator'
CLUSTER = 'adp-dev-eks-cluster'
POLICY = 'arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy'
DIGEST = 'sha256:' + 'a' * 64


def resource(kind, name='main', module='module.ecr', **attributes):
    return {'mode': 'managed', 'type': kind, 'name': name, 'module': module,
            'instances': [{'attributes': attributes}]}


class OwnershipTests(unittest.TestCase):
    def test_old_install_without_singletons_does_not_adopt_account_settings(self):
        with patch.object(preflight, 'aws') as read:
            settings = preflight.check_settings({'resources': []}, 'us-east-1', read=read)
        self.assertEqual(settings, dict(manage_ecr_registry_scanning=False,
                                       manage_bedrock_invocation_logging=False,
                                       bedrock_invocation_logging_enabled=False))
        read.assert_not_called()

    def test_partial_logging_apply_keeps_supporting_resources_without_put_logging(self):
        state = {'resources': [resource('aws_kms_key', module='module.bedrock_invocation_logging[0]')]}
        settings = preflight.account_settings(state)
        self.assertTrue(settings['manage_bedrock_invocation_logging'])
        self.assertFalse(settings['bedrock_invocation_logging_enabled'])

    def test_ecr_organization_conflict_refuses_instead_of_downgrading(self):
        state = {'resources': [resource('aws_ecr_registry_scanning_configuration')]}
        for scan_type in ('BASIC', 'ENHANCED'):
            with patch.object(preflight, 'aws', return_value={'scanningConfiguration': {'scanType': scan_type}}) as read:
                if scan_type == 'BASIC':
                    self.assertTrue(preflight.check_settings(state, 'us-east-1', read)['manage_ecr_registry_scanning'])
                else:
                    with self.assertRaisesRegex(ValueError, 'Relinquish'):
                        preflight.check_settings(state, 'us-east-1', read)

    def test_external_bedrock_destination_is_never_overwritten(self):
        state = {'resources': [resource('aws_bedrock_model_invocation_logging_configuration',
                                       module='module.bedrock_invocation_logging[0]', logging_config=[{
                                           'cloudwatch_config': [{'log_group_name': 'adp-logs', 'role_arn': ROLE}],
                                           's3_config': [{'bucket_name': 'adp-logs', 'key_prefix': 'logs'}]}])]}
        live = {'loggingConfig': {'cloudWatchConfig': {'logGroupName': 'adp-logs', 'roleArn': ROLE},
                                  's3Config': {'bucketName': 'adp-logs', 'keyPrefix': 'logs'}}}
        with patch.object(preflight, 'aws', return_value=live) as read:
            self.assertTrue(preflight.check_settings(state, 'us-east-1', read)['bedrock_invocation_logging_enabled'])
            live['loggingConfig']['s3Config']['bucketName'] = 'organization-logs'
            with self.assertRaisesRegex(ValueError, 'destinations differ'):
                preflight.check_settings(state, 'us-east-1', read)

    def test_access_denied_is_not_treated_as_no_ownership(self):
        state = {'resources': [resource('aws_ecr_registry_scanning_configuration')]}
        with patch.object(preflight, 'aws', side_effect=subprocess.CalledProcessError(254, ['aws'])) as read:
            with self.assertRaises(subprocess.CalledProcessError):
                preflight.check_settings(state, 'us-east-1', read)

    def test_operator_entry_and_policy_conflicts_require_explicit_import(self):
        identity = {'Arn': f'arn:aws:sts::{ACCOUNT}:assumed-role/Administrator/session'}
        entries = {'accessEntries': [ROLE]}
        policies = {'associatedAccessPolicies': [{'policyArn': POLICY, 'accessScope': {'type': 'cluster'}}]}
        entry = resource('aws_eks_access_entry', 'admins', 'module.eks', principal_arn=ROLE)
        association = resource('aws_eks_access_policy_association', 'admins', 'module.eks',
                               principal_arn=ROLE, policy_arn=POLICY)
        for rows, message in (([], 'access exists outside'), ([entry], 'policy exists outside'),
                              ([entry, association], None)):
            with patch.object(preflight, 'aws', side_effect=[{'Role': {'Arn': ROLE}}, entries, policies]) as read:
                if message:
                    with self.assertRaisesRegex(ValueError, message):
                        preflight.check_operator({'resources': rows}, identity, 'us-east-1', CLUSTER, read)
                else:
                    preflight.check_operator({'resources': rows}, identity, 'us-east-1', CLUSTER, read)


class EngineBootstrapTests(unittest.TestCase):
    def test_existing_engine_is_paused_and_drained_before_changes(self):
        with patch.object(preflight.engine, 'current_image_digest', return_value=DIGEST), \
                patch.object(preflight.engine, 'command', side_effect=['{"Timeout": 60}', '']) as command, \
                patch.object(preflight.engine.time, 'sleep') as sleep:
            result = preflight.engine.quiesce(account=ACCOUNT, region='us-east-1', environment='dev')
        self.assertEqual(result['status'], 'quiesced')
        self.assertEqual(command.call_args_list[1].args[0][:3], ['aws', 'events', 'disable-rule'])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [30, 30, 5])

    def test_failed_pause_cannot_be_reported_as_quiesced(self):
        with patch.object(preflight.engine, 'current_image_digest', return_value=DIGEST), \
                patch.object(preflight.engine, 'command', side_effect=[
                    '{"Timeout": 60}', subprocess.CalledProcessError(254, ['aws'])]), \
                patch.object(preflight.engine.time, 'sleep') as sleep:
            with self.assertRaises(subprocess.CalledProcessError):
                preflight.engine.quiesce(account=ACCOUNT, region='us-east-1', environment='dev')
        sleep.assert_not_called()

    def test_only_explicit_not_found_can_bootstrap(self):
        for error, allowed in (('(ResourceNotFoundException)', True), ('(AccessDeniedException)', False),
                               ('timeout', False), ('not found', False)):
            with self.subTest(error=error), patch.object(preflight.engine, 'command', side_effect=[
                    json.dumps({'Account': ACCOUNT}), subprocess.CalledProcessError(254, ['aws'], stderr=error)]):
                if allowed:
                    self.assertIsNone(preflight.engine.current_image_digest(account=ACCOUNT, region='us-east-1',
                                                                            environment='dev', allow_missing=True))
                else:
                    with self.assertRaises(subprocess.CalledProcessError):
                        preflight.engine.current_image_digest(account=ACCOUNT, region='us-east-1',
                                                              environment='dev', allow_missing=True)

    def prepare(self, tmp, state, digest, retained=None):
        root = Path(tmp)
        (root / 'platform.tfvars.json').write_text('{}')
        (root / 'gateway.tfvars.json').write_text(json.dumps(retained or {}))

        def read(region, service, operation, *args):
            if service == 'sts':
                return {'Account': ACCOUNT, 'Arn': ROLE}
            if service == 's3api':
                Path(args[-1]).write_text(json.dumps(state if 'gateway' in args[-1] else {'resources': []}))
                return {}
            if service == 'events':
                return {'State': 'DISABLED'}
            self.fail((service, operation))

        with patch.object(preflight, 'aws', side_effect=read), patch.object(preflight, 'check_operator'), \
                patch.object(preflight.engine, 'current_image_digest', return_value=digest) as engine:
            preflight.prepare(tmp, ACCOUNT, 'us-east-1', 'dev')
        return json.loads((root / 'gateway.tfvars.json').read_text()), engine

    def test_old_install_gets_engine_and_desired_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings, engine = self.prepare(tmp, {'resources': []}, None)
            self.assertTrue(settings['orchestration_tick_schedule_enabled'])
            self.assertTrue(engine.call_args.kwargs['allow_missing'])
            self.assertEqual(json.loads((Path(tmp) / 'engine-before.json').read_text()),
                             {'missing': True, 'desired_schedule_enabled': True})

    def test_untracked_live_engine_requires_import(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, 'outside gateway state'):
            self.prepare(tmp, {'resources': []}, DIGEST)

    def test_partial_apply_uses_current_state_and_retains_original_schedule_intent(self):
        state = {'resources': [resource('aws_lambda_function', 'tick', 'module.orchestration_tick[0]'),
                               resource('aws_cloudwatch_event_rule', 'tick', 'module.orchestration_tick[0]', state='DISABLED')]}
        for retained, expected in (({}, False), ({'orchestration_tick_schedule_enabled': True}, True)):
            with self.subTest(retained=retained), tempfile.TemporaryDirectory() as tmp:
                # Original state lacks the engine, as when resuming after creation.
                (Path(tmp) / 'gateway-before.tfstate').write_text('{"resources": []}')
                if retained:
                    (Path(tmp) / 'engine-before.json').write_text(json.dumps({
                        'missing': True, 'desired_schedule_enabled': retained['orchestration_tick_schedule_enabled']}))
                settings, engine = self.prepare(tmp, state, DIGEST, retained)
                self.assertEqual(settings['orchestration_tick_schedule_enabled'], expected)
                self.assertFalse(engine.call_args.kwargs['allow_missing'])
                self.assertEqual(json.loads((Path(tmp) / 'engine-before.json').read_text()),
                                 {'missing': False, 'desired_schedule_enabled': expected})

    def test_new_run_recovers_interrupted_hold_but_honors_operator_emergency_stop(self):
        state = {'resources': [resource('aws_lambda_function', 'tick', 'module.orchestration_tick[0]')],
                 'outputs': {'release_configuration': {'value': {'orchestration_tick_schedule_enabled': True}}}}
        for held in (True, False):
            state['outputs']['orchestration_tick_upgrade_hold'] = {'value': held}
            with self.subTest(held=held), tempfile.TemporaryDirectory() as tmp:
                settings, _ = self.prepare(tmp, state, DIGEST)
                self.assertEqual(settings['orchestration_tick_schedule_enabled'], held)


class UpgradeProtectionTests(unittest.TestCase):
    def test_saved_plan_gate_blocks_cluster_replacement_even_when_confirmed(self):
        row = {'address': 'module.eks.aws_eks_cluster.main', 'type': 'aws_eks_cluster',
               'change': {'before': {'id': CLUSTER}, 'after': {'id': CLUSTER}, 'actions': ['delete', 'create']}}
        result, calls = self.run_gate('platform', [row])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('protected infrastructure', result.stderr)
        self.assertNotIn('applied', calls)

    def run_gate(self, module, changes):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'fixture.json').write_text(json.dumps({'resource_changes': changes}))
            shell = '''set -euo pipefail
fail() { echo "$*" >&2; exit 1; }
ok() { :; }
warn() { :; }
terraform() {
  case "$1" in
    plan) echo "$*" >> "$CALLS"; return 2 ;;
    show) cat "$FIXTURE" ;;
    apply) echo applied >> "$CALLS" ;;
    *) exit 90 ;;
  esac
}
source "$HELPER"
terraform_update_apply "$MODULE" config.tfvars -var orchestration_tick_upgrade_hold=false
'''
            env = dict(os.environ, HELPER=str(SCRIPTS / 'terraform-update.sh'), MODULE=module,
                       ACCOUNT_ID=ACCOUNT, CONFIRM_DESTRUCTIVE='true', UPGRADE_RUN_DIR=tmp,
                       CALLS=str(path / 'calls'), FIXTURE=str(path / 'fixture.json'))
            env.pop('UPGRADE_CHECK_ONLY', None)
            result = subprocess.run(['/bin/bash', '-c', shell], env=env, text=True, capture_output=True)
            return result, (path / 'calls').read_text()

    def test_all_gateway_passes_hold_schedule_until_finalization(self):
        for module in ('gateway', 'gateway-alb-wire', 'gateway-worker-authority', 'gateway-final'):
            with self.subTest(module=module):
                result, calls = self.run_gate(module, [])
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = calls.splitlines()[0]
                expected = 'false' if module == 'gateway-final' else 'true'
                # Last -var wins even if a caller supplied a premature activation.
                self.assertEqual(plan.split('orchestration_tick_upgrade_hold=')[-1].split()[0], expected)

    def test_durable_resources_and_singletons_cannot_be_deleted_or_forgotten(self):
        for kind in ('aws_eks_cluster', 'aws_db_instance', 'aws_rds_cluster', 'aws_ecr_repository',
                     'aws_iam_openid_connect_provider', 'aws_ecr_registry_scanning_configuration',
                     'aws_bedrock_model_invocation_logging_configuration'):
            for actions in (['delete'], ['delete', 'create'], ['create', 'delete'], ['forget']):
                with self.subTest(kind=kind, actions=actions):
                    row = {'address': 'module.example.' + kind + '.main', 'type': kind,
                           'change': {'before': {'id': 'existing'}, 'after': None, 'actions': actions}}
                    self.assertEqual(policy.evaluate({'resource_changes': [row]}, 'platform', ACCOUNT)['protected'], [row['address']])
            row['change'].update(actions=['update'], after={'id': 'existing'})
            self.assertFalse(policy.evaluate({'resource_changes': [row]}, 'platform', ACCOUNT)['protected'])

    def test_missing_flock_is_actionable_before_cloud_calls(self):
        with tempfile.TemporaryDirectory() as empty:
            result = subprocess.run(['/bin/bash', '-c', 'source "$1"; echo unexpected', 'test',
                                     str(SCRIPTS / 'deploy-prerequisites.sh')],
                                    env=dict(os.environ, PATH=empty), text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('brew install flock', result.stderr)
            self.assertNotIn('unexpected', result.stdout)


class LegacyWorkflowTests(unittest.TestCase):
    def test_missing_engine_builds_before_plan_and_never_hides_errors(self):
        source = (SCRIPTS / 'deploy-all.sh').read_text()
        start = source.index('if deploy_phase_begin gateway-infra; then')
        block = source[start:source.index('\nrefresh_credentials', start)]
        shell = '''set -euo pipefail
deploy_phase_begin() { return 0; }
deploy_phase_complete() { :; }
step() { :; }
fail() { echo "$*" >&2; exit 1; }
python3() {
  if [[ " $* " = *" --current-image-digest "* ]]; then
    echo "$DISCOVERED"
  elif [ "$1" = - ]; then
    command python3 "$@"
  else
    echo quiesce >> "$CALLS"
  fi
}
prepare_gateway_image() {
  echo build >> "$CALLS"
  [ "$BUILD_FAIL" = false ] || return 1
  GATEWAY_IMAGE="example@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
terraform() { echo init >> "$CALLS"; }
bash() { :; }
gateway_alb_vars() { GATEWAY_ALB_ARGS=(); }
terraform_update_apply() { echo "plan $*" >> "$CALLS"; }
'''
        for discovery, original_missing, build_fail, expected in (
                ('MISSING', True, False, True), ('MISSING', False, False, False),
                ('MISSING', True, True, False), (DIGEST, False, False, True)):
            with self.subTest(discovery=discovery, original_missing=original_missing, build_fail=build_fail), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                (path / 'engine-before.json').write_text(json.dumps({'missing': original_missing}))
                env = dict(os.environ, ROOT_DIR=str(ROOT), SCRIPT_DIR=str(SCRIPTS), UPDATE_MODE='true',
                           DEPLOY_GATEWAY='true', ACCOUNT_ID=ACCOUNT, ENVIRONMENT='dev', AWS_REGION='us-east-1',
                           GATEWAY_UPDATE_VAR_FILE='gateway.json', UPGRADE_RUN_DIR=tmp,
                           CALLS=str(path / 'calls'), DISCOVERED=discovery, BUILD_FAIL=str(build_fail).lower())
                result = subprocess.run(['/bin/bash', '-c', shell + block], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, expected, result.stderr)
                calls = (path / 'calls').read_text() if (path / 'calls').exists() else ''
                if expected:
                    self.assertIn('orchestration_tick_image_digest=' + DIGEST, calls)
                    self.assertEqual('build' in calls, discovery == 'MISSING')
                    if discovery == 'MISSING':
                        self.assertLess(calls.index('build'), calls.index('plan'))
                else:
                    self.assertNotIn('plan', calls)
                    self.assertNotIn('quiesce', calls)


if __name__ == '__main__':
    unittest.main()
