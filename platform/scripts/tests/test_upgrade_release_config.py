"""Portable release inputs retain target state and exclude platform rollout data."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'platform/scripts' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


config = load('release_config', 'prepare-release-config.py')
state = load('release_state', 'upgrade-state.py')


class ConfigTests(unittest.TestCase):
    def test_real_bundled_dev_settings_are_replaced_and_pass_foreign_account_gate(self):
        for account, environment, region in (('925091290508', 'dev', 'us-east-1'),
                                              ('111122223333', 'staging', 'eu-west-1')):
            with self.subTest(account=account), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / 'source'
                shutil.copytree(ROOT / 'config/release-defaults', root / 'config/release-defaults')
                shutil.copytree(ROOT / 'environments/dev', root / 'environments' / environment)
                original = (root / 'environments' / environment / 'modules/gateway.tfvars').read_text()
                self.assertIn('879318057152', original)
                config.prepare(root, environment, region)
                for name in ('platform', 'gateway', 'webhook-ingress'):
                    path = root / 'environments' / environment / ('platform.tfvars' if name == 'platform' else f'modules/{name}.tfvars')
                    result = subprocess.run(['bash', '-c', 'source "$1"; terraform_update_var_file "$2" "" "$3"',
                                             'test', str(ROOT / 'platform/scripts/terraform-update.sh'), str(path), account],
                                            text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    content = path.read_text()
                    for forbidden in ('879318057152', 'aws-e/adp', '59o2rakc50', 'qualification_id', 'worker_image_digests', 'retired = true'):
                        self.assertNotIn(forbidden, content)
                    self.assertIn(f'aws_region = "{region}"', content)
                self.assertEqual((Path(temp) / 'original-environment-config/gateway.tfvars').read_text(), original)

    def test_invalid_target_cannot_escape_environment_directory(self):
        for env in ('../dev', '/tmp', ''):
            with self.assertRaises(ValueError):
                config.prepare(ROOT, env, 'us-east-1')

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='true')
    def test_account_configuration_retains_qualified_settings(self):
        saved = {'task_api_flags': {'admission': True}, 'task_api_artifact_bucket_name': 'customer-artifacts',
                 'task_api_runtime_bindings': {'qualification_id': 'customer-proof', 'queue_url': 'customer-queue'}}
        self.assertEqual(state.release_settings({'outputs': {'release_configuration': {'value': saved}}}, 'gateway'), saved)
        with self.assertRaises(ValueError):
            state.release_settings({'outputs': {'release_configuration': {'value': {'unexpected': 'value'}}}}, 'gateway')

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='true')
    def test_legacy_active_authority_never_silently_reverts_to_portable_defaults(self):
        active = {'resources': [{'mode': 'managed', 'type': 'aws_lambda_function', 'name': 'github_webhook',
                                 'instances': [{'attributes': {'environment': [{'variables': {'AGENT_AUTHORITY_ENABLED': 'true'}}]}}]}]}
        with self.assertRaisesRegex(ValueError, 'original target-specific tfvars'):
            state.release_settings(active, 'webhook-ingress')

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='true')
    def test_legacy_engine_target_is_not_inferred_from_tick_state(self):
        def resource(kind, name, attrs):
            return {'mode': 'managed', 'type': kind, 'name': name, 'instances': [{'attributes': attrs}]}
        fixture = {'resources': [resource('aws_lambda_function', 'tick', {'environment': [{'variables': {
            'FEATURE_ORCHESTRATION_ENGINE_ENABLED': 'true', 'BG_ORCH_DISPATCH_REPO': 'customer/project'}}]}),
            resource('aws_db_instance', 'db', {'instance_class': 'db.r6g.large', 'allocated_storage': 100})]}
        self.assertEqual(state.release_settings(fixture, 'gateway'), {'orchestration_engine_enabled': True,
            'rds_instance_class': 'db.r6g.large', 'rds_allocated_storage': 100})

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='true')
    def test_upgrade_drops_only_retired_repository_from_saved_configuration(self):
        saved = {'orchestration_dispatch_repo': 'customer/project', 'orchestration_engine_enabled': True,
                 'orchestration_tick_schedule_enabled': True}
        fixture = {'outputs': {'release_configuration': {'value': saved}}}
        self.assertEqual(state.release_settings(fixture, 'gateway'),
                         {'orchestration_engine_enabled': True, 'orchestration_tick_schedule_enabled': True})
        self.assertEqual(saved['orchestration_dispatch_repo'], 'customer/project')
        fixture['outputs']['release_configuration']['value']['unexpected'] = 'unsafe'
        with self.assertRaisesRegex(ValueError, 'Invalid retained gateway release configuration'):
            state.release_settings(fixture, 'gateway')

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='true')
    def test_scn_03_fresh_and_preserved_upgrade_have_no_global_repository_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            shutil.copytree(ROOT / 'config/release-defaults', root / 'config/release-defaults')
            shutil.copytree(ROOT / 'environments/dev', root / 'environments/dev')
            config.prepare(root, 'dev', 'us-east-1')
            fresh = (root / 'environments/dev/modules/gateway.tfvars').read_text()
            self.assertNotIn('orchestration_dispatch_repo', fresh)
            self.assertIn('enable_api_gateway', fresh)

        saved = {'orchestration_dispatch_repo': 'example/retired',
                 'orchestration_engine_enabled': True, 'orchestration_tick_schedule_enabled': True}
        fixture = {'outputs': {'release_configuration': {'value': saved}}}
        upgraded = state.release_settings(fixture, 'gateway')
        self.assertEqual(upgraded, {'orchestration_engine_enabled': True, 'orchestration_tick_schedule_enabled': True})
        self.assertNotIn('orchestration_dispatch_repo', upgraded)
        self.assertEqual(saved['orchestration_dispatch_repo'], 'example/retired')

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='true')
    def test_legacy_ingestion_permission_retains_exact_customer_queue(self):
        queue = 'arn:aws:sqs:us-east-1:925091290508:customer-knowledge.fifo'
        policy = {'Statement': [{'Sid': 'IngestionSQSPublish', 'Effect': 'Allow',
                                  'Action': ['sqs:SendMessage', 'sqs:GetQueueUrl'], 'Resource': queue}]}
        fixture = {'resources': [{'mode': 'managed', 'type': 'aws_iam_role_policy',
            'name': 'gateway_ingestion_sqs_publish',
            'instances': [{'attributes': {'policy': json.dumps(policy)}}]}]}
        self.assertEqual(state.release_settings(fixture, 'gateway'), {
            'enable_agent_context_sqs': True, 'agent_context_ingestion_queue_arn': queue})
        # #7944: the same statement now rides in the consolidated data managed policy,
        # and sits under the pass-1 legacy carrier mid-cutover; both yield the queue.
        # The other consolidated groups carry no ingestion statement and infer nothing.
        other = {'Statement': [{'Sid': 'CognitoReadUsers', 'Effect': 'Allow', 'Action': ['cognito-idp:ListUsers'],
                                'Resource': 'arn:aws:cognito-idp:us-east-1:925091290508:userpool/us-east-1_abc'}]}
        for kind, name, key in (('aws_iam_policy', 'gateway_role', 'data'),
                                ('aws_iam_role_policy', 'gateway_role_legacy', 'ingestion-sqs-publish')):
            consolidated = {'resources': [
                {'mode': 'managed', 'type': kind, 'name': name,
                 'instances': [{'index_key': key, 'attributes': {'policy': json.dumps(policy)}},
                               {'index_key': 'identity', 'attributes': {'policy': json.dumps(other)}}]}]}
            with self.subTest(kind=kind):
                self.assertEqual(state.release_settings(consolidated, 'gateway'), {
                    'enable_agent_context_sqs': True, 'agent_context_ingestion_queue_arn': queue})
        self.assertNotIn('enable_agent_context_sqs', state.release_settings({'resources': [
            {'mode': 'managed', 'type': 'aws_iam_policy', 'name': 'gateway_role',
             'instances': [{'index_key': 'identity', 'attributes': {'policy': json.dumps(other)}}]}]}, 'gateway'))
        # Explicit retained settings take precedence over legacy inference.
        fixture['outputs'] = {'release_configuration': {'value': {'enable_agent_context_sqs': False}}}
        self.assertEqual(state.release_settings(fixture, 'gateway'), {'enable_agent_context_sqs': False})
        del fixture['outputs']
        for resource in ('*', [queue], 'not-an-arn'):
            policy['Statement'][0]['Resource'] = resource
            fixture['resources'][0]['instances'][0]['attributes']['policy'] = json.dumps(policy)
            with self.subTest(resource=resource), self.assertRaisesRegex(ValueError, 'original target-specific'):
                state.release_settings(fixture, 'gateway')
        self.assertNotIn('enable_agent_context_sqs', state.release_settings({}, 'gateway'))

    def test_output_contract_matches_declared_inputs(self):
        contract = json.loads((ROOT / 'config/release-defaults/preserved-inputs.json').read_text())
        import re
        for module, folder in [('platform', 'platform/infra'), ('gateway', 'modules/gateway/infra'),
                               ('webhook-ingress', 'modules/agent-factory/webhook-ingress/infra')]:
            content = '\n'.join(p.read_text() for p in (ROOT / folder).glob('*.tf'))
            variables = set(re.findall(r'variable "([a-z0-9_]+)"', content))
            self.assertFalse(set(contract[module]) - variables)
            output = (ROOT / folder / 'release-configuration.tf').read_text()
            # A transient hold is a recovery marker, not a retained operator input.
            declared = set(re.findall(r'= var\.([a-z0-9_]+)', output)) - {'orchestration_tick_upgrade_hold'}
            self.assertEqual(declared, set(contract[module]))

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='true')
    def test_portable_upgrade_retains_chat_inputs_omitted_by_older_outputs(self):
        bindings = [{'source': 'chat', 'producer_role': 'arn:aws:iam::111122223333:role/ingest',
                     'chat_supervisor_role': 'arn:aws:iam::111122223333:role/supervisor',
                     'tenant_id': 'customer-org', 'personas': ['chief-of-staff']}]
        expected = {'feature_chat_enabled': True, 'feature_assistant_ui_enabled': False,
                    'model_root_bindings': bindings}
        fixture = {'resources': [
            {'mode': 'managed', 'type': 'aws_ssm_parameter', 'name': name,
             'instances': [{'attributes': {'value': value}}]}
            for name, value in [('feature_chat', 'true'), ('feature_assistant_ui', 'false'),
                                ('model_root_bindings', json.dumps(bindings))]
        ]}
        for outputs in ({}, {'release_configuration': {'value': {}}},
                        {'release_configuration': {'value': {'enable_api_gateway': True}}}):
            with self.subTest(outputs=outputs):
                fixture['outputs'] = outputs
                before = json.dumps(fixture, sort_keys=True)
                self.assertEqual(state.release_settings(fixture, 'gateway'),
                                 {**expected, **outputs.get('release_configuration', {}).get('value', {})})
                self.assertEqual(json.dumps(fixture, sort_keys=True), before)

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='true')
    def test_explicit_retained_chat_settings_win_including_disable_and_empty_bindings(self):
        saved = {'feature_chat_enabled': False, 'feature_assistant_ui_enabled': True,
                 'model_root_bindings': []}
        fixture = {'outputs': {'release_configuration': {'value': saved}}, 'resources': [
            {'mode': 'managed', 'type': 'aws_ssm_parameter', 'name': name,
             'instances': [{'attributes': {'value': 'old-value-must-not-override-output'}}]}
            for name in ('feature_chat', 'feature_assistant_ui', 'model_root_bindings')
        ]}
        self.assertEqual(state.release_settings(fixture, 'gateway'), saved)

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='true')
    def test_invalid_or_ambiguous_chat_state_refuses_portable_upgrade(self):
        for name, value in [('feature_chat', 'yes'), ('feature_assistant_ui', True),
                            ('model_root_bindings', 'not-json'), ('model_root_bindings', '{}'),
                            ('model_root_bindings', '[1]'), ('model_root_bindings', None)]:
            fixture = {'outputs': {'release_configuration': {'value': {}}}, 'resources': [
                {'mode': 'managed', 'type': 'aws_ssm_parameter', 'name': name,
                 'instances': [{'attributes': {'value': value}}]}
            ]}
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                state.release_settings(fixture, 'gateway')
        fixture['resources'][0]['instances'] = [{'attributes': {'value': '[]'}}] * 2
        with self.assertRaisesRegex(ValueError, 'Ambiguous retained chat input'):
            state.release_settings(fixture, 'gateway')

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='true')
    def test_chat_recovery_does_not_import_child_module_or_data_parameters(self):
        base = {'mode': 'managed', 'type': 'aws_ssm_parameter', 'name': 'feature_chat',
                'instances': [{'attributes': {'value': 'true'}}]}
        fixture = {'resources': [{**base, 'module': 'module.unrelated'}, {**base, 'mode': 'data'}]}
        self.assertEqual(state.release_settings(fixture, 'gateway'), {})

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='false')
    def test_source_upgrade_keeps_operator_chat_overlay_authoritative(self):
        fixture = {'outputs': {'release_configuration': {'value': {'feature_chat_enabled': False}}},
                   'resources': [{'mode': 'managed', 'type': 'aws_ssm_parameter', 'name': 'feature_chat',
                                  'instances': [{'attributes': {'value': 'false'}}]}]}
        self.assertEqual(state.release_settings(fixture, 'gateway'), {})

    def test_portable_defaults_disable_test_users_and_reject_unsafe_templates_before_writes(self):
        for environment in ('dev', 'staging', 'prod'):
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / 'source'
                shutil.copytree(ROOT / 'config/release-defaults', root / 'config/release-defaults')
                shutil.copytree(ROOT / 'environments/dev', root / 'environments' / environment)
                config.prepare(root, environment, 'us-east-1')
                target = root / 'environments' / environment / 'modules/gateway.tfvars'
                self.assertRegex(target.read_text(), r'create_test_users\s*=\s*false')
                before = {p: p.read_bytes() for p in (root / 'environments').rglob('*.tfvars')}
                template = root / 'config/release-defaults/gateway.tfvars'
                template.write_text(template.read_text().replace('create_test_users                  = false',
                                                                'create_test_users                  = true'))
                with self.assertRaisesRegex(ValueError, 'must disable create_test_users'):
                    config.prepare(root, environment, 'us-east-1')
                self.assertEqual(before, {p: p.read_bytes() for p in before})

    @patch.dict(os.environ, ADP_PORTABLE_RELEASE_CONFIG='true')
    def test_legacy_test_admin_is_not_silently_deleted_by_new_defaults(self):
        fixture = {'resources': [{'mode': 'managed', 'type': 'aws_cognito_user', 'name': 'test_admin',
                                 'instances': [{'attributes': {'username': 'test@example.com'}}]}]}
        with self.assertRaisesRegex(ValueError, 'avoid deleting the only administrator'):
            state.release_settings(fixture, 'gateway')
        fixture['outputs'] = {'release_configuration': {'value': {}}}
        with self.assertRaisesRegex(ValueError, 'avoid deleting the only administrator'):
            state.release_settings(fixture, 'gateway')
        fixture['outputs'] = {'release_configuration': {'value': {'create_test_users': True}}}
        self.assertEqual(state.release_settings(fixture, 'gateway'), {'create_test_users': True})
