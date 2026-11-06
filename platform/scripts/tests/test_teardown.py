"""Behavioral regressions from an interrupted, multi-state teardown."""
import importlib.util
import json
import os
import subprocess

import pytest
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

PATH = Path(__file__).resolve().parents[1] / 'teardown.py'
SPEC = importlib.util.spec_from_file_location('teardown', PATH)
t = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t)


def state(*rows):
    return {'resources': [{'mode': 'managed', 'type': kind, 'name': name,
                           'instances': [{'attributes': attrs}]} for kind, name, attrs in rows]}


def change(kind, name, before=None, actions=None):
    return {'address': f'{kind}.{name}', 'type': kind,
            'change': {'actions': actions or ['delete'], 'before': before or {}}}


class TeardownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = t.Run(self.temp.name, '123456789012', 'us-east-1', 'dev')
        self.run.states = {p: {} for p in t.ORDER}

    def test_tools_selection_precedes_gateway_and_uses_isolated_state_key(self):
        self.run.states['tools'] = state(('aws_bedrockagentcore_gateway', 'tools', {'id': 'gateway'}))
        for phase in ('gateway', 'platform'):
            with self.subTest(phase=phase), self.assertRaisesRegex(t.TeardownError, 'tools'):
                self.run.check_selection([phase])
        self.assertLess(t.ORDER.index('tools'), t.ORDER.index('gateway'))
        with patch.dict(t.os.environ, {'ADP_TOOLS_STATE_BUCKET': 'private-tools-state'}):
            self.assertEqual(self.run.state_backend('tools'),
                             ('private-tools-state', 'tools/platform/dev.tfstate'))
        self.assertEqual(self.run.state_backend('platform'),
                         (self.run.bucket, 'dev/platform/terraform.tfstate'))

    def test_tools_only_dry_run_plans_only_tools_and_real_run_requires_typed_account(self):
        selected = []
        with patch.object(t, 'Run', return_value=self.run), \
             patch.object(self.run, 'load_states'), patch.object(self.run, 'setup_kube'), \
             patch.object(self.run, 'check_selection', side_effect=lambda phases: selected.extend(phases)), \
             patch.object(self.run, 'prepare', return_value=False), \
             patch.object(self.run, 'quiesce'), patch.object(self.run, 'execute'), \
             patch.object(t, 'aws', return_value={'Account': '123456789012', 'Arn': 'operator'}), \
             patch.object(t, 'command', return_value='source-sha'), \
             patch.dict(t.os.environ, {}, clear=True):
            with patch('builtins.input', side_effect=AssertionError('dry-run prompted')):
                t.main(['--tools-only', '--dry-run', '--root', self.temp.name])
            self.assertEqual(selected, ['tools'])
            with patch('builtins.input', return_value='wrong-account'):
                with self.assertRaisesRegex(t.TeardownError, 'Account confirmation did not match'):
                    t.main(['--tools-only', '--root', self.temp.name, '--yes'])
                with self.assertRaisesRegex(t.TeardownError, 'Account confirmation did not match'):
                    t.main(['--phase', 'tools', '--root', self.temp.name])
            self.assertEqual(selected, ['tools'])
            with patch('builtins.input', return_value='123456789012'):
                t.main(['--tools-only', '--root', self.temp.name])
            self.assertEqual(selected, ['tools', 'tools'])
        with self.assertRaisesRegex(t.TeardownError, 'cannot combine'):
            t.main(['--tools-only', '--bootstrap', '--root', self.temp.name])

    def test_tools_preflight_binds_private_inputs_and_kms_destroy_override(self):
        self.run.states['tools'] = state(
            ('aws_kms_key', 'gateway', {'id': 'key'}),
            ('aws_kms_key', 'assertion', {'id': 'assertion-key'}))
        (self.run.directory / 'kubeconfig').touch()
        supplied = {'aws_account_id': self.run.account,
                    'tools_facade_role_arn': 'arn:aws:iam::123456789012:role/adp-dev-role-tools-facade',
                    'interceptor_lambda_arn': 'arn:aws:lambda:us-east-1:123456789012:function:example-interceptor',
                    'interceptor_execution_role_arn': 'arn:aws:iam::123456789012:role/example-interceptor'}
        with patch.dict(t.os.environ, {'ADP_TOOLS_TFVARS_JSON': json.dumps(supplied),
                                        'ADP_TOOLS_STATE_BUCKET': 'private-tools-state'}), \
             patch.object(self.run, 'tf', return_value=json.dumps(self.run.states['tools'])), \
             patch.object(self.run, 'plan'), patch.object(self.run, 'tools_facade'), \
             patch.object(self.run, 'runtime_secrets'), patch.object(self.run, 'runtime_parameters'):
            self.assertTrue(self.run.prepare('tools'))
        backend = json.loads((self.run.directory / 'tools-backend.json').read_text())
        self.assertEqual(backend['key'], 'tools/platform/dev.tfstate')
        self.assertEqual(backend['bucket'], 'private-tools-state')
        self.assertNotIn('dynamodb_table', backend)
        override = self.run.root / 'modules/tools/platform/infra/adp_teardown_target_override.tf.json'
        self.addCleanup(override.unlink, missing_ok=True)
        self.assertEqual(json.loads(override.read_text())['resource']['aws_kms_key'], {
            'gateway': {'lifecycle': {'prevent_destroy': False}},
            'assertion': {'lifecycle': {'prevent_destroy': False}},
        })
        variables = self.run.directory / 'tools.tfvars.json'
        self.assertEqual(variables.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(variables.read_text())['aws_account_id'], self.run.account)

    def test_tools_inputs_reject_retired_gateway_role(self):
        supplied = {'aws_account_id': self.run.account,
                    'adp_gateway_role_arn': 'arn:aws:iam::123456789012:role/gateway'}
        with patch.dict(t.os.environ, {'ADP_TOOLS_TFVARS_JSON': json.dumps(supplied)}):
            with self.assertRaisesRegex(t.TeardownError, 'façade role'):
                self.run.inputs('tools', {})
        self.assertFalse((self.run.directory / 'tools.tfvars.json').exists())

    def test_facade_teardown_selects_only_deployment_in_service_namespace(self):
        (self.run.directory / 'kubeconfig').touch()
        image = f'123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-tools-facade@sha256:{"a" * 64}'
        deployment = {'metadata': {'uid': 'original', 'namespace': 'adp-gateway'},
                      'spec': {'template': {'spec': {'containers': [{'name': 'tools-facade', 'image': image}]}}}}
        with patch.object(self.run, 'kube', return_value=json.dumps(deployment)) as kube:
            self.assertTrue(self.run.tools_facade())
            self.assertTrue(self.run.tools_facade(delete=True))
        self.assertEqual([call.args for call in kube.call_args_list], [
            ('get', 'deployment', 'tools-facade', '-n', 'adp-gateway', '--ignore-not-found', '-o', 'json'),
            ('get', 'deployment', 'tools-facade', '-n', 'adp-gateway', '--ignore-not-found', '-o', 'json'),
            ('delete', 'deployment', 'tools-facade', '-n', 'adp-gateway', '--wait=true', '--timeout=300s'),
        ])

    def test_facade_preflight_snapshot_prevents_replaced_deployment_deletion(self):
        (self.run.directory / 'kubeconfig').touch()
        image = f'123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-tools-facade@sha256:{"a" * 64}'
        snapshot = {'metadata': {'uid': 'original', 'namespace': 'adp-gateway'},
                    'spec': {'template': {'spec': {'containers': [{'name': 'tools-facade', 'image': image}]}}}}
        replaced = json.loads(json.dumps(snapshot))
        replaced['metadata']['uid'] = 'replacement'
        with patch.object(self.run, 'kube', side_effect=[json.dumps(snapshot), json.dumps(replaced)]) as kube:
            self.assertTrue(self.run.tools_facade())
            with self.assertRaisesRegex(t.TeardownError, 'changed since the preflight'):
                self.run.tools_facade(delete=True)
        self.assertFalse(any(call.args[0] == 'delete' for call in kube.call_args_list))

    def test_tools_phase_never_empties_separate_state_backend(self):
        self.run.prepared['tools'] = ['-var-file=private']
        bucket = change('aws_s3_bucket', 'state', {'id': 'private-tools-state'})
        with patch.dict(t.os.environ, {'ADP_TOOLS_STATE_BUCKET': 'private-tools-state'}), \
             patch.object(self.run, 'tools_facade'), patch.object(self.run, 'runtime_logs'), \
             patch.object(self.run, 'lambda_cleanup'), patch.object(self.run, 'stop_keda'), \
             patch.object(self.run, 'stage'), patch.object(self.run, 'stop_builds'), \
             patch.object(self.run, 'plan', return_value=(None, [bucket])), \
             patch.object(t, 'empty_bucket') as empty:
            with self.assertRaisesRegex(t.TeardownError, 'Refusing to empty Terraform backend'):
                self.run.execute('tools')
        empty.assert_not_called()

    def test_facade_deletion_runs_before_gateway_plan_and_preserves_backend(self):
        self.run.prepared['tools'] = ['-var-file=private']
        actions = []
        def record(name):
            return lambda *args, **kwargs: actions.append(name)
        with patch.object(self.run, 'tools_facade', side_effect=record('facade')), \
             patch.object(self.run, 'runtime_logs'), patch.object(self.run, 'runtime_secrets'), \
             patch.object(self.run, 'runtime_parameters'), patch.object(self.run, 'lambda_cleanup', side_effect=record('lambda')), \
             patch.object(self.run, 'stop_keda'), patch.object(self.run, 'stage'), \
             patch.object(self.run, 'stop_builds'), patch.object(self.run, 'plan', side_effect=lambda *args: (actions.append('gateway-plan') or (None, []))), \
             patch.object(self.run, 'apply'), patch.object(self.run, 'tf', return_value='{}'), \
             patch.object(self.run, 'verify'):
            self.run.execute('tools')
        self.assertLess(actions.index('facade'), actions.index('lambda'))
        self.assertLess(actions.index('lambda'), actions.index('gateway-plan'))

    def test_absent_optional_module_needs_no_module_tooling(self):
        with patch.object(self.run, 'tf', side_effect=AssertionError('unexpected Terraform')):
            self.assertFalse(self.run.prepare('superplane'))
        self.run.check_selection(['agent_context', 'agent_factory', 'webhook_ingress', 'gateway', 'platform'])

    def test_installed_optional_module_blocks_shared_dependency_removal(self):
        self.run.states['superplane'] = state(('aws_s3_bucket', 'module', {'id': 'module'}))
        for phase in ('agent_context', 'agent_factory', 'gateway', 'platform'):
            with self.subTest(phase=phase), self.assertRaisesRegex(t.TeardownError, 'superplane'):
                self.run.check_selection([phase])
        self.run.check_selection(list(t.ORDER))

    def test_public_insights_only_teardown_keeps_confirmation_and_isolated_selection(self):
        selected = []
        with patch.object(t, 'Run', return_value=self.run), \
             patch.object(self.run, 'load_states'), patch.object(self.run, 'setup_kube'), \
             patch.object(self.run, 'check_selection', side_effect=lambda phases: selected.extend(phases)), \
             patch.object(self.run, 'prepare', return_value=False), \
             patch.object(self.run, 'quiesce'), patch.object(self.run, 'execute'), \
             patch.object(t, 'aws', return_value={'Account': '123456789012', 'Arn': 'operator'}), \
             patch.object(t, 'command', return_value='source-sha'), patch('builtins.input', return_value='123456789012') as prompt, \
             patch.dict(t.os.environ, {}, clear=True):
            t.main(['--insights-only', '--root', self.temp.name])
        self.assertEqual(selected, ['insights'])
        prompt.assert_called_once()

    def test_insights_state_is_removed_before_platform_and_preserves_images(self):
        self.run.states['insights'] = state(('aws_s3_bucket', 'evidence', {'id': 'insights'}))
        with self.assertRaisesRegex(t.TeardownError, 'insights'):
            self.run.check_selection(['platform'])
        self.assertLess(t.ORDER.index('insights'), t.ORDER.index('platform'))
        self.assertEqual(t.MODULES['insights'], ('modules/insights/platform', 'modules/insights'))
        self.run.states['insights']['outputs'] = {'release_configuration': {'value': {
            'emitter_image': 'image@sha256:' + 'a' * 64,
            'distiller_image': '',
            'github_api_cidrs': [],
            'aws_account_id': '123456789012',
        }}}
        args = self.run.inputs('insights', self.run.states['insights'])
        self.assertTrue(args[0].startswith('-var-file='))
        self.assertIn('-var=environment=dev', args)

    def test_superplane_inputs_use_only_the_app_environment(self):
        app = self.run.root / 'modules/domain-apps/superplane/environments/dev/superplane.tfvars'
        app.parent.mkdir(parents=True)
        app.write_text('environment = "dev"\naccount_id = "ACCOUNT_ID"\n')
        args = self.run.inputs('superplane', {})
        self.assertEqual(args[0], '-var-file=' + str(app))
        self.assertIn('-var=environment=dev', args)
        self.assertEqual(args[-1], '-var=account_id=123456789012')
        self.assertIn('ACCOUNT_ID', app.read_text())

    def test_gateway_destroy_retains_the_recorded_image_without_a_default_tag(self):
        digest = 'sha256:' + 'a' * 64
        image = f'123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@{digest}'
        data = {'mode': 'data', 'type': 'aws_ecr_image', 'name': 'orchestration_tick',
                'instances': [{'attributes': {'image_uri': image}}]}
        inputs = {'resources': [data], 'outputs': {'release_configuration': {'value': {}}}}
        args = self.run.inputs('gateway', inputs)
        self.assertIn('-var=orchestration_tick_image_digest=' + digest, args)
        for invalid in (image.replace('123456789012', '999999999999'),
                        image.replace('us-east-1', 'us-west-2'),
                        image.replace('adp-gateway', 'other-image'),
                        image.replace('@' + digest, ':latest'),
                        image.replace(digest, 'sha256:invalid')):
            with self.subTest(image=invalid):
                data['instances'][0]['attributes']['image_uri'] = invalid
                with self.assertRaisesRegex(t.TeardownError, 'Invalid recorded gateway image'):
                    self.run.inputs('gateway', inputs)

    def test_superplane_recorded_inputs_cannot_override_confirmed_account(self):
        args = self.run.inputs('superplane', {'outputs': {'release_configuration': {
            'value': {'account_id': '999999999999'}}}})
        self.assertEqual(args[-1], '-var=account_id=123456789012')

    def test_superplane_does_not_fall_back_to_a_stale_shared_input(self):
        legacy = self.run.root / 'environments/dev/modules/superplane.tfvars'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('environment = "dev"\n')
        with self.assertRaisesRegex(t.TeardownError, 'original deployment inputs are required'):
            self.run.inputs('superplane', {})

    def test_factory_must_precede_webhook_and_platform(self):
        self.run.states['agent_factory'] = state(('aws_sqs_queue', 'work', {'id': 'q'}))
        with self.assertRaisesRegex(t.TeardownError, 'agent_factory'):
            self.run.check_selection(['webhook_ingress', 'gateway', 'platform'])
        self.assertLess(t.ORDER.index('agent_factory'), t.ORDER.index('webhook_ingress'))

    def test_retained_credentials_do_not_block_next_phase(self):
        self.run.states['webhook_ingress'] = state(('aws_secretsmanager_secret', 'app',
                                                   {'name': 'adp/dev/github-app/key', 'id': 'secret'}))
        self.run.check_selection(['gateway'])

    def test_retention_includes_versions_key_alias_scanning_but_not_application_secret(self):
        data = state(
            ('aws_secretsmanager_secret', 'app', {'name': 'adp/org/gh-app-key', 'id': 'secret', 'kms_key_id': 'key'}),
            ('aws_secretsmanager_secret_version', 'app', {'secret_id': 'secret'}),
            ('aws_kms_key', 'credentials', {'id': 'key', 'arn': 'arn:key'}),
            ('aws_kms_alias', 'credentials', {'target_key_id': 'key'}),
            ('aws_ecr_registry_scanning_configuration', 'main', {'id': 'account'}),
            ('aws_secretsmanager_secret', 'database', {'name': 'bedrockgw-dev-db', 'id': 'db'}))
        keep = t.protected(data)
        self.assertEqual(len(keep), 5)
        self.assertNotIn('aws_secretsmanager_secret.database', keep)

    def test_plan_expansion_cannot_delete_retained_resources(self):
        with self.assertRaisesRegex(t.TeardownError, 'retained'):
            t.deletions({'resource_changes': [change('aws_kms_key', 'credentials')]}, ['aws_kms_key.credentials'])
        with self.assertRaisesRegex(t.TeardownError, 'Non-delete'):
            t.deletions({'resource_changes': [change('aws_s3_bucket', 'data', actions=['delete', 'create'])]})

    def test_bucket_access_denied_is_not_absence(self):
        call = Mock(side_effect=t.TeardownError('AccessDenied'))
        with self.assertRaisesRegex(t.TeardownError, 'AccessDenied'):
            t.empty_bucket('bucket', '123456789012', call)
        self.assertEqual(call.call_count, 1)

    def test_bucket_versions_and_markers_then_unversioned_objects(self):
        call = Mock(side_effect=[{}, {'Versions': [{'Key': 'a', 'VersionId': 'v'}],
                                     'DeleteMarkers': [{'Key': 'a', 'VersionId': 'd'}]}, {},
                                {}, {'Contents': [{'Key': 'b'}]}, {}, {}, {}, {}])
        t.empty_bucket('bucket', '123456789012', call)
        batches = [json.loads(c.args[c.args.index('--delete') + 1]) for c in call.call_args_list
                   if c.args[1] == 'delete-objects']
        self.assertEqual(batches[0]['Objects'], [{'Key': 'a', 'VersionId': 'v'}, {'Key': 'a', 'VersionId': 'd'}])
        self.assertEqual(batches[1]['Objects'], [{'Key': 'b'}])

    def test_bucket_per_object_failure_stops_without_looping(self):
        call = Mock(side_effect=[{}, {'Versions': [{'Key': 'a', 'VersionId': 'v'}]}, {'Errors': [{'Code': 'AccessDenied'}]}])
        with self.assertRaisesRegex(t.TeardownError, 'object errors'):
            t.empty_bucket('bucket', '123456789012', call)
        self.assertEqual(call.call_count, 3)

    def test_ecr_batch_failure_stops(self):
        call = Mock(side_effect=[{'imageIds': [{'imageDigest': 'sha256:abc'}]}, {'failures': [{'failureCode': 'KmsError'}]}])
        with self.assertRaisesRegex(t.TeardownError, 'image errors'):
            t.empty_repository('repo', '123456789012', call)

    def test_lambda_stage_cannot_remove_execution_role(self):
        data = state(('aws_lambda_function', 'tick', {'role': 'arn:role/tick'}))
        self.run.states['gateway'] = data
        with patch.object(self.run, 'tf', return_value=json.dumps(data)), \
             patch.object(self.run, 'plan', return_value=('plan', [change('aws_iam_role', 'tick', {'arn': 'arn:role/tick'})])), \
             patch.object(self.run, 'apply') as apply:
            with self.assertRaisesRegex(t.TeardownError, 'execution/access'):
                self.run.stage('gateway', 'lambda-functions', lambda k: k == 'aws_lambda_function')
            apply.assert_not_called()

    def test_platform_kubernetes_stage_allows_nodegroups_but_keeps_cluster_access(self):
        data = state(('kubernetes_config_map', 'cni', {'id': 'kube-system/cni'}))
        self.run.states['platform'] = data
        rows = [change('aws_eks_node_group', 'gvisor')]
        with patch.object(self.run, 'tf', return_value=json.dumps(data)), \
             patch.object(self.run, 'plan', return_value=('plan', rows)), \
             patch.object(self.run, 'apply') as apply:
            self.run.stage('platform', 'kubernetes', lambda k: k.startswith('kubernetes_'))
            apply.assert_called_once_with('platform', 'plan')
            for kind in ('aws_eks_cluster', 'aws_eks_access_entry', 'aws_eks_access_policy_association', 'aws_eks_addon'):
                rows[:] = [change(kind, 'protected')]
                with self.assertRaisesRegex(t.TeardownError, 'execution/access'):
                    self.run.stage('platform', 'kubernetes', lambda k: k.startswith('kubernetes_'))
            self.assertEqual(apply.call_count, 1)

    def test_pending_lambda_eni_keeps_roles_and_checkpoint_for_retry(self):
        self.run.states['gateway'] = state(('aws_lambda_function', 'tick', {
            'role': 'arn:role/tick', 'vpc_config': [{'security_group_ids': ['sg-1'], 'subnet_ids': ['subnet-1']}]}))
        with patch.object(t, 'aws', return_value={'NetworkInterfaces': [{'NetworkInterfaceId': 'eni-1', 'InterfaceType': 'lambda'}]}), \
             patch.object(t.time, 'monotonic', side_effect=[0, 1801]), \
             patch.object(self.run, 'plan', return_value=(None, [])) as plan:
            with self.assertRaisesRegex(t.TeardownError, 'roles and permissions retained'):
                self.run.lambda_cleanup('gateway')
            self.assertEqual(plan.call_count, 1)
            self.assertEqual(json.loads((self.run.directory / 'gateway-lambda-enis.json').read_text()), ['eni-1'])
            self.assertNotIn('gateway', self.run.lambda_cleaned)

    def test_edge_role_deletion_requires_completed_lambda_cleanup(self):
        data = state(('aws_cloudfront_distribution', 'frontend', {'id': 'distribution'}),
                     ('aws_lambda_function', 'broker', {'role': 'arn:role/broker'}))
        self.run.states['gateway'] = data
        rows = [change('aws_iam_role', 'broker', {'arn': 'arn:role/broker'})]
        with patch.object(self.run, 'tf', return_value=json.dumps(data)), \
             patch.object(self.run, 'plan', return_value=('plan', rows)), \
             patch.object(self.run, 'apply') as apply:
            with self.assertRaisesRegex(t.TeardownError, 'execution/access'):
                self.run.stage('gateway', 'cloudfront', lambda k: k == 'aws_cloudfront_distribution')
            apply.assert_not_called()
            self.run.lambda_cleaned.add('gateway')
            self.run.stage('gateway', 'cloudfront', lambda k: k == 'aws_cloudfront_distribution')
            apply.assert_called_once_with('gateway', 'plan')
            for kind in ('aws_lambda_function', 'aws_eks_access_entry'):
                rows[:] = [change(kind, 'unexpected')]
                with self.assertRaisesRegex(t.TeardownError, 'execution/access'):
                    self.run.stage('gateway', 'cloudfront', lambda k: k == 'aws_cloudfront_distribution')
            self.assertEqual(apply.call_count, 1)

    def test_targeted_plan_reconciles_only_verified_absent_cleaned_functions(self):
        arn = f'arn:aws:lambda:us-east-1:{self.run.account}:function:broker'
        attrs = {'arn': arn, 'role': 'arn:role/broker'}
        data = state(('aws_cloudfront_distribution', 'frontend', {'id': 'distribution'}),
                     ('aws_lambda_function', 'broker', attrs))
        self.run.states['gateway'] = data
        rows = [change('aws_lambda_function', 'broker', attrs)]
        with patch.object(self.run, 'tf', return_value=json.dumps(data)), \
             patch.object(self.run, 'plan', return_value=('plan', rows)), \
             patch.object(self.run, 'apply') as apply, patch.object(t, 'aws', return_value=None) as aws:
            with self.assertRaisesRegex(t.TeardownError, 'execution/access'):
                self.run.stage('gateway', 'cloudfront', lambda k: k == 'aws_cloudfront_distribution')
            aws.assert_not_called()
            self.run.lambda_cleaned.add('gateway')
            self.run.stage('gateway', 'cloudfront', lambda k: k == 'aws_cloudfront_distribution')
            apply.assert_called_once_with('gateway', 'plan')
            aws.assert_called_once_with('lambda', 'get-function-configuration', '--function-name', arn,
                                        absent=('ResourceNotFoundException',))
            aws.return_value = {'FunctionArn': arn}
            with self.assertRaisesRegex(t.TeardownError, 'execution/access'):
                self.run.stage('gateway', 'cloudfront', lambda k: k == 'aws_cloudfront_distribution')
            self.assertEqual(apply.call_count, 1)

    def test_failed_lambda_stage_never_reaches_storage_or_final_apply(self):
        self.run.prepared['gateway'] = []
        with patch.object(self.run, 'ingress'), \
             patch.object(self.run, 'stage'), \
             patch.object(self.run, 'lambda_cleanup', side_effect=t.TeardownError('ENI timeout')), \
             patch.object(self.run, 'plan') as plan, patch.object(self.run, 'apply') as apply:
            with self.assertRaisesRegex(t.TeardownError, 'ENI timeout'):
                self.run.execute('gateway')
            plan.assert_not_called()
            apply.assert_not_called()

    def test_independent_vpc_group_requires_explicit_retention(self):
        self.run.states['platform'] = state(('aws_vpc', 'main', {'id': 'vpc-1'}))
        with patch.object(t, 'aws', return_value={'SecurityGroups': [{'GroupId': 'sg-other', 'GroupName': 'evaluation'}]}):
            with self.assertRaisesRegex(t.TeardownError, 'outside ADP state'):
                self.run.check_network()
            self.run.retain_vpc = True
            self.run.check_network()
        self.assertEqual(t.protected(self.run.states['platform'], True), {'aws_vpc.main'})

    def test_large_json_uses_private_file_and_is_removed_after_call(self):
        content = json.dumps({'Objects': [{'Key': 'long-' + 'x' * 200000}]})
        paths = []
        def invoke(argv, **kwargs):
            uri = argv[argv.index('--delete') + 1]
            self.assertTrue(uri.startswith('file://'))
            path = Path(uri[7:])
            self.assertEqual(path.read_text(), content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            paths.append(path)
            return Mock(returncode=0, stdout='{}')
        with patch.object(t.subprocess, 'run', side_effect=invoke):
            t.aws('s3api', 'delete-objects', '--bucket', 'example', '--delete', content)
        self.assertFalse(paths[0].exists())

    def test_aws_permission_failure_not_misclassified_as_missing(self):
        with patch.object(t.subprocess, 'run', return_value=Mock(returncode=1,
                stderr='An error occurred (AccessDenied) when calling HeadBucket')):
            with self.assertRaisesRegex(t.TeardownError, 'AccessDenied'):
                t.aws('s3api', 'head-bucket', '--bucket', 'example', absent=('404',))

    def test_changed_state_lineage_rejects_old_checkpoints(self):
        self.run.states['platform'] = {'lineage': 'first'}
        self.run.bind_checkpoint()
        self.run.states['platform'] = {'lineage': 'replacement-install'}
        with self.assertRaisesRegex(t.TeardownError, 'lineage changed'):
            self.run.bind_checkpoint()

    def test_all_selected_plans_checked_before_first_delete_and_failure_stops_run(self):
        events = []
        def prepare(phase):
            events.append(('prepare', phase))
        def execute(phase):
            events.append(('execute', phase))
            if phase == 'agent_factory':
                raise t.TeardownError('KEDA unavailable')
        with patch.object(t, 'Run', return_value=self.run), \
             patch.object(self.run, 'load_states'), patch.object(self.run, 'setup_kube'), \
             patch.object(self.run, 'check_network'), patch.object(self.run, 'check_account_logging'), \
             patch.object(self.run, 'prepare', side_effect=prepare), \
             patch.object(self.run, 'execute', side_effect=execute), \
             patch.object(t, 'aws', return_value={'Account': '123456789012', 'Arn': 'operator'}), \
             patch.object(t, 'command', return_value='source-sha'), patch('builtins.input', return_value='123456789012'), \
             patch.dict(t.os.environ, {}, clear=True):
            with self.assertRaisesRegex(t.TeardownError, 'KEDA unavailable'):
                t.main(['--root', self.temp.name])
        self.assertEqual(events[:len(t.ORDER)], [('prepare', p) for p in t.ORDER])
        self.assertEqual(events[len(t.ORDER):], [('execute', p) for p in t.ORDER[:t.ORDER.index('agent_factory') + 1]])
        journal = json.loads((self.run.directory / 'journal.json').read_text())
        self.assertEqual(journal['phases']['agent_factory'], 'failed')
        self.assertNotIn('platform', journal['phases'])

    def test_backend_check_includes_other_environments_and_unknown_modules(self):
        def call(*args, **kwargs):
            if args[1] == 'list-objects-v2':
                return {'Contents': [{'Key': 'prod/modules/custom/terraform.tfstate'}]}
            Path(args[-1]).write_text(json.dumps(state(('aws_vpc', 'other', {'id': 'vpc-other'}))))
            return {}
        with patch.object(t, 'aws', side_effect=call):
            with self.assertRaisesRegex(t.TeardownError, 'prod/modules/custom'):
                self.run.assert_backend_empty()

    def test_ecr_manifest_list_progress_retries_children(self):
        call = Mock(side_effect=[{'imageIds': [{'imageDigest': 'parent'}, {'imageDigest': 'child'}]},
                                {'imageIds': [{'imageDigest': 'parent'}], 'failures': [{'failureCode': 'ImageReferencedByManifestList'}]},
                                {'imageIds': [{'imageDigest': 'child'}]}, {'imageIds': [{'imageDigest': 'child'}]}, {'imageIds': []}])
        t.empty_repository('example', '123456789012', call)
        self.assertEqual(call.call_count, 5)

    def test_eks_owned_cluster_group_is_not_an_independent_resource(self):
        self.run.states['platform'] = state(('aws_vpc', 'main', {'id': 'vpc-1'}),
            ('aws_eks_cluster', 'main', {'vpc_config': [{'cluster_security_group_id': 'sg-eks'}]}))
        with patch.object(t, 'aws', return_value={'SecurityGroups': [
                {'GroupId': 'sg-eks', 'GroupName': 'eks-cluster-sg-example'},
                {'GroupId': 'sg-controller', 'GroupName': 'k8s-alb',
                 'Tags': [{'Key': 'elbv2.k8s.aws/cluster', 'Value': 'adp-dev-eks-cluster'}]}]}):
            self.run.check_network()

    def test_auto_mode_group_requires_cluster_and_managed_resource_tags(self):
        cluster = 'adp-dev-eks-cluster'
        for prefix in ('ingress.eks.amazonaws.com', 'service.eks.amazonaws.com'):
            for resource in ('ManagedLBSecurityGroup', 'ManagedBackendSecurityGroup'):
                tags = {'eks:eks-cluster-name': cluster, prefix + '/resource': resource}
                self.assertTrue(t.Run.controller_group(tags, cluster))
                self.assertFalse(t.Run.controller_group(tags, 'another-cluster'))
        self.assertFalse(t.Run.controller_group({'eks:eks-cluster-name': cluster}, cluster))

    def test_cloudfront_service_group_requires_every_origin_in_vpc_to_be_owned(self):
        arn = f'arn:aws:elasticloadbalancing:us-east-1:{self.run.account}:loadbalancer/app/gateway/123'
        self.run.states['platform'] = state(('aws_vpc', 'main', {'id': 'vpc-1'}))
        self.run.states['gateway'] = state(('aws_cloudfront_vpc_origin', 'api', {
            'id': 'vo-owned', 'vpc_origin_endpoint_config': [{'arn': arn}]}))
        group = {'GroupId': 'sg-cloudfront', 'GroupName': 'CloudFront-VPCOrigins-Service-SG',
                 'Tags': [{'Key': 'aws.cloudfront.vpcorigin', 'Value': 'enabled'}]}
        for extra in (False, True):
            origins = [{'Id': 'vo-owned', 'OriginEndpointArn': arn}]
            if extra:
                origins.append({'Id': 'vo-independent', 'OriginEndpointArn': arn})
            def call(service, operation, *args, **kwargs):
                if operation == 'describe-security-groups':
                    return {'SecurityGroups': [group]}
                if operation == 'list-vpc-origins':
                    return {'VpcOriginList': {'Items': origins}}
                if operation == 'describe-load-balancers':
                    return {'LoadBalancers': [{'VpcId': 'vpc-1'}]}
                self.fail((service, operation))
            with self.subTest(extra=extra), patch.object(t, 'aws', side_effect=call):
                if extra:
                    with self.assertRaisesRegex(t.TeardownError, 'outside ADP state'):
                        self.run.check_network()
                else:
                    self.run.check_network()
        self.run.states['gateway'] = {}
        with patch.object(t, 'aws', return_value={'SecurityGroups': [group]}):
            with self.assertRaisesRegex(t.TeardownError, 'outside ADP state'):
                self.run.check_network()

    def test_gateway_removes_cloudfront_before_ingress(self):
        self.run.prepared['gateway'] = []
        order = []
        def stage(phase, label, kinds):
            order.append(label)
            self.assertTrue(kinds('aws_cloudfront_distribution'))
            self.assertTrue(kinds('aws_cloudfront_vpc_origin'))
            self.assertFalse(kinds('aws_lb'))
        def ingress(*args):
            order.append('ingress')
            raise t.TeardownError('stop after ordering check')
        with patch.object(self.run, 'runtime_logs'), patch.object(self.run, 'stage', side_effect=stage), \
             patch.object(self.run, 'lambda_cleanup', side_effect=lambda phase: order.append('lambda-cleanup')), \
             patch.object(self.run, 'ingress', side_effect=ingress):
            with self.assertRaisesRegex(t.TeardownError, 'ordering check'):
                self.run.execute('gateway')
        self.assertEqual(order, ['lambda-cleanup', 'cloudfront', 'ingress'])

    def test_ingress_absence_is_empty_output_or_an_empty_list(self):
        for output in ('', '\n', '{"items": []}'):
            with self.subTest(output=output), patch.object(self.run, 'kube', return_value=output) as kube, \
                 patch.object(t, 'aws') as aws:
                self.run.ingress('superplane', 'superplane')
                kube.assert_called_once_with('get', 'ingress', '-n', 'superplane', '-o', 'json', '--ignore-not-found')
                aws.assert_not_called()
        self.assertEqual(json.loads((self.run.directory / 'superplane-ingress.json').read_text()), [])

    def test_s3_incomplete_multipart_uploads_are_aborted(self):
        call = Mock(side_effect=[{}, {}, {}, {'Uploads': [{'Key': 'unfinished', 'UploadId': 'upload'}]},
                                {}, {}, {}, {}])
        t.empty_bucket('example', '123456789012', call)
        self.assertEqual(sum(c.args[1] == 'abort-multipart-upload' for c in call.call_args_list), 1)

    def test_lambda_deletion_does_not_apply_expanded_terraform_targets(self):
        attrs = {'arn': 'arn:aws:lambda:us-east-1:123456789012:function:example',
                 'role': 'arn:role/example', 'vpc_config': []}
        self.run.states['gateway'] = state(('aws_lambda_function', 'example', attrs))
        changes = [change('aws_lambda_function', 'example', attrs)]
        with patch.object(self.run, 'plan', return_value=(None, changes)), \
             patch.object(self.run, 'stage') as stage, \
             patch.object(t, 'aws', side_effect=[{'FunctionArn': attrs['arn'], 'Role': attrs['role']}, {}]) as call:
            self.run.lambda_cleanup('gateway')
            stage.assert_not_called()
            self.assertEqual(call.call_args_list[-1].args[:2], ('lambda', 'delete-function'))
            self.assertIn(attrs['arn'], call.call_args_list[-1].args)

    def test_replaced_runtime_secret_is_not_deleted(self):
        t.write_json(self.run.directory / 'gateway-runtime-secrets.json', {'adp/dev/gateway/token-secret-key': 'arn:old'})
        with patch.object(t, 'aws', return_value={'ARN': 'arn:new'}) as call:
            with self.assertRaisesRegex(t.TeardownError, 'replaced'):
                self.run.runtime_secrets('gateway', delete=True)
            self.assertEqual(call.call_count, 1)

    def test_runtime_secret_discovery_never_lists_or_reads_credential_values(self):
        with patch.object(t, 'aws', return_value=None) as call:
            self.run.runtime_secrets('gateway')
        self.assertEqual(call.call_count, 3)
        self.assertTrue(all(c.args[1] == 'describe-secret' for c in call.call_args_list))
        self.assertEqual({c.args[-1] for c in call.call_args_list}, {
            'adp/dev/gateway/token-secret-key', 'adp/dev/gateway/internal-api-key', 'adp/dev/gateway/magic-link-secret'})

    def test_replaced_runtime_log_group_is_not_deleted(self):
        t.write_json(self.run.directory / 'gateway-runtime-log-groups.json', {'/aws/lambda/example': 100})
        with patch.object(t, 'aws', return_value={'logGroups': [{'logGroupName': '/aws/lambda/example', 'creationTime': 200}]}) as call:
            with self.assertRaisesRegex(t.TeardownError, 'replaced'):
                self.run.runtime_logs('gateway', delete=True)
            self.assertEqual(call.call_count, 1)

    def test_runtime_log_discovery_uses_parent_ids_and_does_not_select_prefix_neighbors(self):
        self.run.states['gateway'] = state(('aws_lambda_function', 'example', {'function_name': 'example'}),
                                         ('aws_api_gateway_stage', 'main', {'rest_api_id': 'api-id', 'stage_name': 'dev'}))
        def read(*args, **kwargs):
            name = args[-1]
            return {'logGroups': [{'logGroupName': name, 'creationTime': 100},
                                  {'logGroupName': name + '-independent', 'creationTime': 101}]}
        with patch.object(t, 'aws', side_effect=read):
            self.run.runtime_logs('gateway')
        names = json.loads((self.run.directory / 'gateway-runtime-log-groups.json').read_text())
        self.assertEqual(set(names), {'/aws/lambda/example', 'API-Gateway-Execution-Logs_api-id/dev'})

    def test_verification_distinguishes_kms_wait_period_from_active_resource(self):
        t.write_json(self.run.directory / 'platform-deletion-manifest.json', {
            'aws_kms_key.logs': {'type': 'aws_kms_key', 'id': 'key-id'}})
        with patch.object(t, 'aws', return_value={'KeyMetadata': {'KeyState': 'PendingDeletion', 'DeletionDate': 'later'}}):
            self.run.verify('platform')
        receipt = json.loads((self.run.directory / 'platform-verification.json').read_text())
        self.assertEqual(len(receipt['pending_deletion']), 1)
        with patch.object(t, 'aws', return_value={'KeyMetadata': {'KeyState': 'Enabled'}}):
            with self.assertRaisesRegex(t.TeardownError, 'not pending deletion'):
                self.run.verify('platform')

    def test_rds_verification_uses_arn_not_opaque_terraform_resource_id(self):
        arn = 'arn:aws:rds:us-east-1:123456789012:db:example'
        t.write_json(self.run.directory / 'gateway-deletion-manifest.json', {
            'aws_db_instance.database': {'type': 'aws_db_instance', 'id': 'db-OPAQUE', 'arn': arn}})
        with patch.object(t, 'aws', return_value=None) as call:
            self.run.verify('gateway')
        self.assertEqual(call.call_args.args[-1], arn)

    def test_secret_recovery_window_is_preserved_by_default(self):
        arn = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:marker-ABCDEF'
        t.write_json(self.run.directory / 'webhook_ingress-deletion-manifest.json', {
            'aws_secretsmanager_secret.marker': {'type': 'aws_secretsmanager_secret', 'id': arn}})
        with patch.object(t, 'aws', return_value={'ARN': arn, 'Name': 'marker', 'DeletedDate': 'later'}) as call:
            self.run.verify('webhook_ingress')
        self.assertEqual(call.call_count, 1)
        receipt = json.loads((self.run.directory / 'webhook_ingress-verification.json').read_text())
        self.assertEqual(len(receipt['pending_deletion']), 1)

    def test_opt_in_secret_purge_waits_for_absence(self):
        arn = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:marker-ABCDEF'
        secret = {'ARN': arn, 'Name': 'adp/dev/webhook-ingress/marker-signing-key', 'DeletedDate': 'later'}
        t.write_json(self.run.directory / 'webhook_ingress-deletion-manifest.json', {
            'aws_secretsmanager_secret.marker': {'type': 'aws_secretsmanager_secret', 'id': arn, 'arn': arn}})
        self.run.purge_deleted_secrets = True
        with patch.object(t, 'aws', side_effect=[secret, {}, secret, None]) as call, patch.object(t.time, 'sleep'):
            self.run.verify('webhook_ingress')
        self.assertIn('--force-delete-without-recovery', call.call_args_list[1].args)
        receipt = json.loads((self.run.directory / 'webhook_ingress-verification.json').read_text())
        self.assertEqual(receipt['pending_deletion'], [])
        self.assertEqual(receipt['verified_absent'], ['aws_secretsmanager_secret.marker'])

    def test_secret_purge_refuses_replaced_retained_or_active_secret(self):
        arn = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:marker-ABCDEF'
        self.run.purge_deleted_secrets = True
        t.write_json(self.run.directory / 'webhook_ingress-deletion-manifest.json', {
            'aws_secretsmanager_secret.marker': {'type': 'aws_secretsmanager_secret', 'id': arn}})
        for secret in (
            {'ARN': arn + 'replaced', 'Name': 'marker', 'DeletedDate': 'later'},
            {'ARN': arn, 'Name': 'adp/dev/github-app/key', 'DeletedDate': 'later'},
            {'ARN': arn, 'Name': 'adp/dev/webhook-ingress/github-webhook-secret', 'DeletedDate': 'later'},
            {'ARN': arn, 'Name': 'marker'},
        ):
            with self.subTest(secret=secret), patch.object(t, 'aws', return_value=secret) as call:
                with self.assertRaises(t.TeardownError):
                    self.run.verify('webhook_ingress')
                self.assertEqual(call.call_count, 1)

    def test_secret_purge_timeout_does_not_claim_absence(self):
        arn = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:marker-ABCDEF'
        with patch.object(t, 'aws', return_value={'ARN': arn}), patch.object(t.time, 'monotonic', side_effect=[0, 121]):
            with self.assertRaisesRegex(t.TeardownError, 'still pending'):
                self.run.purge_secret('marker', {'id': arn}, {'ARN': arn, 'Name': 'marker'})

    def test_keda_jobs_deleted_before_auth_and_without_forcing_finalizers(self):
        with patch.object(self.run, 'kube', return_value='exists') as kube:
            self.run.stop_keda('webhook_ingress')
        deletes = [c.args for c in kube.call_args_list if c.args[0] == 'delete']
        self.assertEqual([c[1] for c in deletes], ['scaledjobs', 'scaledobjects', 'triggerauthentications'])
        self.assertTrue(all('adp-agents' in c and '--wait=true' in c for c in deletes))
        self.assertFalse(any(c.args[0] == 'patch' for c in kube.call_args_list))

    def test_keda_timeout_stops_before_removing_auth(self):
        def kube(*args):
            if args[:2] == ('delete', 'scaledjobs'):
                raise t.TeardownError('timeout')
            return 'exists'
        with patch.object(self.run, 'kube', side_effect=kube) as call:
            with self.assertRaisesRegex(t.TeardownError, 'timeout'):
                self.run.stop_keda('agent_factory')
        self.assertFalse(any(c.args[:2] == ('delete', 'triggerauthentications') for c in call.call_args_list))

    def test_quiesce_scales_gateway_without_waiting_for_completed_migration_jobs(self):
        self.run.states['gateway'] = state(('aws_lambda_function', 'example', {'id': 'function'}))
        def kube(*args):
            if args[:2] == ('get', 'pods'):
                return json.dumps({'items': [
                    {'metadata': {'name': 'gateway', 'ownerReferences': [{'kind': 'ReplicaSet'}]}},
                    {'metadata': {'name': 'completed-migration', 'ownerReferences': [{'kind': 'Job'}]}}]})
            if args[:2] == ('get', 'deployments'):
                return 'deployment.apps/gateway'
            if args[:2] == ('get', 'namespace'):
                return 'namespace/adp-gateway'
            return ''
        with patch.object(self.run, 'kube', side_effect=kube) as call:
            self.run.quiesce(['gateway'])
        wait = next(c.args for c in call.call_args_list if c.args[0] == 'wait')
        self.assertIn('pod/gateway', wait)
        self.assertNotIn('pod/completed-migration', wait)
        self.assertTrue(any(c.args[0] == 'scale' and '--replicas=0' in c.args for c in call.call_args_list))

    def test_changed_discovery_parameter_is_preserved(self):
        t.write_json(self.run.directory / 'gateway-runtime-parameters.json', {
            '/adp/dev/gateway/cloudfront-id': {'version': 1, 'modified': 'first'}})
        with patch.object(t, 'aws', return_value={'Parameter': {'Version': 1, 'LastModifiedDate': 'replacement'}}) as call:
            with self.assertRaisesRegex(t.TeardownError, 'changed since planning'):
                self.run.runtime_parameters('gateway', delete=True)
            self.assertEqual(call.call_count, 1)

    def test_teardown_removes_both_alb_caches_before_fresh_install(self):
        names = {f'/adp/dev/gateway/{plane}-{suffix}'
                 for plane in ('internal-alb', 'internal-plane-alb')
                 for suffix in ('arn', 'dns', 'security-group-ids')}
        neighbor = '/adp/dev/gateway/internal-plane-alb-other-owner'
        parameters = {name: {'Version': 1, 'LastModifiedDate': 'original'}
                      for name in names | {neighbor}}
        def aws(*args, **kwargs):
            self.assertEqual(args[0], 'ssm')
            name = args[args.index('--name') + 1]
            if args[1] == 'get-parameter':
                return {'Parameter': parameters[name]} if name in parameters else None
            self.assertEqual(args[1], 'delete-parameter')
            parameters.pop(name, None)
        with patch.object(t, 'aws', side_effect=aws):
            self.run.runtime_parameters('gateway')
            self.assertEqual(set(parameters), names | {neighbor})  # Preview is read-only.
            self.run.runtime_parameters('gateway', delete=True)
            self.assertEqual(set(parameters), {neighbor})
            self.run.runtime_parameters('gateway', delete=True)  # Already absent is safe.
            self.assertEqual(set(parameters), {neighbor})

    def test_changed_internal_plane_cache_is_not_deleted(self):
        name = '/adp/dev/gateway/internal-plane-alb-arn'
        parameter = {'Version': 1, 'LastModifiedDate': 'original'}
        def aws(*args, **kwargs):
            self.assertEqual(args[:2], ('ssm', 'get-parameter'))
            return {'Parameter': parameter} if args[args.index('--name') + 1] == name else None
        with patch.object(t, 'aws', side_effect=aws):
            self.run.runtime_parameters('gateway')
            parameter['Version'] = 2
            with self.assertRaisesRegex(t.TeardownError, 'changed since planning'):
                self.run.runtime_parameters('gateway', delete=True)


if __name__ == '__main__':
    unittest.main()


LAUNCHER = Path(__file__).resolve().parents[1] / "undeploy.sh"
CREDENTIALS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_SESSION_NAME",
)


