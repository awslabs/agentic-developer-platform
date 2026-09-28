#!/usr/bin/env python3
"""Build an exact Git revision and exercise CodeGraph in disposable offline containers.

Requires local Docker; never pushes images or forwards host credentials. The
receipt identifies the source archive and image, not deployed runtime acceptance.
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULE = Path('modules/agent-context')


def main():
    if not __debug__:
        raise RuntimeError('Acceptance validation requires assertions; do not use Python -O')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(
        ['git', 'rev-parse', '--verify', args.revision + '^{commit}'], cwd=ROOT, text=True
    ).strip()
    archive = subprocess.check_output(
        ['git', 'archive', '--format=tar', revision, '--',
         str(MODULE / 'images/codegraph-context'),
         str(MODULE / 'images/shared'),
         'modules/gateway/security/stdlib',
         str(MODULE / 'tests/container/codegraph_runtime.py')], cwd=ROOT
    )
    digest = hashlib.sha256(archive).hexdigest()
    receipt = {'fixture': 'codegraph-runtime-v1', 'revision': revision, 'source_archive_sha256': digest, 'result': 'incomplete'}
    receipt_path = args.output / 'receipt.json'
    receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
    with tempfile.TemporaryDirectory(prefix='codegraph-gate-') as directory, (
        args.output / 'commands.log'
    ).open('w') as log:
        work = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(work / 'source', filter='data')
        config = work / 'docker-config'
        config.mkdir()
        docker = ['docker', '--host', 'unix:///var/run/docker.sock', '--config', str(config)]
        env = {'PATH': os.environ['PATH'], 'HOME': str(work), 'DOCKER_BUILDKIT': '1'}

        def execute(command):
            log.write(json.dumps(command) + '\n')
            log.flush()
            result = subprocess.run(command, check=False, env=env, text=True, stdout=subprocess.PIPE, stderr=log)
            log.write(result.stdout)
            log.flush()
            if result.returncode:
                raise RuntimeError(f'Command failed ({result.returncode}); see {args.output}/commands.log')
            return result.stdout

        image = f'adp-codegraph-validation:{revision[:12]}-{uuid.uuid4().hex[:12]}'
        source = work / 'source' / MODULE
        shutil.copytree(work / 'source/modules/gateway/security/stdlib', source / 'images/codegraph-context/security-stdlib')
        if (source / 'images/shared').is_dir():
            shutil.copytree(source / 'images/shared', source / 'images/codegraph-context/security-build')
        execute(docker + ['build', '--label', f'org.opencontainers.image.revision={revision}',
                         '--label', f'adp.validation.source-archive={digest}', '-t', image,
                         str(source / 'images/codegraph-context')])
        metadata = json.loads(execute(docker + ['image', 'inspect', image]))[0]
        labels = metadata['Config'].get('Labels') or {}
        assert labels.get('org.opencontainers.image.revision') == revision
        assert labels.get('adp.validation.source-archive') == digest
        receipt['image_id'] = metadata['Id']
        receipt['image_repo_digests'] = metadata.get('RepoDigests', [])
        (args.output / 'image-inspect.json').write_text(json.dumps(metadata, indent=2) + '\n')
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        command = docker + [
            'run', '--rm', '--network', 'none', '--read-only', '--user', '10001:10001',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true',
            '--pids-limit', '128', '--memory', '2g', '--cpus', '2',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,mode=1777',
            '--tmpfs', '/data:rw,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700',
            '--mount', f'type=bind,source={source / "tests/container/codegraph_runtime.py"},target=/validation.py,readonly',
            '-e', 'AWS_EC2_METADATA_DISABLED=true',
            metadata['Id'], 'python', '/validation.py',
        ]
        output = execute(command)
        (args.output / 'runtime.log').write_text(output)
        negative = command.copy()
        data_mount = negative.index('/data:rw,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700')
        del negative[data_mount - 1:data_mount + 1]
        output = execute(negative + ['--missing-data'])
        (args.output / 'negative.log').write_text(output)
        receipt['result'] = 'passed'
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
