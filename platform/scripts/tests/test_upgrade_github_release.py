"""Offline release selection and wrapper routing tests; no AWS mutations."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('github_release', ROOT / 'platform/scripts/upgrade-github-release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def args(self, **kwargs):
        return argparse.Namespace(**dict(dict(release='v1.2.0', env='dev', region='us-east-1',
                                              local=False, gateway_only=False, dry_run=False, update=True), **kwargs))

    def test_published_release_required(self):
        for value in ({'draft': True}, {'published_at': None},
                      {'published_at': 'today', 'tag_name': 'other'}):
            with patch.object(release, 'run', side_effect=['', json.dumps(value)]):
                with self.assertRaises(ValueError):
                    release.resolve('v1.2.0')

    def test_resolves_tag_not_target_branch(self):
        metadata = {'published_at': 'today', 'tag_name': 'v1.2.0', 'target_commitish': 'main'}
        with patch.object(release, 'run', side_effect=['', json.dumps(metadata), json.dumps({'sha': 'a' * 40})]) as run:
            self.assertEqual(release.resolve('v1.2.0')[1], 'a' * 40)
            self.assertEqual(run.call_args.args[0][-1], 'repos/aws-e/adp/commits/v1.2.0')

    def test_dry_run_does_not_create_checkout_or_deploy(self):
        with patch.object(release, 'resolve', return_value=({'id': 1}, 'a' * 40)), \
             patch.object(release, 'run', return_value=json.dumps({'Account': '123', 'Arn': 'caller'})), \
             patch.object(release.tempfile, 'mkdtemp') as directory, \
             patch.object(release.subprocess, 'run') as deploy:
            self.assertEqual(release.upgrade(self.args(dry_run=True)), 0)
            self.assertEqual(release.upgrade(self.args(dry_run=True, update=False)), 0)
            directory.assert_not_called()
            deploy.assert_not_called()

    def test_real_checkout_success_failure_and_moved_tag(self):
        # Use real git and a harmless deployment stand-in, including an annotated tag.
        for exit_code, moved, update, bootstrap_exit in ((0, False, True, 0), (17, False, True, 0), (0, True, True, 0), (0, False, False, 0), (17, False, False, 0), (0, False, False, 12)):
            with self.subTest(exit_code=exit_code, moved=moved), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                origin = directory / 'origin'
                origin.mkdir()
                def git(*args):
                    return subprocess.check_output(['git', *args], cwd=origin, text=True).strip()
                git('init', '--quiet')
                git('config', 'user.email', 'test@example.com')
                git('config', 'user.name', 'Test')
                script = origin / 'platform/scripts/deploy-all.sh'
                script.parent.mkdir(parents=True)
                (script.parent / 'prepare-release-config.py').write_text('from pathlib import Path\nPath("portable-config-used").touch()\n')
                (script.parent / 'bootstrap.sh').write_text('printf bootstrap > bootstrap.txt\nexit ' + str(bootstrap_exit) + '\n')
                script.write_text('#!/bin/bash\n# --update) --confirm-destructive) --allow-known-claude-gap)\nprintf "%s\\n" "$@" > forwarded.txt\nexit ' + str(exit_code) + '\n')
                git('add', '.')
                git('commit', '--quiet', '-m', 'test release')
                git('tag', '-a', 'v1.2.0', '-m', 'release')
                sha = git('rev-parse', 'HEAD')
                work = directory / 'work'
                work.mkdir()
                original_run = release.run
                def fake_services(command, **kwargs):
                    if command[0] == 'aws':
                        return json.dumps({'Account': '123', 'Arn': 'caller'})
                    if 'fetch' in command:
                        command = ['git', 'fetch', '--no-tags', str(origin), 'refs/tags/v1.2.0']
                    return original_run(command, **kwargs)
                with patch.object(release, 'resolve', return_value=({'id': 1}, '0' * 40 if moved else sha)), \
                     patch.object(release, 'run', side_effect=fake_services), \
                     patch.object(release.tempfile, 'mkdtemp', return_value=str(work)):
                    if moved:
                        with self.assertRaisesRegex(ValueError, 'tag changed'):
                            release.upgrade(self.args())
                        self.assertFalse((work / 'source/forwarded.txt').exists())
                    else:
                        expected_exit = bootstrap_exit or exit_code
                        self.assertEqual(release.upgrade(self.args(local=True, gateway_only=True, update=update, confirm_destructive=update, allow_known_claude_gap=True)), expected_exit)
                        mode = 'upgrade' if update else 'install'
                        receipt = json.loads((work / f'release-{mode}.json').read_text())
                        self.assertEqual(receipt['mode'], mode)
                        self.assertEqual(receipt['configuration'], 'portable-release-defaults')
                        self.assertTrue((work / 'source/portable-config-used').exists())
                        self.assertEqual((work / 'source/bootstrap.txt').exists(), not update)
                        if bootstrap_exit:
                            self.assertEqual(receipt['failed_stage'], 'bootstrap')
                            self.assertEqual(receipt['status'], 'failed')
                            self.assertFalse((work / 'source/forwarded.txt').exists())
                            continue
                        self.assertEqual(receipt['status'], 'complete' if exit_code == 0 else 'failed')
                        self.assertEqual(receipt['source_sha'], sha)
                        self.assertEqual((work / 'source/forwarded.txt').read_text().splitlines(),
                                         (['--update'] if update else []) + ['--env', 'dev', '--region', 'us-east-1'] + (['--confirm-destructive'] if update else []) + ['--allow-known-claude-gap', '--local', '--gateway-only'])
                        self.assertFalse((origin / 'forwarded.txt').exists())

    def test_wrapper_routing_and_invalid_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'deploy.sh').write_text((ROOT / 'deploy.sh').read_text())
            scripts = root / 'platform/scripts'
            scripts.mkdir(parents=True)
            (scripts / 'deploy-all.sh').write_text('printf "ARG:%s\\n" "$@"\nexit 19\n')
            (scripts / 'upgrade-github-release.py').write_text('import sys\nprint(repr(sys.argv[1:]))\nsys.exit(23)\n')
            def wrapper(*args):
                return subprocess.run(['bash', str(root / 'deploy.sh'), *args], text=True, capture_output=True)
            result = wrapper('--update', '--env', 'staging', '--region', 'us-west-2', '--skip-agents')
            self.assertEqual(result.returncode, 19, result.stderr)
            self.assertIn('ARG:--gateway-only', result.stdout)
            self.assertIn('ARG:staging', result.stdout)
            result = wrapper('--update', '--release', 'v1.2.0', '--dry-run')
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertIn("'--release', 'v1.2.0'", result.stdout)
            self.assertIn("'--dry-run'", result.stdout)
            self.assertIn("'--update'", result.stdout)
            result = wrapper('--release', 'v1.2.0', '--dry-run')
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertNotIn("'--update'", result.stdout)
            for args in (('--release',), ('--update', '--release', '--dry-run')):
                self.assertEqual(wrapper(*args).returncode, 2)
            self.assertEqual(wrapper('--update', '--dry-run').returncode, 0)

    def test_explicit_profile_reaches_all_paths_and_overrides_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'deploy.sh').write_text((ROOT / 'deploy.sh').read_text())
            scripts = root / 'platform/scripts'
            scripts.mkdir(parents=True)
            probe = 'import json, os\nfrom pathlib import Path\nPath(os.environ["PROBE_FILE"]).write_text(json.dumps(dict(os.environ)))\n'
            (scripts / 'upgrade-github-release.py').write_text(probe)
            (scripts / 'deploy-all.sh').write_text('python3 "$(dirname "$0")/upgrade-github-release.py"\n')
            binaries = root / 'bin'
            binaries.mkdir()
            for name in ('aws', 'terraform', 'kubectl', 'node'):
                stub = binaries / name
                stub.write_text('#!/bin/bash\npython3 "' + str(scripts / 'upgrade-github-release.py') + '"\nexit 1\n')
                stub.chmod(0o755)
            env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                       PROBE_FILE=str(root / 'probe.json'), AWS_PROFILE='inherited',
                       AWS_DEFAULT_PROFILE='other', AWS_ACCESS_KEY_ID='fake-key',
                       AWS_SECRET_ACCESS_KEY='fake-secret', AWS_SESSION_TOKEN='fake-token',
                       AWS_ROLE_ARN='fake-role', AWS_WEB_IDENTITY_TOKEN_FILE='fake-file')
            for path in ([], ['--update'], ['--update', '--release', 'v1.2.0'], ['--release', 'v1.2.0']):
                for explicit in (False, True):
                    args = ['--aws-profile', 'adp-customer-demo'] if explicit else []
                    subprocess.run(['bash', str(root / 'deploy.sh'), *args, *path],
                                   env=env, stdin=subprocess.DEVNULL, capture_output=True, check=False)
                    observed = json.loads((root / 'probe.json').read_text())
                    self.assertEqual(observed['AWS_PROFILE'], 'adp-customer-demo' if explicit else 'inherited')
                    self.assertEqual(observed['AWS_DEFAULT_PROFILE'], 'adp-customer-demo' if explicit else 'other')
                    for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                                'AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE'):
                        self.assertEqual(observed.get(key), None if explicit else env[key])
                    (root / 'probe.json').unlink()
            for value in ([], [''], ['--update']):
                result = subprocess.run(['bash', str(root / 'deploy.sh'), '--aws-profile', *value],
                                        env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn('--aws-profile requires a profile name', result.stderr)


if __name__ == '__main__':
    unittest.main()
