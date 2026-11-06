"""The reset workflow must use the supported scripts, with destructive scope fixed."""
import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('clean_install', ROOT / 'platform/scripts/integration-clean-install.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CleanInstallTests(unittest.TestCase):
    def test_dry_run_only_plans_teardown(self):
        steps = module.commands('v2026.10.14-rc.1', True, False)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0][0], 'teardown-plan')
        self.assertIn('--dry-run', steps[0][1])
        self.assertIn('--purge-deleted-secrets', steps[0][1])
        self.assertNotIn('--bootstrap', steps[0][1])

    def test_real_run_uses_full_teardown_then_exact_release_without_recovery(self):
        steps = module.commands('v2026.10.14-rc.2', False, True)
        self.assertEqual([s[0] for s in steps], ['teardown', 'install'])
        self.assertTrue(steps[0][1][1].endswith('/platform/scripts/undeploy.sh'))
        self.assertEqual(steps[1][1], ['bash', str(ROOT / 'deploy.sh'), '--release', 'v2026.10.14-rc.2',
                                     '--env', 'dev', '--region', 'us-east-1', '--allow-known-claude-gap'])
        for _, command in steps:
            for forbidden in ('--resume', '--from', '--update', '--skip', '--skip-agents', '--confirm-destructive', '--bootstrap'):
                self.assertNotIn(forbidden, command)

    def test_non_candidates_are_rejected(self):
        for tag in ('main', '--update', 'v2026.10.14', 'v2026.10.14-rc.1\n', 'v2026.10.14-rc.1;exit'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                module.commands(tag, False, False)

    def test_wrong_confirmation_makes_no_aws_calls(self):
        with patch.dict(os.environ, {'CONFIRMED_ACCOUNT': '999999999999'}), patch.object(module.boto3, 'Session') as session:
            with self.assertRaisesRegex(ValueError, 'confirmation'):
                module.main()
            session.assert_not_called()

    def test_wrong_identity_never_starts_a_script(self):
        with patch.dict(os.environ, {'CONFIRMED_ACCOUNT': module.ACCOUNT}), patch.object(module.boto3, 'Session') as session, \
                patch.object(module.subprocess, 'Popen') as process:
            session.return_value.client.return_value.get_caller_identity.return_value = {'Account': '999999999999'}
            with self.assertRaisesRegex(ValueError, 'identity'):
                module.main()
            process.assert_not_called()

    def test_teardown_failure_prevents_install_and_success_runs_it_once(self):
        for teardown_exit in (0, 1):
            with self.subTest(teardown_exit=teardown_exit), tempfile.TemporaryDirectory() as directory:
                env = {'CONFIRMED_ACCOUNT': module.ACCOUNT, 'RELEASE_TAG': 'v2026.10.14-rc.1',
                       'CLEAN_INSTALL_DRY_RUN': 'false', 'CLEAN_INSTALL_ALLOW_CLAUDE_GAP': 'false',
                       'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'RUNNER_TEMP': directory}
                with patch.dict(os.environ, env), patch.object(module.boto3, 'Session') as session, \
                        patch.object(module.subprocess, 'Popen') as process, \
                        patch.object(module.subprocess, 'check_output', return_value='a' * 40), \
                        patch.object(module.subprocess, 'run'), \
                        patch.object(module.signal, 'signal'):
                    session.return_value.client.return_value.get_caller_identity.return_value = {'Account': module.ACCOUNT}
                    process.return_value.returncode = teardown_exit
                    process.return_value.poll.return_value = teardown_exit
                    if teardown_exit:
                        with self.assertRaisesRegex(RuntimeError, 'teardown script failed'):
                            module.main()
                        self.assertEqual(process.call_count, 1)
                    else:
                        module.main()
                        self.assertEqual(process.call_count, 2)
                        self.assertIn('--release', process.call_args.args[0])

    def test_missing_admin_selection_stops_before_teardown(self):
        env = {'CONFIRMED_ACCOUNT': module.ACCOUNT, 'RELEASE_TAG': 'v2026.10.14-rc.1',
               'CLEAN_INSTALL_DRY_RUN': 'false', 'CLEAN_INSTALL_ALLOW_CLAUDE_GAP': 'false'}
        with patch.dict(os.environ, env), patch.object(module.boto3, 'Session') as session, \
                patch.object(module.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['bootstrap'])) as preflight, \
                patch.object(module.subprocess, 'Popen') as process:
            session.return_value.client.return_value.get_caller_identity.return_value = {'Account': module.ACCOUNT}
            with self.assertRaises(subprocess.CalledProcessError):
                module.main()
            self.assertIn('--check-config', preflight.call_args.args[0])
            process.assert_not_called()

    def test_workflow_is_main_only_self_hosted_and_shares_upgrade_lock(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/adp-integration-clean-install.yml').read_text())
        job = workflow['jobs']['clean-install']
        self.assertEqual(job['runs-on'], 'arc-runner-org')
        self.assertEqual(job['environment'], 'integration-test')
        self.assertIn("github.ref == 'refs/heads/main'", job['if'])
        self.assertEqual(workflow['concurrency']['group'], 'adp-release-upgrade-608380991969')
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        credential_step = next(s for s in job['steps'] if s.get('uses', '').startswith('aws-actions/configure-aws-credentials'))
        self.assertEqual(credential_step['with']['allowed-account-ids'], module.ACCOUNT)
        self.assertEqual(credential_step['with']['role-to-assume'], f'arn:aws:iam::{module.ACCOUNT}:role/adp-release-deploy')
        self.assertFalse(any('upload-artifact' in s.get('uses', '') for s in job['steps']))


if __name__ == '__main__':
    unittest.main()
