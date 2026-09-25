import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'platform/scripts/release'))
import common
import artifacts
import storage
import upgrade
import workflow
import bootstrap


def fixture(directory):
    for name in common.REQUIRED_FILES:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, 'w') as archive:
            archive.writestr('index.js', 'exports.handler = async () => {};')
    manifest = {'schema_version': 1, 'repository': 'aws-e/adp', 'release_id': 'r123',
                'source_sha': 'a' * 40, 'terraform_environment': 'dev', 'region': 'us-east-1',
                'migrations': {'modules/gateway/alembic/versions/0001_initial.py': 'c' * 64},
                'files': {name: {'sha256': common.sha256(directory / name), 'size': (directory / name).stat().st_size} for name in common.REQUIRED_FILES},
                'images': {name: {'repository': item[0], 'digest': 'sha256:' + 'd' * 64} for name, item in common.IMAGES.items()}}
    common.json_write(directory / 'manifest.json', manifest)
    return manifest


class ReleaseContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.manifest = fixture(self.directory)

    def test_complete_release_verifies(self):
        self.assertEqual(common.load(self.directory), self.manifest)

    def test_tampered_and_missing_artifact_refused(self):
        path = self.directory / 'frontend.zip'
        path.write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            common.load(self.directory)
        path.unlink()
        with self.assertRaises(ValueError):
            common.load(self.directory)

    def test_incomplete_inventory_refused(self):
        del self.manifest['files']['lambda/session-sweeper.zip']
        with self.assertRaises(ValueError):
            common.validate_manifest(self.manifest)

    def test_unsupported_account_and_target_pairing(self):
        for env, account in [('integration-test', '615296308642'), ('production', '925091290508'), ('pre-production', '')]:
            with self.subTest(env=env), self.assertRaises(ValueError):
                workflow.target(env, account)

    def test_sts_mismatch_refused(self):
        with patch.object(common, 'aws', return_value={'Account': '000000000000'}), self.assertRaises(ValueError):
            common.identity(common.ACCOUNTS['integration-test'])

    def test_bad_manifest_fields(self):
        for key, value in [('source_sha', 'main'), ('region', 'us-west-2'), ('repository', 'attacker/adp'),
                           ('release_id', '../bad'), ('terraform_environment', 'prod')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                common.validate_manifest(dict(self.manifest, **{key: value}))

    def test_artifact_path_and_unexpected_repository_refused(self):
        malicious = copy.deepcopy(self.manifest)
        malicious['files']['../outside.zip'] = malicious['files'].pop('frontend.zip')
        with self.assertRaises(ValueError):
            common.validate_manifest(malicious)
        malicious = copy.deepcopy(self.manifest)
        malicious['images']['gateway']['repository'] = 'different-repository'
        with self.assertRaises(ValueError):
            common.validate_manifest(malicious)

    def test_symlink_package_refused(self):
        path = self.directory / 'frontend.zip'
        other = self.directory / 'other'
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaises(ValueError):
            common.load(self.directory)

    def test_unsafe_archives_refused(self):
        for name in ('../outside', '/tmp/outside', 'safe/../../outside'):
            with self.subTest(name=name):
                archive = self.directory / 'bad.zip'
                with zipfile.ZipFile(archive, 'w') as output:
                    output.writestr(name, 'bad')
                with self.assertRaises(ValueError):
                    common.extract(archive, self.directory / 'extract')
        archive = self.directory / 'bad.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            info = zipfile.ZipInfo('link')
            info.external_attr = 0o120777 << 16
            output.writestr(info, '../outside')
        with self.assertRaises(ValueError):
            common.extract(archive, self.directory / 'extract')

    def test_source_guard_rejects_modified_or_wrong_checkout(self):
        with patch.object(common, 'run', return_value='b' * 40), self.assertRaises(ValueError):
            common.check_source(self.manifest)
        with patch.object(common, 'run', side_effect=['a' * 40, ' M platform/scripts/deploy-all.sh']), self.assertRaises(ValueError):
            common.check_source(self.manifest)

    def test_digest_uri_has_no_mutable_tag(self):
        uri = common.image_uri(self.manifest, 'chat-agent', '615296308642')
        self.assertEqual(uri, '615296308642.dkr.ecr.us-east-1.amazonaws.com/adp-chat-agent@sha256:' + 'd' * 64)

    def test_preproduction_requires_same_successfully_tested_manifest(self):
        digest = common.sha256(self.directory / 'manifest.json')
        evidence = {'status': 'passed', 'account': '608380991969', 'manifest_sha256': digest,
                    'source_sha': self.manifest['source_sha'], 'release_id': 'r123'}
        upgrade.integration_gate(self.manifest, digest, evidence)
        for key in evidence:
            with self.subTest(key=key), self.assertRaises(ValueError):
                upgrade.integration_gate(self.manifest, digest, dict(evidence, **{key: 'wrong'}))

    def test_release_lock_escapes_reserved_owner_attribute(self):
        dynamo = Mock()
        lock = {'LockID': {'S': 'adp-release-upgrade/123456789012'}}
        upgrade.release_lock(dynamo, lock, 'run-owner')
        self.assertEqual(dynamo.delete_item.call_args.kwargs['ConditionExpression'], '#owner = :owner')
        self.assertEqual(dynamo.delete_item.call_args.kwargs['ExpressionAttributeNames'], {'#owner': 'Owner'})

    def test_lock_cleanup_does_not_mask_deployment_failure(self):
        dynamo = Mock()
        dynamo.delete_item.side_effect = RuntimeError('cleanup failed')
        upgrade.release_lock(dynamo, {}, 'run-owner', ValueError('deployment failed'))
        with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
            upgrade.release_lock(dynamo, {}, 'run-owner')

    def test_terraform_inputs_use_verified_bytes_and_keep_identity_config(self):
        overrides = artifacts.overrides(self.manifest, self.directory, '615296308642')
        for name, (module, resource, _) in common.LAMBDAS.items():
            values = overrides[module]['resource']['aws_lambda_function'][resource]
            self.assertEqual(set(values), {'filename', 'source_code_hash'})
            self.assertEqual(values['source_code_hash'], common.code_hash(self.directory / f'lambda/{name}.zip'))
        for name, (module, resource, _) in common.LAYERS.items():
            values = overrides[module]['resource']['aws_lambda_layer_version'][resource]
            self.assertTrue(values['skip_destroy'])
            self.assertEqual(values['lifecycle'], {'create_before_destroy': True})
            self.assertIn(self.manifest['files'][f'layers/{name}.zip']['sha256'], values['s3_key'])
        tick = overrides['modules/gateway/infra']['data']['aws_ecr_image']['orchestration_tick']
        self.assertIsNone(tick['image_tag'])
        self.assertEqual(tick['image_digest'], self.manifest['images']['gateway']['digest'])

    def test_budget_lambda_source_account_is_known_before_apply(self):
        root = (ROOT / 'modules/gateway/infra/main.tf').read_text()
        module = (ROOT / 'modules/gateway/infra/modules/budget-lambda/main.tf').read_text()
        iam = (ROOT / 'modules/gateway/infra/modules/budget-lambda/iam.tf').read_text()
        self.assertIn('account_id  = data.aws_caller_identity.current.account_id', root)
        self.assertIn('source_account = var.account_id', module)
        self.assertNotIn('data "aws_caller_identity" "current"', iam)

    def test_runtime_configuration_is_serialized_as_data(self):
        value = {'VITE_COGNITO_DOMAIN': "example\"; alert(1); //</script>\n"}
        serialized = artifacts.runtime_config(value)
        self.assertNotIn('</script>', serialized)
        self.assertEqual(json.loads(serialized.removeprefix('window.__ADP_CONFIG__ = ').strip().removesuffix(';')), value)

    def test_manifest_published_only_after_all_artifacts(self):
        calls = []
        with patch.object(storage, 'identity'), patch.object(storage, 'client'), patch.object(storage, 'put_once', side_effect=lambda s3, bucket, key, path: calls.append(key)):
            storage.publish(self.directory)
        self.assertEqual(calls[-1], 'releases/r123/manifest.json')
        self.assertEqual(len(calls), len(common.REQUIRED_FILES) + 1)

    def test_incomplete_upload_never_publishes_manifest(self):
        with patch.object(storage, 'identity'), patch.object(storage, 'client'), patch.object(storage, 'put_once', side_effect=RuntimeError('upload failed')) as put:
            with self.assertRaises(RuntimeError):
                storage.publish(self.directory)
        self.assertFalse(any(call.args[2].endswith('manifest.json') for call in put.call_args_list))

    def test_download_rejects_different_manifest_before_any_package_download(self):
        target = self.directory / 'download'
        fake = Mock()
        fake.download_file.side_effect = lambda bucket, key, name: Path(name).write_bytes((self.directory / 'manifest.json').read_bytes())
        with patch.object(storage, 'client', return_value=fake), self.assertRaises(ValueError):
            storage.download(target, 'r123', '0' * 64)
        self.assertEqual(fake.download_file.call_count, 1)

    def test_conditional_storage_write_is_idempotent_but_rejects_collision(self):
        import botocore.exceptions
        fake = Mock()
        path = self.directory / 'frontend.zip'
        storage.put_once(fake, 'bucket', 'key', path)
        self.assertEqual(fake.put_object.call_args.kwargs['IfNoneMatch'], '*')
        fake.put_object.side_effect = botocore.exceptions.ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        fake.head_object.return_value = {'ChecksumSHA256': common.code_hash(path), 'ContentLength': path.stat().st_size}
        storage.put_once(fake, 'bucket', 'key', path)
        fake.head_object.return_value['ChecksumSHA256'] = 'different'
        with self.assertRaises(ValueError):
            storage.put_once(fake, 'bucket', 'key', path)

    def test_manual_approval_requires_designated_actor_workflow_and_main(self):
        env = {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main',
               'GITHUB_WORKFLOW_REF': 'aws-e/adp/.github/workflows/adp-release-promote.yml@refs/heads/main',
               'GITHUB_ACTOR_ID': '20402445', 'GITHUB_ACTOR': 'PranavSharma1000',
               'GITHUB_TRIGGERING_ACTOR': 'PranavSharma1000'}
        workflow.approver(env)
        for key in env:
            with self.subTest(key=key), self.assertRaises(ValueError):
                workflow.approver(dict(env, **{key: 'wrong'}))

    def test_integration_run_must_be_successful_and_from_release_workflow(self):
        value = {'conclusion': 'success', 'status': 'completed', 'event': 'workflow_dispatch',
                 'head_branch': 'main', 'repository': {'full_name': 'aws-e/adp'}, 'path': '.github/workflows/adp-release.yml'}
        with patch.object(workflow, 'run', return_value=json.dumps(value)):
            workflow.integration_run('123')
        for key, bad in [('conclusion', 'failure'), ('status', 'in_progress'), ('event', 'pull_request'),
                         ('head_branch', 'unreviewed'), ('repository', {'full_name': 'other/repo'}), ('path', '.github/workflows/other.yml')]:
            with self.subTest(key=key), patch.object(workflow, 'run', return_value=json.dumps(dict(value, **{key: bad}))), self.assertRaises(ValueError):
                workflow.integration_run('123')
        with self.assertRaises(ValueError):
            workflow.integration_run('../123')

    def test_artifact_helpers_do_not_build_in_release_mode(self):
        binary = self.directory / 'bin'
        binary.mkdir()
        calls = self.directory / 'commands'
        python = binary / 'python3'
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$RELEASE_TEST_LOG"\n')
        python.chmod(0o755)
        env = dict(os.environ, ADP_RELEASE_DIR=str(self.directory), PATH=str(binary) + ':' + os.environ['PATH'], RELEASE_TEST_LOG=str(calls))
        for script in ['platform/scripts/build-agent-factory-lambdas.sh', 'platform/scripts/build-lambda-layers.sh',
                       'modules/gateway/scripts/deploy-broker.sh', 'modules/gateway/scripts/deploy-frontend.sh']:
            subprocess.run(['bash', str(ROOT / script)], env=env, check=True, capture_output=True)
        lines = calls.read_text().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(all('release/artifacts.py' in line for line in lines))

    def test_workflow_gates_exact_release_after_integration(self):
        import yaml
        parent = yaml.safe_load((ROOT / '.github/workflows/adp-release.yml').read_text())
        self.assertNotIn('pre-production', parent['jobs'])
        self.assertEqual(parent['jobs']['integration']['needs'], 'release')
        promote = yaml.safe_load((ROOT / '.github/workflows/adp-release-promote.yml').read_text())
        self.assertEqual(promote['jobs']['promote']['needs'], 'approval')
        self.assertEqual(promote['jobs']['promote']['with']['manifest_sha256'], '${{ needs.approval.outputs.manifest_sha256 }}')
        reusable = yaml.safe_load((ROOT / '.github/workflows/adp-release-upgrade.yml').read_text())
        job = reusable['jobs']['upgrade']
        self.assertEqual(job['environment'], '${{ inputs.environment }}')
        self.assertFalse(job['concurrency']['cancel-in-progress'])
        self.assertIn("github.ref == 'refs/heads/main'", job['if'])
        role_index = next(i for i, step in enumerate(job['steps']) if step.get('uses', '').startswith('aws-actions/configure-aws-credentials'))
        self.assertTrue(any('--check-approver' in step.get('run', '') for step in job['steps'][:role_index]))

    def test_all_github_actions_jobs_use_self_hosted_runners(self):
        import yaml

        allowed = {
            'arc-runner-org',
            "${{ vars.ARC_RUNNER_LABEL || 'arc-runner-org' }}",
        }
        workflow_dir = ROOT / '.github/workflows'
        workflows = sorted([*workflow_dir.glob('*.yml'), *workflow_dir.glob('*.yaml')])
        self.assertTrue(workflows)
        for path in workflows:
            document = yaml.safe_load(path.read_text()) or {}
            for name, job in (document.get('jobs') or {}).items():
                with self.subTest(workflow=path.name, job=name):
                    # Reusable-workflow calls inherit the called workflow's runner and
                    # correctly omit both `steps` and `runs-on` in the caller.
                    if 'uses' in job:
                        continue
                    self.assertIn('steps', job, 'job must define steps or call a reusable workflow')
                    self.assertIn('runs-on', job, 'executable job must select a self-hosted runner')
                    if isinstance(job['runs-on'], dict):
                        self.assertEqual(job['runs-on'], {
                            'group': 'adp-deployment', 'labels': 'arc-runner-deployment',
                        })
                        if (path.name, name) == ('webhook-code-deploy.yml', 'deploy-code'):
                            self.assertEqual(job.get('environment'), 'adp-webhook-code-dev')
                        else:
                            self.assertTrue(str(job.get('environment', '')).startswith(('adp-deploy-', 'adp-build-')))
                        self.assertIn("github.ref == 'refs/heads/main'", job.get('if', ''))
                        continue
                    # S14 isolates the developer persona on its reviewed agent lane.
                    # This label is permitted only for that exact workflow/job pair.
                    if (path.name, name) == ('agent-developer.yml', 'work'):
                        self.assertEqual(job['runs-on'], 'arc-runner-agent')
                        continue
                    self.assertIn(
                        job['runs-on'],
                        allowed,
                        'GitHub-hosted runner labels are prohibited; use arc-runner-org',
                    )


if __name__ == '__main__':
    unittest.main()
