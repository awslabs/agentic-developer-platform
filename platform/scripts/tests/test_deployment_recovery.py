"""Offline regression coverage for the public deployment entry point and resume."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / 'platform/scripts'


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


checkpoints = module('checkpoints', 'deploy-checkpoints.py')
tfvars = module('tfvars', 'tfvars-account-check.py')


class TfvarsTests(unittest.TestCase):
    def test_comment_forms_and_real_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.tfvars'
            for comment in ('# 222222222222', '// 222222222222', '/*\n222222222222\n*/'):
                path.write_text(comment + '\nrole = "arn:aws:iam::111111111111:role/test"\n')
                self.assertEqual(tfvars.check(path, '111111111111'), path)
            for value in ('"https://example/#222222222222"', '"https://example//222222222222"',
                          '"/*222222222222*/"', '"escaped \\" #222222222222"',
                          '<<EOF\n#222222222222\nEOF', '<<-EOF\n//222222222222\n  EOF',
                          '"${join("#", ["arn:aws:iam::222222222222:role/test"])}"',
                          '222222222222'):
                path.write_text('value = ' + value)
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'different AWS account'):
                    tfvars.check(path, '111111111111')

    def test_escaped_template_openers_are_literals(self):
        for value in ('"$${literal"', '"%%{literal"'):
            self.assertEqual(tfvars.values_without_comments(value), value)

    def test_real_shell_helper_ignores_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.tfvars'
            path.write_text('# documentation 222222222222\nregion="us-east-1"\n')
            result = subprocess.run(['bash', '-c', 'source "$1"; terraform_update_var_file "$2" "" 111111111111',
                                     'test', str(SCRIPTS / 'terraform-update.sh'), str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class CheckpointTests(unittest.TestCase):
    def test_resume_failure_and_target_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            binding = dict(account='111111111111', source='abc', region='us-east-1', options='full')
            checkpoints.initialize(path, binding)
            state = json.loads(path.read_text())
            state['phases']['bootstrap'] = 'complete'
            state['phases']['platform'] = 'failed'
            checkpoints.save(path, state)
            checkpoints.initialize(path, binding, resume=True)
            resumed = json.loads(path.read_text())
            self.assertEqual(resumed['phases']['bootstrap'], 'complete')
            self.assertEqual(resumed['phases']['platform'], 'pending')
            for key in binding:
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'changed'):
                    checkpoints.initialize(path, dict(binding, **{key: 'different'}), resume=True)
            with self.assertRaisesRegex(ValueError, 'incomplete prerequisite'):
                checkpoints.initialize(path, binding, resume=True, start='gateway')
            checkpoints.initialize(path, binding, resume=True, start='bootstrap')
            self.assertTrue(all(v == 'pending' for v in json.loads(path.read_text())['phases'].values()))

    def test_real_shell_records_failure_and_does_not_repeat_completed_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            checkpoints.initialize(path, {'test': True})
            shell = '''set -euo pipefail
SCRIPT_DIR="$1"
DEPLOY_CHECKPOINT_FILE="$2"
source "$SCRIPT_DIR/deploy-checkpoints.sh"
DEPLOY_ACTIVE_PHASE=""
trap 'deploy_checkpoint_exit $?' EXIT
if deploy_phase_begin bootstrap; then
  echo bootstrap >> "$3"
  deploy_phase_complete
fi
if deploy_phase_begin platform; then
  echo platform >> "$3"
  if [ "$4" = fail ]; then false; fi
  deploy_phase_complete
fi
'''
            log = Path(directory) / 'calls'
            args = ['bash', '-c', shell, 'test', str(SCRIPTS), str(path), str(log)]
            failed = subprocess.run([*args, 'fail'], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(json.loads(path.read_text())['phases']['platform'], 'failed')
            checkpoints.initialize(path, {'test': True}, resume=True)
            passed = subprocess.run([*args, 'pass'], capture_output=True, text=True)
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertEqual(log.read_text().splitlines(), ['bootstrap', 'platform', 'platform'])


class EntrypointTests(unittest.TestCase):
    def test_update_flags_reach_orchestrator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(ROOT / 'deploy.sh', root / 'deploy.sh')
            target = root / 'platform/scripts/deploy-all.sh'
            target.parent.mkdir(parents=True)
            target.write_text('printf "%s\\n" "$@"\n')
            result = subprocess.run(['bash', str(root / 'deploy.sh'), '--update', '--confirm-destructive',
                                     '--allow-known-claude-gap', '--from', 'webhook', '--skip-agents',
                                     '--env', 'prod', '--region', 'eu-west-1'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = result.stdout.splitlines()
            for flag in ('--update', '--confirm-destructive', '--allow-known-claude-gap', '--from',
                         'webhook', '--gateway-only', 'prod', 'eu-west-1'):
                self.assertIn(flag, args)

    def test_fresh_resume_does_not_run_wrapper_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy(ROOT / 'deploy.sh', root / 'deploy.sh')
            target = root / 'platform/scripts/deploy-all.sh'
            target.parent.mkdir(parents=True)
            target.write_text('printf "%s\\n" "$@"\n')
            result = subprocess.run(['bash', str(root / 'deploy.sh'), '--resume'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--resume', result.stdout.splitlines())
            self.assertNotIn('--update', result.stdout.splitlines())
            self.assertNotIn('--gateway-only', result.stdout.splitlines())

    def test_fresh_install_routes_all_components_once(self):
        for skip in (False, True):
            with self.subTest(skip=skip), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copy(ROOT / 'deploy.sh', root / 'deploy.sh')
                scripts = root / 'platform/scripts'
                scripts.mkdir(parents=True)
                (scripts / 'deploy-all.sh').write_text('printf "ARG:%s\\n" "$@"; exit 19\n')
                (root / 'environments/dev/modules').mkdir(parents=True)
                (root / 'environments/dev/platform.tfvars').touch()
                (root / 'environments/dev/modules/gateway.tfvars').touch()
                tools = root / 'bin'
                tools.mkdir()
                for tool in ('aws', 'terraform', 'kubectl', 'node'):
                    body = '#!/bin/bash\n'
                    if tool == 'aws':
                        body += 'case "$1 $2" in "sts get-caller-identity") echo 111111111111 ;; esac\n'
                    else:
                        body += 'exit 0\n'
                    (tools / tool).write_text(body)
                    (tools / tool).chmod(0o755)
                result = subprocess.run(['bash', str(root / 'deploy.sh'), '--region', 'eu-west-1',
                                         '--allow-known-claude-gap', *(['--skip-agents'] if skip else [])],
                                        env=dict(os.environ, PATH=f"{tools}:{os.environ['PATH']}"),
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True)
                self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
                self.assertIn('ARG:--allow-known-claude-gap', result.stdout)
                self.assertIn('ARG:eu-west-1', result.stdout)
                self.assertEqual('ARG:--gateway-only' in result.stdout, skip)

    def test_destructive_confirmation_requires_update(self):
        result = subprocess.run(['bash', str(ROOT / 'deploy.sh'), '--confirm-destructive', '--dry-run'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('requires --update', result.stderr)


if __name__ == '__main__':
    unittest.main()
