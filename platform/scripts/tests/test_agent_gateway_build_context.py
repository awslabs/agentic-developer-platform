"""Execute all agent-gateway build paths with an offline Docker contract double."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / 'modules/agent-factory'
BUILDSPEC = yaml.safe_load((ROOT / 'codebuild/bs-agent-gateway.yml').read_text())

DOCKER = r'''#!/usr/bin/env python3
import json, os, shlex, sys
from pathlib import Path
args = sys.argv[1:]
record = Path(os.environ['BUILD_TEST_RECORD'])
if args[0] == 'login':
    sys.stdin.read()
elif args[0] == 'build':
    context = Path(args[-1]).resolve()
    dockerfile = Path(args[args.index('-f') + 1]).resolve() if '-f' in args else context / 'Dockerfile'
    assert context == Path(os.environ['MODULE_ROOT']).resolve(), 'Unexpected Docker context'
    inputs = []
    for line in dockerfile.read_text().splitlines():
        if not line.startswith('COPY '):
            continue
        words = shlex.split(line)
        if words and words[0] == 'COPY':
            for source in words[1:-1]:
                path = context / source
                assert path.exists(), 'Missing Docker COPY input: ' + source
                inputs.append(source)
    assert (context / 'rules/personas/developer.md').is_file(), 'Shared personas are missing'
    with record.open('a') as out:
        out.write(json.dumps({'build': args[args.index('-t') + 1], 'inputs': inputs}) + '\n')
    if os.environ.get('BUILD_TEST_FAIL') == 'true':
        sys.exit(41)
elif args[0] == 'push':
    with record.open('a') as out:
        out.write(json.dumps({'push': args[1]}) + '\n')
'''


def build_commands(path):
    if path == 'codebuild':
        return '\n'.join(BUILDSPEC['phases']['build']['commands'])
    if path == 'full_upgrade':
        source = (ROOT / 'platform/scripts/deploy-all.sh').read_text()
        source = source[source.index('# --- Agent Gateway build + deploy'):]
        start = source.index('then\n') + len('then\n')
        return source[start:source.index('\n  else', start)]
    source = (MODULE / 'scripts/deploy-gateway.sh').read_text()
    return source[source.index('# Step 2: Docker build'):source.index('# Step 3: K8s')]


class AgentGatewayBuildTests(unittest.TestCase):
    def execute(self, path, fail=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            module = root / 'modules/agent-factory'
            (module / 'gateway').mkdir(parents=True)
            shutil.copytree(MODULE / 'gateway/app', module / 'gateway/app')
            shutil.copytree(MODULE / 'rules/personas', module / 'rules/personas')
            for name in ('Dockerfile', 'Dockerfile.dockerignore', 'entrypoint.sh'):
                shutil.copyfile(MODULE / 'gateway' / name, module / 'gateway' / name)
            binaries = root / 'bin'
            binaries.mkdir()
            stubs = {
                'docker': DOCKER,
                'aws': '#!/bin/sh\nif [ "$1 $2" = "sts get-caller-identity" ]; then echo 111122223333; fi\n',
                # A reintroduced shared staging directory must fail without
                # deleting or overwriting anything outside this throwaway tree.
                'rm': '#!/bin/sh\nexit 97\n',
                'cp': '#!/bin/sh\nexit 97\n',
            }
            for name, body in stubs.items():
                tool = binaries / name
                tool.write_text(body)
                tool.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if not k.startswith('AWS_')}
            registry = '111122223333.dkr.ecr.us-east-1.amazonaws.com'
            record = root / 'calls.jsonl'
            env.update(PATH=str(binaries) + os.pathsep + env['PATH'], ROOT_DIR=str(root), MODULE_ROOT=str(module),
                       AWS_REGION='us-east-1', REGISTRY=registry, ECR_REPO='adp-agent-gateway',
                       ECR_REPO_NAME='adp-agent-gateway', IMAGE_TAG='release-sha', LOCAL_IMAGE_TAG='release-sha',
                       AGENT_IMAGE_TAG='release-sha', SKIP_IMG='false', DRY_RUN='false',
                       BUILD_TEST_RECORD=str(record), BUILD_TEST_FAIL=str(fail).lower())
            result = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + build_commands(path)],
                                    cwd=root, env=env, text=True, capture_output=True)
            calls = [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
            return result, calls, registry + '/adp-agent-gateway:release-sha'

    def assert_build(self, path):
        result, calls, image = self.execute(path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls[0]['build'], image)
        self.assertIn('rules/personas/', calls[0]['inputs'])
        self.assertIn({'push': image}, calls)

    def assert_failed_build_stops_push(self, path):
        result, calls, _ = self.execute(path, fail=True)
        self.assertEqual(result.returncode, 41, result.stdout + result.stderr)
        self.assertTrue(calls)
        self.assertFalse(any('push' in call for call in calls))

    def test_codebuild_context(self):
        self.assert_build('codebuild')

    def test_full_upgrade_local_context(self):
        self.assert_build('full_upgrade')

    def test_standalone_context(self):
        self.assert_build('standalone')

    def test_codebuild_failure_stops_push(self):
        self.assert_failed_build_stops_push('codebuild')

    def test_full_upgrade_failure_stops_push(self):
        self.assert_failed_build_stops_push('full_upgrade')

    def test_standalone_failure_stops_push(self):
        self.assert_failed_build_stops_push('standalone')

    def test_failed_codebuild_does_not_report_success(self):
        commands = '\n'.join(BUILDSPEC['phases']['post_build']['commands'])
        result = subprocess.run(['bash', '-c', commands], env=dict(os.environ, CODEBUILD_BUILD_SUCCEEDING='0'),
                                text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Pushed ', result.stdout)

    def test_successful_codebuild_reports_publication(self):
        commands = '\n'.join(BUILDSPEC['phases']['post_build']['commands'])
        result = subprocess.run(['bash', '-c', commands], env=dict(os.environ, CODEBUILD_BUILD_SUCCEEDING='1',
                                REGISTRY='example.test', ECR_REPO='worker', IMAGE_TAG='release'),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('Pushed example.test/worker:release', result.stdout)


if __name__ == '__main__':
    unittest.main()
