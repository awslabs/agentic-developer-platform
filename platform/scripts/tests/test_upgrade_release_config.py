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
    def test_legacy_engine_target_and_database_size_are_retained(self):
        def resource(kind, name, attrs):
            return {'mode': 'managed', 'type': kind, 'name': name, 'instances': [{'attributes': attrs}]}
        fixture = {'resources': [resource('aws_lambda_function', 'tick', {'environment': [{'variables': {
            'FEATURE_ORCHESTRATION_ENGINE_ENABLED': 'true', 'BG_ORCH_DISPATCH_REPO': 'customer/project'}}]}),
            resource('aws_db_instance', 'db', {'instance_class': 'db.r6g.large', 'allocated_storage': 100})]}
        self.assertEqual(state.release_settings(fixture, 'gateway'), {'orchestration_engine_enabled': True,
            'orchestration_dispatch_repo': 'customer/project', 'rds_instance_class': 'db.r6g.large', 'rds_allocated_storage': 100})

    def test_output_contract_matches_declared_inputs(self):
        contract = json.loads((ROOT / 'config/release-defaults/preserved-inputs.json').read_text())
        import re
        for module, folder in [('platform', 'platform/infra'), ('gateway', 'modules/gateway/infra'),
                               ('webhook-ingress', 'modules/agent-factory/webhook-ingress/infra')]:
            content = '\n'.join(p.read_text() for p in (ROOT / folder).glob('*.tf'))
            variables = set(re.findall(r'variable "([a-z0-9_]+)"', content))
            self.assertFalse(set(contract[module]) - variables)
            output = (ROOT / folder / 'release-configuration.tf').read_text()
            self.assertEqual(set(re.findall(r'= var\.([a-z0-9_]+)', output)), set(contract[module]))
