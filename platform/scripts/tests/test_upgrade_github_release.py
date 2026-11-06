"""Offline release selection and wrapper routing tests; no AWS mutations."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile
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

    def test_factory_scope_refuses_fresh_or_conflicting_scope_before_aws(self):
        with patch.object(release, 'resolve') as resolve:
            for values in ({'update': False}, {'gateway_only': True}):
                with self.assertRaisesRegex(ValueError, 'agent-factory-only requires'):
                    release.upgrade(self.args(agent_factory_only=True, **values))
            resolve.assert_not_called()

    def test_dev_box_scope_refuses_invalid_combinations_before_aws(self):
        with patch.object(release, 'resolve') as resolve:
            for values in ({'update': False}, {'gateway_only': True},
                           {'agent_factory_only': True}, {'dev_box': 'off'}):
                with self.assertRaisesRegex(ValueError, 'dev-box-only'):
                    release.upgrade(self.args(dev_box_only=True, **values))
            resolve.assert_not_called()

    def test_selected_fresh_release_checks_prerequisites_before_backend_mutation(self):
        # Use the production release checkout, configuration preparation,
        # deploy-all and administrator gate. Double only network services and
        # preflight's external dependency check; log every AWS child invocation.
        for preflight_exit in (0, 23):
            with self.subTest(preflight_exit=preflight_exit), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                origin = directory / 'origin'
                scripts = origin / 'platform/scripts'
                scripts.mkdir(parents=True)
                for name in ('deploy-all.sh', 'bootstrap.sh', 'deploy-prerequisites.sh',
                             'gateway-rollout.sh', 'load-deploy-config.sh', 'terraform-update.sh',
                             'upgrade-scope.sh', 'gateway-alb-vars.sh', 'deploy-checkpoints.sh',
                             'deploy-checkpoints.py', 'prepare-backends.py', 'prepare-release-config.py'):
                    shutil.copy(ROOT / 'platform/scripts' / name, scripts / name)
                (scripts / 'preflight-check.sh').write_text(f'exit {preflight_exit}\n')
                admin = origin / 'modules/gateway/scripts/bootstrap-admin.py'
                admin.parent.mkdir(parents=True)
                shutil.copy(ROOT / 'modules/gateway/scripts/bootstrap-admin.py', admin)
                shutil.copytree(ROOT / 'config/release-defaults', origin / 'config/release-defaults')
                environment = origin / 'environments/dev'
                environment.mkdir(parents=True)
                (environment / 'backend.tfvars').write_text('bucket = "adp-terraform-state-ACCOUNT_ID"\n')

                def git(*args):
                    return subprocess.check_output(['git', *args], cwd=origin, text=True).strip()

                git('init', '--quiet')
                git('config', 'user.email', 'test@example.com')
                git('config', 'user.name', 'Test')
                git('add', '.')
                git('commit', '--quiet', '-m', 'fresh release validation fixture')
                git('tag', 'v1.2.0')
                sha = git('rev-parse', 'HEAD')
                work = directory / 'work'
                work.mkdir()
                binaries = directory / 'bin'
                binaries.mkdir()
                calls = directory / 'aws-calls.jsonl'
                aws = binaries / 'aws'
                aws.write_text('#!/usr/bin/env python3\n'
                               'import json, os, sys\n'
                               'with open(os.environ["AWS_CALLS"], "a") as out:\n'
                               '    out.write(json.dumps(sys.argv[1:]) + "\\n")\n'
                               'if sys.argv[1:3] == ["sts", "get-caller-identity"]:\n'
                               '    print("123456789012")\n'
                               'elif sys.argv[1:3] in (["s3api", "head-bucket"], ["dynamodb", "describe-table"]):\n'
                               '    sys.exit(1)\n')
                aws.chmod(0o755)
                original_run = release.run
                original_subprocess_run = subprocess.run
                deployment_output = []

                def capture_deployment(command, **kwargs):
                    if command[:1] == ['bash'] and command[1].endswith('/deploy-all.sh'):
                        kwargs.update(capture_output=True, text=True)
                        result = original_subprocess_run(command, **kwargs)
                        deployment_output.append(result.stdout + result.stderr)
                        return result
                    return original_subprocess_run(command, **kwargs)

                def fake_services(command, **kwargs):
                    if command[0] == 'aws':
                        return json.dumps({'Account': '123456789012', 'Arn': 'caller'})
                    if 'fetch' in command:
                        command = ['git', 'fetch', '--no-tags', str(origin), 'refs/tags/v1.2.0']
                    return original_run(command, **kwargs)

                environment = dict(PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                                   AWS_CALLS=str(calls), ADP_BOOTSTRAP_ADMIN_EMAIL='',
                                   ADP_BOOTSTRAP_USE_TEST_ADMIN='false', ADP_DEV_BOX='off')
                with patch.dict(os.environ, environment, clear=True), \
                     patch.object(release, 'resolve', return_value=({'id': 1}, sha)), \
                     patch.object(release, 'run', side_effect=fake_services), \
                     patch.object(release.subprocess, 'run', side_effect=capture_deployment), \
                     patch.object(release.tempfile, 'mkdtemp', return_value=str(work)):
                    self.assertNotEqual(release.upgrade(self.args(update=False)), 0)
                self.assertEqual(len(deployment_output), 1)
                self.assertIn('Preflight checks failed' if preflight_exit else 'Select ADP_BOOTSTRAP_ADMIN_EMAIL',
                              deployment_output[0])
                commands = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertTrue(commands)
                self.assertTrue(all(command[:2] == ['sts', 'get-caller-identity'] for command in commands), commands)
                receipt = json.loads((work / 'release-install.json').read_text())
                self.assertEqual(receipt['status'], 'failed')
                self.assertEqual(receipt['source_sha'], sha)

    def test_public_release_launcher_forwards_module_options_to_selected_source(self):
        # Exercise both real argument parsers, the release checkout and its
        # child invocation. Only remote Git/GitHub/AWS and deployment are doubled.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launcher = root / 'launcher'
            scripts = launcher / 'platform/scripts'
            scripts.mkdir(parents=True)
            (launcher / 'deploy.sh').write_text((ROOT / 'deploy.sh').read_text())
            for name in ('deploy-prerequisites.sh', 'upgrade-github-release.py'):
                (scripts / name).write_text((ROOT / 'platform/scripts' / name).read_text())
            origin = root / 'origin'
            release_scripts = origin / 'platform/scripts'
            release_scripts.mkdir(parents=True)
            (release_scripts / 'prepare-release-config.py').write_text('pass\n')
            (release_scripts / 'bootstrap.sh').write_text('exit 0\n')
            (release_scripts / 'deploy-all.sh').write_text(
                '# --update) --confirm-destructive) --allow-known-claude-gap) --agent-factory-only)\n'
                '# --chat) --chat-exception) --dev-box) --dev-box-only)\n'
                'printf "%s\\0" "$@" > "$FORWARDED_FILE"\n')
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=origin, text=True).strip()
            git('init', '--quiet')
            git('config', 'user.email', 'test@example.com')
            git('config', 'user.name', 'Test')
            git('add', '.')
            git('commit', '--quiet', '-m', 'release option fixture')
            git('tag', 'v1.2.0')
            sha = git('rev-parse', 'HEAD')
            binaries = root / 'bin'
            binaries.mkdir()
            (binaries / 'git').write_text(
                '#!/usr/bin/env python3\nimport os, sys\nargs = sys.argv[1:]\n'
                'if "fetch" in args:\n'
                '    args = ["fetch", "--no-tags", os.environ["FIXTURE_ORIGIN"], "refs/tags/v1.2.0"]\n'
                'os.execv(os.environ["REAL_GIT"], ["git", *args])\n')
            (binaries / 'gh').write_text(
                '#!/usr/bin/env python3\nimport json, os, sys\n'
                'if sys.argv[1:] == ["api", "repos/aws-e/adp/releases/tags/v1.2.0"]:\n'
                '    print(json.dumps({"id": 1, "tag_name": "v1.2.0", "published_at": "today"}))\n'
                'elif sys.argv[1:] == ["api", "repos/aws-e/adp/commits/v1.2.0"]:\n'
                '    print(json.dumps({"sha": os.environ["FIXTURE_SHA"]}))\n'
                'else:\n    sys.exit("Unexpected GitHub command")\n')
            (binaries / 'aws').write_text(
                '#!/usr/bin/env python3\nimport json, sys\n'
                'assert sys.argv[1:] == ["sts", "get-caller-identity", "--output", "json"]\n'
                'print(json.dumps({"Account": "123", "Arn": "caller"}))\n')
            for path in binaries.iterdir():
                path.chmod(0o755)
            cases = [
                (True, ['--dev-box', 'off', '--chat', 'retire'], 'full'),
                (True, ['--chat', 'delegated', '--chat-exception', '#6937 reviewed evidence', '--dev-box', 'on'], 'full'),
                (True, ['--chat', 'keep-legacy'], 'full'),
                (True, ['--dev-box-only'], 'dev-box-only'),
                (True, ['--agent-factory-only', '--confirm-destructive', '--allow-known-claude-gap', '--dev-box', 'off'], 'agent-factory-only'),
                (False, ['--dev-box', 'off', '--chat', 'retire'], 'full'),
            ]
            for index, (update, options, scope) in enumerate(cases):
                with self.subTest(update=update, options=options):
                    case_directory = root / str(index)
                    case_directory.mkdir()
                    forwarded = case_directory / 'forwarded'
                    env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                               TMPDIR=str(case_directory), REAL_GIT=shutil.which('git'),
                               FIXTURE_ORIGIN=str(origin), FIXTURE_SHA=sha, FORWARDED_FILE=str(forwarded))
                    result = subprocess.run(
                        ['bash', str(launcher / 'deploy.sh'), '--release', 'v1.2.0',
                         '--env', 'staging', '--region', 'us-west-2',
                         *(['--update'] if update else []), *options],
                        env=env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    observed = forwarded.read_text().rstrip('\0').split('\0')
                    # Compare pairs so option ordering cannot hide lost values
                    # or split an exception reference that contains spaces.
                    flag_values = {}
                    iterator = iter(observed)
                    for flag in iterator:
                        flag_values[flag] = next(iterator) if flag in {
                            '--env', '--region', '--chat', '--chat-exception', '--dev-box'} else True
                    expected = {'--env': 'staging', '--region': 'us-west-2'}
                    if update:
                        expected['--update'] = True
                    iterator = iter(options)
                    for flag in iterator:
                        expected[flag] = next(iterator) if flag in {
                            '--chat', '--chat-exception', '--dev-box'} else True
                    self.assertEqual(flag_values, expected)
                    mode = 'upgrade' if update else 'install'
                    receipt_path, = case_directory.glob(f'adp-github-release-*/release-{mode}.json')
                    receipt = json.loads(receipt_path.read_text())
                    self.assertEqual(receipt['scope'], scope)
                    self.assertEqual(receipt['status'], 'complete')

    def test_restrictive_umask_release_build_uses_committed_source_permissions(self):
        # Exercise the real release checkout and CodeBuild packager. Only AWS
        # and remote Git are doubled; the uploaded zip is the independent oracle.
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            origin = directory / 'origin'
            scripts = origin / 'platform/scripts'
            scripts.mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=origin, text=True).strip()
            git('init', '--quiet')
            git('config', 'user.email', 'test@example.com')
            git('config', 'user.name', 'Test')
            for name in ('codebuild-run.sh', 'zip-source.sh'):
                (scripts / name).write_text((ROOT / 'platform/scripts' / name).read_text())
            (scripts / 'prepare-release-config.py').write_text(
                'from pathlib import Path\nPath("environments/generated.tfvars").write_text("private override")\n')
            (scripts / 'bootstrap.sh').write_text('exit 0\n')
            (scripts / 'deploy-all.sh').write_text(
                '# --update)\nset -eu\n'
                'printf modified > modules/package.py\n'
                'cp modules/package.py modules/packaged-handler.py\n'
                'mkdir modules/build-output\n'
                'printf dependency > modules/build-output/dependency.py\n'
                'STATE_BUCKET=example-state bash platform/scripts/codebuild-run.sh example-build\n')
            for name in ('modules', 'environments', 'libs'):
                (origin / name).mkdir()
            (origin / 'modules/package.py').write_text('COMMITTED = True\n')
            executable = origin / 'modules/entrypoint.sh'
            executable.write_text('#!/bin/sh\nexit 0\n')
            executable.chmod(0o755)
            (origin / 'environments/.keep').touch()
            (origin / 'libs/.keep').touch()
            git('add', '.')
            git('commit', '--quiet', '-m', 'release fixture')
            git('tag', 'v1.2.0')
            sha = git('rev-parse', 'HEAD')
            binaries = directory / 'bin'
            binaries.mkdir()
            aws = binaries / 'aws'
            aws.write_text('''#!/usr/bin/env python3
import json, os, shutil, sys
args = sys.argv[1:]
if args[:2] == ['s3', 'cp']:
    shutil.copyfile(args[2], os.environ['UPLOADED_ARCHIVE'])
elif args[:2] == ['codebuild', 'start-build']:
    print('example-build:fixture')
elif args[:2] == ['codebuild', 'batch-get-builds']:
    print(json.dumps({'s': 'SUCCEEDED', 'p': 'COMPLETED'}))
else:
    sys.exit('Unexpected AWS command')
''')
            aws.chmod(0o755)
            original_run = release.run
            def fake_services(command, **kwargs):
                if command[0] == 'aws':
                    return json.dumps({'Account': '123', 'Arn': 'caller'})
                if 'fetch' in command:
                    command = ['git', 'fetch', '--no-tags', str(origin), 'refs/tags/v1.2.0']
                return original_run(command, **kwargs)
            for update in (False, True):
                with self.subTest(update=update):
                    work = directory / ('upgrade' if update else 'install')
                    work.mkdir(mode=0o700)
                    archive = work / 'uploaded.zip'
                    with patch.object(release, 'resolve', return_value=({'id': 1}, sha)), \
                         patch.object(release, 'run', side_effect=fake_services), \
                         patch.object(release.tempfile, 'mkdtemp', return_value=str(work)), \
                         patch.dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                                    UPLOADED_ARCHIVE=str(archive), ADP_RELEASE_BUILD='false'):
                        previous = os.umask(0o077)
                        try:
                            self.assertEqual(release.upgrade(self.args(update=update)), 0)
                            self.assertEqual(os.umask(0o077), 0o077)
                        finally:
                            os.umask(previous)
                    for path in ('modules/package.py', 'modules/packaged-handler.py',
                                 'modules/build-output/dependency.py'):
                        self.assertEqual((work / 'source' / path).stat().st_mode & 0o777, 0o644)
                    self.assertEqual((work / 'source/modules/build-output').stat().st_mode & 0o777, 0o755)
                    self.assertEqual((work / 'source/environments/generated.tfvars').stat().st_mode & 0o777, 0o600)
                    self.assertEqual(work.stat().st_mode & 0o777, 0o700)
                    with zipfile.ZipFile(archive) as bundle:
                        self.assertEqual(bundle.read('modules/package.py'), b'COMMITTED = True\n')
                        self.assertNotIn('environments/generated.tfvars', bundle.namelist())
                    # Git encodes regular files using zip's portable default,
                    # so check the real extracted modes, not only Unix attrs.
                    extracted = work / 'codebuild-source'
                    subprocess.run(['unzip', '-q', str(archive), '-d', str(extracted)],
                                   check=True, umask=0o022)
                    self.assertEqual((extracted / 'modules/package.py').stat().st_mode & 0o777, 0o644)
                    self.assertEqual((extracted / 'modules/entrypoint.sh').stat().st_mode & 0o777, 0o755)
                    mode = 'upgrade' if update else 'install'
                    self.assertEqual((work / f'release-{mode}.json').stat().st_mode & 0o777, 0o600)

    def test_real_checkout_success_failure_and_moved_tag(self):
        # Use real git and a harmless deployment stand-in, including an annotated tag.
        for exit_code, moved, update in ((0, False, True), (17, False, True), (0, True, True), (0, False, False), (17, False, False)):
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
                (script.parent / 'bootstrap.sh').write_text('printf duplicate > duplicate-bootstrap.txt\nexit 12\n')
                script.write_text('#!/bin/bash\n# --update) --confirm-destructive) --allow-known-claude-gap) --agent-factory-only)\nprintf "%s\\n" "$@" > forwarded.txt\nexit ' + str(exit_code) + '\n')
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
                        factory_only = update and exit_code == 0
                        self.assertEqual(release.upgrade(self.args(local=True, gateway_only=not factory_only, agent_factory_only=factory_only, update=update, confirm_destructive=update, allow_known_claude_gap=True)), exit_code)
                        mode = 'upgrade' if update else 'install'
                        receipt = json.loads((work / f'release-{mode}.json').read_text())
                        self.assertEqual(receipt['scope'], 'agent-factory-only' if factory_only else 'gateway-only')
                        self.assertEqual(receipt['mode'], mode)
                        self.assertEqual(receipt['configuration'], 'portable-release-defaults')
                        self.assertTrue((work / 'source/portable-config-used').exists())
                        self.assertFalse((work / 'source/duplicate-bootstrap.txt').exists())
                        self.assertEqual(receipt['status'], 'complete' if exit_code == 0 else 'failed')
                        self.assertEqual(receipt['source_sha'], sha)
                        self.assertEqual((work / 'source/forwarded.txt').read_text().splitlines(),
                                         (['--update'] if update else []) + ['--env', 'dev', '--region', 'us-east-1'] + (['--confirm-destructive'] if update else []) + ['--allow-known-claude-gap', '--local', '--agent-factory-only' if factory_only else '--gateway-only'])
                        self.assertFalse((origin / 'forwarded.txt').exists())

    def test_wrapper_routing_and_invalid_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'deploy.sh').write_text((ROOT / 'deploy.sh').read_text())
            scripts = root / 'platform/scripts'
            scripts.mkdir(parents=True)
            (scripts / 'deploy-prerequisites.sh').write_text((ROOT / 'platform/scripts/deploy-prerequisites.sh').read_text())
            (scripts / 'deploy-all.sh').write_text('printf "ARG:%s\\n" "$@"\nexit 19\n')
            (scripts / 'upgrade-github-release.py').write_text('import sys\nprint(repr(sys.argv[1:]))\nsys.exit(23)\n')
            def wrapper(*args):
                return subprocess.run(['bash', str(root / 'deploy.sh'), *args], text=True, capture_output=True)
            result = wrapper('--update', '--env', 'staging', '--region', 'us-west-2', '--skip-agents')
            self.assertEqual(result.returncode, 19, result.stderr)
            self.assertIn('ARG:--gateway-only', result.stdout)
            self.assertIn('ARG:staging', result.stdout)
            result = wrapper('--update', '--agent-factory-only')
            self.assertEqual(result.returncode, 19, result.stderr)
            self.assertIn('ARG:--agent-factory-only', result.stdout)
            for args in (('--agent-factory-only',), ('--update', '--agent-factory-only', '--skip-agents')):
                self.assertEqual(wrapper(*args).returncode, 2)
            result = wrapper('--update', '--agent-factory-only', '--release', 'v1.2.0')
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertIn("'--agent-factory-only'", result.stdout)
            for args in (('--reuse-images-from',), ('--reuse-images-from', 'source'), ('--update', '--reuse-images-from', 'source')):
                self.assertEqual(wrapper(*args).returncode, 2)
            result = wrapper('--update', '--release', 'v1.2.0', '--reuse-images-from', 'source')
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertIn("'--reuse-images-from', 'source'", result.stdout)
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
            (scripts / 'deploy-prerequisites.sh').write_text((ROOT / 'platform/scripts/deploy-prerequisites.sh').read_text())
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
