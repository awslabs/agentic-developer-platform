"""Guarded image-only ingestion promotion; never replace running Jobs."""
import copy
import json
import os
import re
import subprocess
from pathlib import Path

TARGETS = (
    ('cronjob', 'ingestion-refresh', '/spec/jobTemplate/spec/template/spec/containers/0/image', 'refresh'),
    ('cronjob', 'vuln-scan', '/spec/jobTemplate/spec/template/spec/containers/0/image', 'vuln-scan'),
    ('scaledjob', 'ingestion-worker', '/spec/jobTargetRef/template/spec/containers/0/image', 'worker'),
)


def run(*args):
    return subprocess.check_output(args, text=True)


def get(kind, name):
    return json.loads(run('kubectl', '-n', 'agent-context', 'get', kind, name, '-o', 'json'))


def value_at(document, path):
    value = document
    for part in path.strip('/').split('/'):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def image_patch(document, path, container, expected, candidate):
    parent = value_at(document, path.rsplit('/', 1)[0])
    if parent['name'] != container or parent['image'] != expected:
        raise ValueError('Live container or image changed; refusing stale promotion')
    patch = [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': document['metadata']['resourceVersion']},
        {'op': 'test', 'path': path, 'value': expected},
    ]
    if document['kind'] == 'ScaledJob':
        rollout = {**document['spec'].get('rollout', {}), 'strategy': 'gradual'}
        patch.append({'op': 'add', 'path': '/spec/rollout', 'value': rollout})
    return patch + [{'op': 'replace', 'path': path, 'value': candidate}]


def apply(target, document, expected, candidate, dry_run=False):
    kind, name, path, container = target
    args = ['kubectl', '-n', 'agent-context', 'patch', kind, name, '--type=json', '-p',
            json.dumps(image_patch(document, path, container, expected, candidate))]
    if dry_run:
        args += ['--dry-run=server']
    print(run(*args), end='')


def main():
    expected, candidate = os.environ['INGESTION_EXPECTED_IMAGE'], os.environ['INGESTION_IMAGE']
    prefix = os.environ['ECR_REGISTRY'] + '/adp-' + os.environ['ENVIRONMENT'] + '-agent-context-ingestion@'
    for image in (expected, candidate):
        if not image.startswith(prefix) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image[len(prefix):]):
            raise ValueError('Exact environment ingestion repository digests required')
    before = [get(*t[:2]) for t in TARGETS]
    # Validate every target before any mutation.
    for t, d in zip(TARGETS, before):
        apply(t, d, expected, candidate, dry_run=True)
    changed = []
    try:
        for t, d in zip(TARGETS, before):
            apply(t, d, expected, candidate)
            changed.append(t)
        for t, d in zip(TARGETS, before):
            after = get(*t[:2])
            desired = copy.deepcopy(d)
            value_at(desired, t[2].rsplit('/', 1)[0])['image'] = candidate
            if d['kind'] == 'ScaledJob':
                desired['spec']['rollout'] = {**d['spec'].get('rollout', {}), 'strategy': 'gradual'}
            if after['spec'] != desired['spec']:
                raise RuntimeError('Unexpected concurrent template change')
        Path('ingestion-promotion-receipt.json').write_text(json.dumps({
            'previous': expected, 'candidate': candidate, 'targets': [t[1] for t in TARGETS],
            'other_spec_preserved': True, 'scaledjob_rollout': 'gradual',
            'running_jobs_modified': False, 'end_to_end_task_canary': False,
        }, indent=2) + '\n')
    except Exception:
        errors = []
        for t in reversed(changed):
            try:
                apply(t, get(*t[:2]), candidate, expected)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            print('Guarded rollback failures: ' + repr(errors))
        raise


if __name__ == '__main__':
    main()
