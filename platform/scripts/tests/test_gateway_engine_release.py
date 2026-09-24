"""Release parity gates with AWS/Kubernetes calls replaced by controlled responses."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('engine_sync', ROOT / 'modules/gateway/scripts/sync-gateway-engine.py')
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)
ACCOUNT = '123456789012'
REGISTRY = f'{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/adp-gateway'
NEW = REGISTRY + '@sha256:' + 'a' * 64
OLD = REGISTRY + '@sha256:' + 'b' * 64


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.engine = OLD
        self.gateway = NEW
        self.account = ACCOUNT
        self.status = 'Successful'
        self.fail_get = False
        self.fail_update = False
        self.race = False

    def command(self, args):
        self.calls.append(args)
        if args[:3] == ['aws', 'sts', 'get-caller-identity']:
            return json.dumps({'Account': self.account})
        if args[:3] == ['aws', 'ecr', 'describe-images']:
            return json.dumps({'imageDetails': [{'imageDigest': NEW.split('@')[1]}]})
        if args[:3] == ['kubectl', 'rollout', 'status']:
            return ''
        if args[:2] == ['kubectl', 'get']:
            return json.dumps({'spec': {'template': {'spec': {'containers': [
                {'name': 'bedrockgateway', 'image': self.gateway}]}}}})
        if args[:3] == ['aws', 'lambda', 'get-function']:
            if self.fail_get:
                raise subprocess.CalledProcessError(254, args)
            return json.dumps({'Code': {'ResolvedImageUri': self.engine}, 'Configuration': {
                'RevisionId': 'observed-revision', 'State': 'Active', 'LastUpdateStatus': self.status}})
        if args[:3] == ['aws', 'lambda', 'update-function-code']:
            if self.fail_update:
                raise subprocess.CalledProcessError(254, args)
            self.engine = args[args.index('--image-uri') + 1]
            if self.race:
                self.gateway = OLD
            return '{}'
        if args[:4] == ['aws', 'lambda', 'wait', 'function-updated-v2']:
            return ''
        self.fail(f'Unexpected command: {args}')

    def sync(self, **kwargs):
        with patch.object(script, 'command', self.command):
            return script.synchronize(image=REGISTRY + ':release', account=ACCOUNT,
                                      region='us-east-1', environment='dev', namespace='adp-gateway', **kwargs)

    def updates(self):
        return [c for c in self.calls if 'update-function-code' in c]

    def test_update_is_pinned_and_revision_fenced(self):
        self.assertEqual(self.sync()['image'], NEW)
        update = self.updates()[0]
        self.assertEqual(update[update.index('--revision-id') + 1], 'observed-revision')
        self.assertEqual(update[update.index('--image-uri') + 1], NEW)
        self.assertTrue(any('function-updated-v2' in c for c in self.calls))

    def test_matching_release_is_idempotent(self):
        self.engine = NEW
        self.sync()
        self.assertFalse(self.updates())

    def test_verify_only_detects_drift_without_mutation(self):
        with self.assertRaisesRegex(ValueError, 'Engine release incomplete'):
            self.sync(verify_only=True)
        self.assertFalse(self.updates())

    def test_wrong_account_stops_before_mutation(self):
        self.account = '999999999999'
        with self.assertRaisesRegex(ValueError, 'AWS account'):
            self.sync()
        self.assertFalse(self.updates())

    def test_wrong_gateway_stops_before_mutation(self):
        self.gateway = OLD
        with self.assertRaisesRegex(ValueError, 'Gateway changed'):
            self.sync()
        self.assertFalse(self.updates())

    def test_missing_or_inaccessible_function_fails(self):
        self.fail_get = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.sync()
        self.assertFalse(self.updates())

    def test_failed_update_cannot_report_success(self):
        self.fail_update = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.sync()

    def test_unsuccessful_lambda_state_fails_even_with_matching_digest(self):
        self.engine = NEW
        self.status = 'Failed'
        with self.assertRaisesRegex(ValueError, 'Engine release incomplete'):
            self.sync()

    def test_concurrent_gateway_change_is_detected(self):
        self.race = True
        with self.assertRaisesRegex(ValueError, 'Gateway changed'):
            self.sync()

    def test_both_entrypoints_require_sync_and_final_verification(self):
        for path in ['platform/scripts/deploy-all.sh', '.github/workflows/gateway-deploy.yml']:
            source = (ROOT / path).read_text()
            self.assertIn('scripts/sync-gateway-engine.py', source)
            self.assertIn('--verify-only --image', source)
        workflow = (ROOT / '.github/workflows/gateway-deploy.yml').read_text()
        self.assertLess(workflow.index('Verify gateway and engine release parity'),
                        workflow.index('Record gateway-backend build evidence'))


if __name__ == '__main__':
    unittest.main()
