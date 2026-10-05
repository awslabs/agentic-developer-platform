"""Use this build's ingestion image, or preserve the unanimous live digest."""
import json
import os
import re
import subprocess
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def resolve(run=subprocess.run):
    sha = os.environ['TRIGGER_SOURCE_SHA']
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('Expected exact triggering source SHA')
    repo = 'adp-' + os.environ['ENVIRONMENT'] + '-agent-context-ingestion'
    prefix = os.environ['ECR_REGISTRY'] + '/' + repo + '@'
    r = run(['aws', 'ecr', 'describe-images', '--region', os.environ['AWS_REGION'],
             '--repository-name', repo, '--image-ids', 'imageTag=' + sha,
             '--query', 'imageDetails[0].imageDigest', '--output', 'text'], text=True, capture_output=True)
    if r.returncode == 0:
        digest = r.stdout.strip()
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('ECR did not return an exact digest')
        return prefix + digest
    if 'ImageNotFoundException' not in r.stderr:
        raise RuntimeError('Unable to resolve triggering ingestion image: ' + r.stderr)
    spec = spec_from_file_location('promotion', Path(__file__).with_name('promote-ingestion-image.py'))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    images = set()
    for kind, name, path, container in module.TARGETS:
        result = run(['kubectl', '-n', 'agent-context', 'get', kind, name, '-o', 'json'], text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError('Cannot preserve missing ingestion template: ' + name)
        parent = module.value_at(json.loads(result.stdout), path.rsplit('/', 1)[0])
        if parent['name'] != container:
            raise ValueError('Unexpected ingestion container')
        images.add(parent['image'])
    if len(images) != 1:
        raise ValueError('Live ingestion templates disagree; explicit image promotion required')
    image = images.pop()
    if not image.startswith(prefix) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image[len(prefix):]):
        raise ValueError('Live ingestion image is not an exact environment repository digest')
    return image


if __name__ == '__main__':
    print(resolve())
