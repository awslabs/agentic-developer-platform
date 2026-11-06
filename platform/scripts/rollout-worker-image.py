#!/usr/bin/env python3
"""Roll out a built worker digest, preserving existing jobs and image trust.

No Terraform apply: only worker images and the gateway image-trust configuration
are changed. The persisted release supplies subsequent webhook CI Terraform inputs.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEYS = ('AGENT_WORKER_IMAGE_DIGESTS', 'ADP_TASK_WORKER_IMAGE_DIGESTS')


def writer_module():
    path = Path(__file__).resolve().parents[2] / 'modules/gateway/scripts/sync-gateway-writers.py'
    spec = importlib.util.spec_from_file_location('gateway_writer_sync', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command(*args):
    return subprocess.check_output(args, text=True).strip()


class Rollout:
    def __init__(self, account, region, environment):
        self.account, self.region, self.environment = account, region, environment
        self.prefix = f'/adp/{environment}'
        self.release_name = self.prefix + '/webhook-ingress/deployed-worker-image'

    def aws(self, *args):
        return json.loads(command('aws', *args, '--region', self.region, '--output', 'json'))

    def kube(self, namespace, *args):
        return command('kubectl', '--request-timeout=30s', '-n', namespace, *args)

    def get(self, namespace, kind, name):
        value = self.kube(namespace, 'get', kind, name, '--ignore-not-found', '-o', 'json')
        return json.loads(value) if value else None

    def release(self):
        # GetParameters distinguishes an absent pin from permission/network errors.
        values = self.aws('ssm', 'get-parameters', '--names', self.release_name)['Parameters']
        return json.loads(values[0]['Value']) if values else None

    def put(self, name, value, kind='String', key_id=None):
        return self.aws('ssm', 'put-parameter', '--name', name, '--value', value,
                        '--type', kind, '--tier', 'Advanced', '--overwrite',
                        *(['--key-id', key_id] if key_id else []))

    def patch(self, namespace, kind, obj, changes):
        patch = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': obj['metadata']['resourceVersion']}, *changes]
        self.kube(namespace, 'patch', kind, obj['metadata']['name'], '--type=json', '-p', json.dumps(patch))

    # The optimistic lock (`test` on metadata.resourceVersion) protects against
    # overwriting a concurrent change, but a DaemonSet or Deployment bumps its
    # resourceVersion on every status update (a node joining is enough), so the
    # first attempt can lose the race through no fault of ours. Re-read and retry
    # the same intent a bounded number of times before giving up.
    PATCH_ATTEMPTS = 5

    TRUST_RETENTION_DAYS = 30
    TRUST_MAX_ENTRIES = 60

    def live_worker_digests(self):
        digests = set()
        for kind, name in (('scaledjob', 'agent-scaledjob'), ('daemonset', 'agent-image-prepull'), ('deployment', 'agent-warm-pool')):
            obj = self.get('adp-agents', kind, name)
            if obj is None:
                continue
            spec = obj['spec']['jobTargetRef']['template']['spec'] if kind == 'scaledjob' else obj['spec']['template']['spec']
            for container in spec.get('containers', []):
                if '@' in container.get('image', ''):
                    digests.add(container['image'].split('@', 1)[1])
        return digests

    def recent_image_digests(self):
        """Digests pushed to the worker repository within the retention window, newest first."""
        pushed = {}
        token = None
        while True:
            args = ['ecr', 'describe-images', '--repository-name', 'adp-agent-runtime', '--max-items', '500']
            if token:
                args += ['--starting-token', token]
            page = self.aws(*args)
            for item in page.get('imageDetails', []):
                when = item.get('imagePushedAt')
                if isinstance(when, (int, float)):
                    when = datetime.fromtimestamp(when, tz=timezone.utc)
                elif isinstance(when, str):
                    when = datetime.fromisoformat(when.replace('Z', '+00:00'))
                else:
                    when = datetime.now(timezone.utc)
                pushed[item['imageDigest']] = when
            token = page.get('NextToken')
            if not token:
                break
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.TRUST_RETENTION_DAYS)
        recent = sorted((d for d, when in pushed.items() if when >= cutoff), key=lambda d: pushed[d], reverse=True)
        return recent

    def bounded_trust(self, trust, digest, current_image):
        keep = {digest} | self.live_worker_digests()
        if '@' in current_image:
            keep.add(current_image.split('@', 1)[1])
        recent = [d for d in self.recent_image_digests() if d in trust and d not in keep]
        room = max(0, self.TRUST_MAX_ENTRIES - len(keep))
        return keep | set(recent[:room])

    def image(self, kind, name, container, image):
        for attempt in range(1, self.PATCH_ATTEMPTS + 1):
            obj = self.get('adp-agents', kind, name)
            if obj is None:
                if kind == 'scaledjob':
                    raise RuntimeError('Worker ScaledJob is missing; install webhook ingress first')
                return
            base = '/spec/jobTargetRef/template/spec' if kind == 'scaledjob' else '/spec/template/spec'
            spec = obj['spec']['jobTargetRef']['template']['spec'] if kind == 'scaledjob' else obj['spec']['template']['spec']
            index = next(i for i, value in enumerate(spec['containers']) if value['name'] == container)
            changes = [{'op': 'replace', 'path': f'{base}/containers/{index}/image', 'value': image}]
            if kind == 'scaledjob':
                # KEDA's default rollout deletes existing jobs. Preserve those jobs.
                changes.append({'op': 'add', 'path': '/spec/rollout', 'value': {**obj['spec'].get('rollout', {}), 'strategy': 'gradual'}})
            try:
                self.patch('adp-agents', kind, obj, changes)
                return
            except subprocess.CalledProcessError:
                if attempt == self.PATCH_ATTEMPTS:
                    raise
                print(f'{kind}/{name} changed while patching (attempt {attempt}); re-reading and retrying', file=sys.stderr)
                time.sleep(attempt)

    def terraform_vars(self):
        release = self.release()
        if not release:
            return {}
        image = release['image']
        expected = f'{self.account}.dkr.ecr.{self.region}.amazonaws.com/adp-agent-runtime@'
        if not image.startswith(expected) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image[len(expected):]):
            raise RuntimeError('Persisted worker pin does not belong to target repository')
        trust = self.aws('ssm', 'get-parameter', '--name', self.prefix + '/gateway/agent-authority-worker-images', '--with-decryption')['Parameter']['Value'].split(',')
        if image.split('@')[1] not in trust:
            raise RuntimeError('Persisted worker image is missing from gateway trust')
        return {'agent_image': image, 'agent_authority_worker_image_digests': trust}

    def gateway_writers(self, gateway):
        module = writer_module()
        primary = next(c for c in gateway['spec']['template']['spec']['containers'] if c['name'] == 'bedrockgateway')
        writers = module.WorkerTrust(namespace='adp-gateway', image=primary['image'],
            inventory=module.bindings(os.environ.get('ADP_GATEWAY_WRITER_INVENTORY', '[]')))
        # Refuse missing enrollment, replaced UIDs, ambiguous processes, duplicate
        # trust keys or insufficient privileges before changing shared trust.
        writers.permissions()
        managed, _ = writers.inspect(writers.snapshot())
        writers.patch_permissions(managed)
        return writers

    def deploy(self, revision):
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ValueError('Full source commit SHA required')
        previous = self.release()
        if previous and previous['revision'] != revision:
            # A late older build must not undo a newer deployment. Checkout has full history.
            ancestor = subprocess.run(['git', 'merge-base', '--is-ancestor', revision, previous['revision']], check=False)
            if ancestor.returncode == 0:
                print('A newer worker revision is already deployed; skipping older build.')
                return
            if ancestor.returncode != 1:
                raise RuntimeError('Cannot establish worker revision ordering')
        details = self.aws('ecr', 'describe-images', '--repository-name', 'adp-agent-runtime', '--image-ids', f'imageTag={revision}')['imageDetails']
        if len(details) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}', details[0]['imageDigest']):
            raise RuntimeError('Missing immutable worker image for source revision')
        digest = details[0]['imageDigest']
        image = f'{self.account}.dkr.ecr.{self.region}.amazonaws.com/adp-agent-runtime@{digest}'
        worker = self.get('adp-agents', 'scaledjob', 'agent-scaledjob')
        if worker is None:
            raise RuntimeError('Install webhook ingress before rolling out a worker image')
        current = next(c['image'] for c in worker['spec']['jobTargetRef']['template']['spec']['containers'] if c['name'] == 'agent-worker')
        if current.split('@')[0].split(':')[0] != image.split('@')[0]:
            raise RuntimeError('This environment uses a domain worker image; deploy it through its domain workflow')
        cm = self.get('adp-gateway', 'configmap', 'adp-worker-authority-config')
        gateway = self.get('adp-gateway', 'deployment', 'bedrockgateway')
        container = next(c for c in gateway['spec']['template']['spec']['containers'] if c['name'] == 'bedrockgateway')
        if not any(e.get('configMapRef', {}).get('name') == 'adp-worker-authority-config' for e in container.get('envFrom', [])):
            raise RuntimeError('Gateway must consume the worker authority ConfigMap before worker rollout')
        writers = self.gateway_writers(gateway)
        trust_name = self.prefix + '/gateway/agent-authority-worker-images'
        parameter = self.aws('ssm', 'get-parameter', '--name', trust_name, '--with-decryption')['Parameter']
        trust = {digest}
        for value in [parameter['Value'], *(cm['data'].get(key, '') for key in KEYS)]:
            trust.update(x for x in value.split(',') if x and x != 'disabled')
        if not all(re.fullmatch(r'sha256:[0-9a-f]{64}', item) for item in trust):
            raise RuntimeError('Invalid existing image trust value')
        # Retain old digests so active jobs remain authorized throughout the rollout,
        # but the list cannot grow forever: SSM refuses values over 8192 characters
        # (113 digests broke the dev rollout). Keep the new digest, every digest a
        # worker workload still runs, and recently pushed images; drop the rest.
        trust = self.bounded_trust(trust, digest, current)
        value = ','.join(sorted(trust))
        metadata = self.aws('ssm', 'describe-parameters', '--parameter-filters', f'Key=Name,Option=Equals,Values={trust_name}')['Parameters']
        if len(metadata) != 1:
            raise RuntimeError('Cannot resolve existing image-trust encryption key')
        self.put(trust_name, value, parameter['Type'], metadata[0].get('KeyId'))
        self.patch('adp-gateway', 'configmap', cm, [{'op': 'add', 'path': '/data/' + key, 'value': value} for key in KEYS])
        # Explicit env overrides envFrom in every application writer, including
        # restricted chat and operator-enrolled gateways. The shared reconciler
        # preserves their full specs and waits for old processes to leave.
        writers.trust = {key: value for key in KEYS}
        writers.run()
        self.image('daemonset', 'agent-image-prepull', 'prepull', image)
        self.image('deployment', 'agent-warm-pool', 'balloon', image)
        self.image('scaledjob', 'agent-scaledjob', 'agent-worker', image)
        self.put(self.release_name, json.dumps({'revision': revision, 'image': image}))
        print(f'Worker rollout complete: {image}. Existing jobs retain their images.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--environment', required=True)
    parser.add_argument('--revision')
    parser.add_argument('--terraform-vars', help='Write the deployed pin as Terraform JSON variables; read-only')
    args = parser.parse_args()
    rollout = Rollout(args.account, args.region, args.environment)
    if rollout.aws('sts', 'get-caller-identity')['Account'] != args.account:
        raise RuntimeError('AWS identity does not match deployment target')
    if args.terraform_vars:
        with open(args.terraform_vars, 'w') as stream:
            json.dump(rollout.terraform_vars(), stream)
    else:
        rollout.deploy(args.revision or '')


if __name__ == '__main__':
    main()
