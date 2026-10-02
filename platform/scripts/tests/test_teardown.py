"""Behavioral regressions from an interrupted, multi-state teardown."""
import importlib.util
import json
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

    def test_failed_lambda_stage_never_reaches_storage_or_final_apply(self):
        self.run.prepared['gateway'] = []
        with patch.object(self.run, 'ingress'), \
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
        self.assertEqual(events[:6], [('prepare', p) for p in t.ORDER])
        self.assertEqual(events[6:], [('execute', p) for p in t.ORDER[:3]])
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


if __name__ == '__main__':
    unittest.main()
