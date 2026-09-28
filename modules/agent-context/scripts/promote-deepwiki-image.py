"""Promote only the DeepWiki image; preserve config and guard concurrent changes."""
import json
import os
import re
import subprocess
from pathlib import Path


def run(*args):
    return subprocess.check_output(args, text=True)


def deployment():
    return json.loads(run('kubectl', '-n', 'agent-context', 'get', 'deploy', 'deepwiki', '-o', 'json'))


def image_patch(document, expected, candidate):
    containers = document['spec']['template']['spec']['containers']
    indices = [i for i, c in enumerate(containers) if c['name'] == 'deepwiki']
    if len(indices) != 1:
        raise ValueError('Expected exactly one DeepWiki container')
    index = indices[0]
    if containers[index]['image'] != expected:
        raise ValueError('Live image changed; refusing stale promotion or rollback')
    path = f'/spec/template/spec/containers/{index}/image'
    return [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': document['metadata']['resourceVersion']},
        {'op': 'test', 'path': path, 'value': expected},
        {'op': 'replace', 'path': path, 'value': candidate},
    ]


def patch(document, expected, candidate, dry_run=False):
    args = ['kubectl', '-n', 'agent-context', 'patch', 'deploy', 'deepwiki', '--type=json',
            '-p', json.dumps(image_patch(document, expected, candidate))]
    if dry_run:
        args += ['--dry-run=server']
    print(run(*args), end='')


def main():
    candidate = os.environ['DEEPWIKI_IMAGE']
    expected = os.environ['DEEPWIKI_EXPECTED_IMAGE']
    prefix = os.environ['ECR_REGISTRY'] + '/adp-' + os.environ['ENVIRONMENT'] + '-agent-context-deepwiki@'
    if any(not value.startswith(prefix) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value[len(prefix):])
           for value in (candidate, expected)):
        raise ValueError('Promotion requires exact digests in this environment DeepWiki repository')
    before = deployment()
    patch(before, expected, candidate, dry_run=True)
    patch(before, expected, candidate)
    try:
        print(run('kubectl', '-n', 'agent-context', 'rollout', 'status', 'deploy/deepwiki', '--timeout=300s'))
        health = "import urllib.request; assert all(urllib.request.urlopen(u,timeout=10).status==200 for u in ['http://127.0.0.1:8001/health','http://127.0.0.1:3000/']); print('DeepWiki API and UI passed')"
        print(run('kubectl', '-n', 'agent-context', 'exec', 'deploy/deepwiki', '-c', 'deepwiki', '--', 'python', '-c', health))
        after = deployment()
        desired = json.loads(json.dumps(before['spec']))
        next(c for c in desired['template']['spec']['containers'] if c['name'] == 'deepwiki')['image'] = candidate
        if after['spec'] != desired:
            raise RuntimeError('Deployment spec changed beyond the requested image')
        Path('deepwiki-promotion-receipt.json').write_text(json.dumps({
            'previous': expected, 'candidate': candidate, 'generation': after['metadata']['generation'],
            'ready_replicas': after['status'].get('readyReplicas', 0),
            'api_health': 200, 'ui_health': 200, 'other_spec_preserved': True,
        }, indent=2) + '\n')
    except Exception:
        # Do not overwrite a concurrent operator's image change.
        patch(deployment(), candidate, expected)
        print(run('kubectl', '-n', 'agent-context', 'rollout', 'status', 'deploy/deepwiki', '--timeout=300s'))
        raise


if __name__ == '__main__':
    main()
