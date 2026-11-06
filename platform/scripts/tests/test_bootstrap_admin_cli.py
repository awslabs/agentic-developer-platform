"""Bootstrap uses explicit operator identities, temporary invitations and safe argv."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('bootstrap_cli', ROOT / 'modules/gateway/scripts/bootstrap-admin.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class BootstrapTests(unittest.TestCase):
    def args(self, *extra):
        with patch.dict(os.environ, {}, clear=True):
            return cli.parser().parse_args(list(extra))

    def runner(self, missing=False, denied=False, bad_claim=False, bootstrap_role="platform_admin"):
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == 'kubectl':
                data = {'status': 'bootstrapped', 'org_id': 'platform-admin', 'role': bootstrap_role}
            elif cmd[1:3] == ['ssm', 'get-parameter']:
                data = {'Parameter': {'Value': 'us-east-1_fixture'}}
            elif cmd[1:3] == ['secretsmanager', 'get-secret-value']:
                data = {'SecretString': json.dumps({'username': 'fixture@example.com', 'password': 'NEVER-PRINT',
                                                   'cognito_user_pool_id': 'us-east-1_fixture'})}
            elif cmd[2] == 'admin-get-user':
                if denied or (missing and not any('admin-create-user' in c for c in calls)):
                    raise subprocess.CalledProcessError(255, cmd, stderr='(AccessDeniedException)' if denied else '(UserNotFoundException)')
                data = {'Enabled': True, 'UserAttributes': [{'Name': 'sub', 'Value': 'sub-id'},
                        {'Name': 'custom:role', 'Value': 'platform_admin'},
                        {'Name': 'custom:org_id', 'Value': 'wrong-org' if bad_claim else 'platform-admin'}]}
            elif cmd[2] == 'admin-create-user':
                data = {'User': {'Enabled': True, 'Attributes': [{'Name': 'sub', 'Value': 'sub-id'}],
                                 'TemporaryPassword': 'NEVER-PRINT'}}
            else:
                data = {}
            self.assertNotIn('shell', kwargs)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), '')
        return calls, run

    def test_selection_fails_before_any_external_calls(self):
        for options in [[], ['--use-test-admin', '--env', 'prod'], ['--use-test-admin', '--env', 'staging'],
                        ['--use-test-admin', '--email', 'admin@example.com']]:
            with self.subTest(options=options), patch.object(cli.subprocess, 'run') as run, self.assertRaises(ValueError):
                cli.bootstrap(self.args(*options))
            run.assert_not_called()

    def test_fresh_operator_invite_then_seed_and_verify_without_password_disclosure(self):
        calls, run = self.runner(missing=True)
        with patch.object(cli.subprocess, 'run', side_effect=run), patch('builtins.print') as output:
            cli.bootstrap(self.args('--email', 'operator@example.com', '--env', 'prod'))
        self.assertEqual([c[2] for c in calls if c[0] == 'aws'],
                         ['get-parameter', 'admin-get-user', 'admin-create-user', 'admin-update-user-attributes', 'admin-get-user'])
        invite = next(c for c in calls if 'admin-create-user' in c)
        self.assertIn('EMAIL', invite)
        attributes = {item['Name']: item['Value'] for item in json.loads(invite[invite.index('--user-attributes') + 1])}
        # The deployed pool requires both standard attributes.
        self.assertEqual(attributes, {'email': 'operator@example.com', 'name': 'operator@example.com'})
        self.assertNotIn('--temporary-password', invite)
        self.assertNotIn('--message-action', invite)
        self.assertNotIn('secretsmanager', str(calls))
        self.assertNotIn('NEVER-PRINT', str(output.call_args_list))
        self.assertNotIn('admin-set-user-password', str(calls))

    def test_existing_operator_is_not_reinvited_and_email_is_one_argument(self):
        email = "operator'$(id)@example.com"
        calls, run = self.runner()
        with patch.object(cli.subprocess, 'run', side_effect=run):
            cli.bootstrap(self.args('--email', email, '--pool-id', 'pool'))
        self.assertFalse(any('admin-create-user' in c for c in calls))
        cmd = next(c for c in calls if c[0] == 'kubectl')
        self.assertEqual(cmd[cmd.index('--email') + 1], email)

    def test_dry_run_never_mutates_existing_or_new_identity(self):
        for missing in (True, False):
            calls, run = self.runner(missing=missing)
            with patch.object(cli.subprocess, 'run', side_effect=run):
                cli.bootstrap(self.args('--email', 'operator@example.com', '--dry-run'))
            self.assertEqual([c[2] for c in calls], ['get-parameter', 'admin-get-user'])

    def test_permission_error_never_creates_user(self):
        calls, run = self.runner(denied=True)
        with patch.object(cli.subprocess, 'run', side_effect=run), self.assertRaises(subprocess.CalledProcessError):
            cli.bootstrap(self.args('--email', 'operator@example.com'))
        self.assertFalse(any('admin-create-user' in c for c in calls))

    def test_claim_readback_must_match_role_and_organization(self):
        _, run = self.runner(bad_claim=True)
        with patch.object(cli.subprocess, 'run', side_effect=run), self.assertRaisesRegex(ValueError, 'did not match'):
            cli.bootstrap(self.args('--email', 'operator@example.com'))

    def test_old_or_mismatched_database_bootstrap_never_writes_claims(self):
        for role in (None, 'member'):
            calls, run = self.runner(bootstrap_role=role)
            with patch.object(cli.subprocess, 'run', side_effect=run), self.assertRaisesRegex(ValueError, 'did not confirm'):
                cli.bootstrap(self.args('--email', 'operator@example.com'))
            self.assertFalse(any('admin-update-user-attributes' in c for c in calls))

    def test_explicit_dev_fixture_can_still_bootstrap(self):
        calls, run = self.runner()
        with patch.object(cli.subprocess, 'run', side_effect=run):
            cli.bootstrap(self.args('--use-test-admin'))
        self.assertIn('get-secret-value', calls[0])
        self.assertFalse(any('admin-create-user' in c for c in calls))

    def test_config_check_has_no_aws_dependency(self):
        with patch.object(cli.subprocess, 'run') as run:
            cli.bootstrap(self.args('--email', 'operator@example.com', '--check-config'))
        run.assert_not_called()
