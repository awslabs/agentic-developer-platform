#!/usr/bin/env python3
"""Roll out a built worker digest, preserving existing jobs and image trust.

No Terraform apply: only worker images and the gateway image-trust configuration
are changed. The persisted release supplies subsequent webhook CI Terraform inputs.
"""
import argparse
import json
import re
import subprocess
import sys

KEYS = ('AGENT_WORKER_IMAGE_DIGESTS', 'ADP_TASK_WORKER_IMAGE_DIGESTS')


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

    def image(self, kind, name, container, image):
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
        self.patch('adp-agents', kind, obj, changes)

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
        trust_name = self.prefix + '/gateway/agent-authority-worker-images'
        parameter = self.aws('ssm', 'get-parameter', '--name', trust_name, '--with-decryption')['Parameter']
        trust = {digest}
        for value in [parameter['Value'], *(cm['data'].get(key, '') for key in KEYS)]:
            trust.update(x for x in value.split(',') if x and x != 'disabled')
        if not all(re.fullmatch(r'sha256:[0-9a-f]{64}', item) for item in trust):
            raise RuntimeError('Invalid existing image trust value')
        value = ','.join(sorted(trust))
        # Retain old digests so active jobs remain authorized throughout the rollout.
        metadata = self.aws('ssm', 'describe-parameters', '--parameter-filters', f'Key=Name,Option=Equals,Values={trust_name}')['Parameters']
        if len(metadata) != 1:
            raise RuntimeError('Cannot resolve existing image-trust encryption key')
        self.put(trust_name, value, parameter['Type'], metadata[0].get('KeyId'))
        self.patch('adp-gateway', 'configmap', cm, [{'op': 'add', 'path': '/data/' + key, 'value': value} for key in KEYS])
        # Explicit env entries override envFrom. Keep both sources consistent.
        # Use a strategic merge by name to retain all other env and pod settings.
        self.kube('adp-gateway', 'patch', 'deployment', 'bedrockgateway', '--type=strategic', '-p', json.dumps({
            'spec': {'template': {'metadata': {'annotations': {'adp.dev/worker-image': digest}}, 'spec': {'containers': [{
                'name': 'bedrockgateway', 'env': [{'name': key, 'value': value, 'valueFrom': None} for key in KEYS]
            }]}}}}))
        self.kube('adp-gateway', 'rollout', 'status', 'deployment/bedrockgateway', '--timeout=1200s')
        selector = ','.join(f'{k}={v}' for k, v in gateway['spec']['selector']['matchLabels'].items())
        pods = json.loads(self.kube('adp-gateway', 'get', 'pods', '-l', selector, '-o', 'json'))['items']
        live = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
        if not live:
            raise RuntimeError('No live gateway pods after rollout')
        for pod in live:
            self.kube('adp-gateway', 'exec', pod['metadata']['name'], '-c', 'bedrockgateway', '--', 'python', '-S', '-c',
                      'import os,sys; assert all(sys.argv[1] in os.environ.get(k, "").split(",") for k in sys.argv[2:])', digest, *KEYS)
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