@pytest.fixture
def launcher(tmp_path):
    script = tmp_path / "undeploy.sh"
    script.write_text(LAUNCHER.read_text())
    (tmp_path / "load-deploy-config.sh").write_text(
        'export PROFILE_AT_CONFIG="${AWS_PROFILE:-}"\n'
        'export KEY_AT_CONFIG="${AWS_ACCESS_KEY_ID:-}"\n'
    )
    (tmp_path / "teardown.py").write_text(
        "import json, os, sys\n"
        'print(json.dumps({"args": sys.argv[1:], "env": dict(os.environ)}))\n'
    )
    return script


def run(launcher, *args):
    env = dict(os.environ, AWS_PROFILE="inherited", AWS_DEFAULT_PROFILE="inherited")
    env.update({key: "fixture-credential" for key in CREDENTIALS})
    env.pop("PROFILE_AT_CONFIG", None)
    return subprocess.run(
        ["bash", str(launcher), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_explicit_profile_precedes_configuration_and_preserves_arguments(launcher):
    result = run(
        launcher,
        "--region",
        "us-east-1",
        "--aws-profile",
        "customer",
        "--purge-deleted-secrets",
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["args"] == [
        "--region",
        "us-east-1",
        "--purge-deleted-secrets",
        "--dry-run",
    ]
    assert data["env"]["PROFILE_AT_CONFIG"] == "customer"
    assert (
        data["env"]["AWS_PROFILE"] == data["env"]["AWS_DEFAULT_PROFILE"] == "customer"
    )
    assert data["env"]["KEY_AT_CONFIG"] == ""
    assert all(key not in data["env"] for key in CREDENTIALS)


def test_no_profile_keeps_existing_credentials_and_empty_arguments(launcher):
    result = run(launcher)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["args"] == []
    assert data["env"]["PROFILE_AT_CONFIG"] == "inherited"
    assert all(data["env"][key] == "fixture-credential" for key in CREDENTIALS)


@pytest.mark.parametrize(
    "args", [("--aws-profile",), ("--aws-profile", ""), ("--aws-profile", "--dry-run")]
)
def test_missing_profile_fails_before_configuration(launcher, args):
    result = run(launcher, *args)
    assert result.returncode == 2
    assert "requires a profile name" in result.stderr
    assert not result.stdout


def test_help_after_profile_does_not_load_configuration(launcher):
    result = run(launcher, "--aws-profile", "customer", "--help")
    assert result.returncode == 0, result.stderr
    assert "--aws-profile PROFILE" in result.stdout
    data = json.loads(result.stdout.splitlines()[-1])
    assert "PROFILE_AT_CONFIG" not in data["env"]
