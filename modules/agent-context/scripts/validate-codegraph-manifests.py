#!/usr/bin/env python3
"""Exercise both rendered CodeGraph startup/probe/storage contracts on local Docker.

Requires PyYAML and a passed validate-codegraph-image.py receipt for this revision.
PVCs are represented by disposable tmpfs; this does not validate live PVC migration.
"""

import argparse
import hashlib
import io
import json
import os
import runpy
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
MODULE = Path('modules/agent-context')


def main():
    if not __debug__:
        raise RuntimeError('Acceptance validation requires assertions; do not use Python -O')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--image-receipt', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(['git', 'rev-parse', '--verify', args.revision + '^{commit}'], cwd=ROOT, text=True).strip()
    receipt = json.loads(args.image_receipt.read_text())
    assert receipt['revision'] == revision, 'image receipt must match requested source revision'
    paths = ['scripts/render-codegraph.py', 'manifests/codegraph.yaml',
             'kubernetes/codegraph-deployment.yaml', 'tests/container/codegraph_runtime.py']
    archive = subprocess.check_output(['git', 'archive', '--format=tar', revision, '--',
                                      *[str(MODULE / path) for path in paths]], cwd=ROOT)
    outcome = {'revision': revision, 'manifest_archive_sha256': hashlib.sha256(archive).hexdigest(),
               'image_id': receipt['image_id'], 'result': 'incomplete', 'variants': {}}
    result_path = args.output / 'receipt.json'
    result_path.write_text(json.dumps(outcome, indent=2) + '\n')
    with tempfile.TemporaryDirectory(prefix='codegraph-manifests-') as directory, (args.output / 'commands.log').open('w') as log:
        work = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(work, filter='data')
        config = work / 'docker-config'
        config.mkdir()
        env = {'PATH': os.environ['PATH'], 'HOME': str(work)}
        docker = ['docker', '--host', 'unix:///var/run/docker.sock', '--config', str(config)]

        def execute(command):
            log.write(json.dumps(command) + '\n')
            log.flush()
            result = subprocess.run(command, env=env, text=True, capture_output=True, check=False, timeout=180)
            log.write(result.stdout + result.stderr)
            log.flush()
            if result.returncode:
                raise RuntimeError(f'Command failed ({result.returncode}); see {args.output}/commands.log')
            return result.stdout

        metadata = json.loads(execute(docker + ['image', 'inspect', receipt['image_id']]))[0]
        labels = metadata['Config'].get('Labels') or {}
        assert labels['org.opencontainers.image.revision'] == revision
        assert labels['adp.validation.source-archive'] == receipt['source_archive_sha256']
        image = receipt['image_repo_digests'][0]
        assert image in metadata['RepoDigests'], 'requested identity must be an actual OCI repository digest'
        render = runpy.run_path(str(work / MODULE / 'scripts/render-codegraph.py'))['render']
        for variant in ['current', 'legacy']:
            rendered = render(image, args.image_receipt, 'fixture', 'fixture', variant)
            (args.output / f'{variant}.yaml').write_text(rendered)
            pod = next(doc for doc in yaml.safe_load_all(rendered) if doc['kind'] == 'Deployment')['spec']['template']['spec']
            container, = pod['containers']
            assert not pod.get('initContainers')
            security = pod['securityContext']
            boundary = container['securityContext']
            assert boundary['privileged'] is False and boundary['runAsNonRoot'] is True
            assert security['seccompProfile']['type'] == 'RuntimeDefault'
            assert pod['automountServiceAccountToken'] is False
            name = 'codegraph-manifest-' + uuid.uuid4().hex
            command = docker + ['run', '-d', '--name', name, '--network', 'none',
                                '--user', f"{security['runAsUser']}:{security['runAsGroup']}",
                                '--memory', '2g', '--pids-limit', '128', '--cpus', '2']
            if boundary['readOnlyRootFilesystem']:
                command += ['--read-only']
            if not boundary['allowPrivilegeEscalation']:
                command += ['--security-opt', 'no-new-privileges=true']
            for capability in boundary['capabilities']['drop']:
                command += ['--cap-drop', capability]
            for mount in container['volumeMounts']:
                # Fresh fixtures only: no host/PVC content is mounted.
                command += ['--tmpfs', f"{mount['mountPath']}:rw,nosuid,nodev,size=256m,uid={security['runAsUser']},gid={security['runAsGroup']},mode=0700"]
            for variable in container['env']:
                if 'value' in variable:
                    command += ['-e', f"{variable['name']}={variable['value']}"]
            command += ['--mount', f"type=bind,source={work / MODULE / 'tests/container/codegraph_runtime.py'},target=/validation.py,readonly",
                        container['image'], *container['command'], *container.get('args', [])]
            try:
                execute(command)
                for probe in ['readinessProbe', 'livenessProbe']:
                    execute(docker + ['exec', name, *container[probe]['exec']['command']])
                execute(docker + ['exec', name, 'python', '/validation.py'])
                outcome['variants'][variant] = {'result': 'passed', 'rendered_sha256': hashlib.sha256(rendered.encode()).hexdigest()}
            finally:
                execute(docker + ['rm', '-f', name])
        outcome['result'] = 'passed'
        result_path.write_text(json.dumps(outcome, indent=2) + '\n')
        print(json.dumps(outcome, indent=2))


if __name__ == '__main__':
    main()
