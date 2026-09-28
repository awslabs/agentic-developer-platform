"""Promote the reviewed runner digest without changing scale-set configuration."""
import argparse
import copy
import json
from pathlib import Path
import re
import subprocess

RELEASES = ('arc-runner-org', 'arc-runner-agent')
RESOURCE = 'autoscalingrunnersets.actions.github.com'

def image_patch(current, image):
    containers = current['spec']['template']['spec']['containers']
    indices = [i for i, c in enumerate(containers) if c['name'] == 'runner']
    if len(indices) != 1:
        raise ValueError('Expected exactly one runner container')
    index = indices[0]
    path = f'/spec/template/spec/containers/{index}/image'
    return [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
        {'op': 'test', 'path': path, 'value': containers[index]['image']},
        {'op': 'replace', 'path': path, 'value': image},
    ]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'\d{12}', args.account):
        raise ValueError('Invalid account')
    source = Path('modules/agent-factory/infra/variables.tf').read_text()
    match = re.search(r'variable "runner_image_tag"\s*\{.*?default\s*=\s*"([^"]+)"', source, re.S)
    if not match or not re.fullmatch(r'[A-Za-z0-9_.-]+@sha256:[a-f0-9]{64}', match[1]):
        raise ValueError('Runner source must pin an immutable reviewed digest')
    image = f'{args.account}.dkr.ecr.us-east-1.amazonaws.com/adp-arc-runner:{match[1]}'
    namespace = 'arc-runners'
    rows = []
    # Validate both scale sets before making any change.
    for name in RELEASES:
        cmd = ['kubectl', '-n', namespace, 'get', RESOURCE, name, '-o', 'json']
        current = json.loads(subprocess.check_output(cmd, text=True))
        patch = image_patch(current, image)
        rows.append((name, current, patch))
    for name, current, patch in rows:
        cmd = ['kubectl', '-n', namespace, 'patch', RESOURCE, name, '--type=json', '-p', json.dumps(patch), '-o', 'json']
        preview = json.loads(subprocess.check_output(cmd + ['--dry-run=server'], text=True))
        expected = copy.deepcopy(current['spec'])
        for container in expected['template']['spec']['containers']:
            if container['name'] == 'runner':
                container['image'] = image
        if preview['spec'] != expected:
            raise ValueError('Admission changed unrelated scale-set configuration')
        if args.apply:
            applied = json.loads(subprocess.check_output(cmd, text=True))
            if applied['spec'] != expected:
                raise RuntimeError('Applied scale-set spec differs from reviewed preview')
        print(json.dumps({'scale_set': name, 'namespace': namespace, 'previous_image': patch[1]['value'], 'image': image, 'applied': args.apply, 'running_jobs': 'preserved; existing ephemeral runners drain normally'}))

if __name__ == '__main__':
    main()
