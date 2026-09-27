"""Align the scheduled engine with a rolled-out gateway release; fail on drift.

Use after gateway rollout, and --verify-only before declaring a release complete.
No invocation, schedule, flow-state or configuration changes are made.
"""

import argparse
import json
import re
import subprocess


def command(args):
    return subprocess.check_output(args, text=True, timeout=360).strip()


def admission_settings(environment):
    """Match the runtime's fail-closed defaults without changing authority."""
    names = ('AGENT_AUTHORITY_ENABLED', 'ADP_WORK_CLAIMS_ENABLED')
    settings = {name: environment.get(name, 'false').lower() == 'true' for name in names}
    if len(set(settings.values())) != 1:
        raise ValueError('Protected authority and work-claim admission differ; apply matching Terraform rollout settings')
    return settings


def synchronize(*, image, account, region, environment, namespace, verify_only=False):
    def aws(*args):
        result = command(['aws', *args, '--region', region, '--output', 'json'])
        return json.loads(result) if result else None

    if aws('sts', 'get-caller-identity')['Account'] != account:
        raise ValueError('AWS account differs from requested release account')
    registry = f'{account}.dkr.ecr.{region}.amazonaws.com/adp-gateway'

    def pinned(reference):
        if reference.startswith(registry + '@sha256:'):
            digest = reference.split('@', 1)[1]
        elif reference.startswith(registry + ':'):
            tag = reference[len(registry) + 1:]
            rows = aws('ecr', 'describe-images', '--repository-name', 'adp-gateway',
                       '--image-ids', 'imageTag=' + tag)['imageDetails']
            if len(rows) != 1:
                raise ValueError('Release image is ambiguous')
            digest = rows[0]['imageDigest']
        else:
            raise ValueError('Image must belong to the target account gateway repository')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('Invalid release digest')
        return registry + '@' + digest

    expected = pinned(image)

    def gateway(*, wait_for_rollout=False):
        if wait_for_rollout:
            command(['kubectl', 'rollout', 'status', 'deployment/bedrockgateway',
                     '-n', namespace, '--timeout=300s'])
        deployment = json.loads(command(['kubectl', 'get', 'deployment/bedrockgateway',
                                         '-n', namespace, '-o', 'json']))
        containers = deployment['spec']['template']['spec']['containers']
        actual = next(c['image'] for c in containers if c['name'] == 'bedrockgateway')
        if pinned(actual) != expected:
            raise ValueError('Gateway changed or does not match the requested release')
        selector = ','.join(f'{key}={value}' for key, value in sorted(
            deployment['spec']['selector']['matchLabels'].items()))
        pods = json.loads(command(['kubectl', 'get', 'pods', '-n', namespace,
                                   '-l', selector, '-o', 'json']))['items']
        # Read admission settings from a ready pod running the requested digest.
        # A deployment-level exec can choose a terminating or previous-release
        # pod while Karpenter replaces nodes. -S avoids startup output.
        ready = []
        for pod in pods:
            if pod['status']['phase'] != 'Running' or pod['metadata'].get('deletionTimestamp'):
                continue
            statuses = pod['status'].get('containerStatuses', [])
            target = next((c for c in statuses if c['name'] == 'bedrockgateway'), None)
            image_id = target.get('imageID', '').removeprefix('docker-pullable://') if target else ''
            if target and target.get('ready') and image_id == expected:
                ready.append(pod['metadata']['name'])
        if not ready:
            raise ValueError('No ready gateway pod runs the requested release digest')
        for pod_name in ready:
            try:
                observed = json.loads(command([
                    'kubectl', 'exec', 'pod/' + pod_name, '-n', namespace,
                    '-c', 'bedrockgateway', '--', 'python', '-S', '-c',
                    'import json,os; print(json.dumps({k:os.environ.get(k,"false") for k in '
                    '["AGENT_AUTHORITY_ENABLED","ADP_WORK_CLAIMS_ENABLED"]}))',
                ]))
                break
            except subprocess.CalledProcessError:
                if pod_name == ready[-1]:
                    raise
        return admission_settings(observed)

    gateway_admission = gateway(wait_for_rollout=not verify_only)
    function = f'arn:aws:lambda:{region}:{account}:function:adp-{environment}-orchestration-tick'
    # Missing function and permission errors are failures, not evidence of parity.
    before = aws('lambda', 'get-function', '--function-name', function)
    if admission_settings(before['Configuration'].get('Environment', {}).get('Variables', {})) != gateway_admission:
        raise ValueError('Gateway and engine admission settings differ; apply matching Terraform rollout settings')
    if not verify_only and before['Code']['ResolvedImageUri'] != expected:
        aws('lambda', 'update-function-code', '--function-name', function,
            '--image-uri', expected, '--revision-id', before['Configuration']['RevisionId'])
        aws('lambda', 'wait', 'function-updated-v2', '--function-name', function)
    after = aws('lambda', 'get-function', '--function-name', function)
    config = after['Configuration']
    if (after['Code']['ResolvedImageUri'] != expected or config['State'] != 'Active'
            or config['LastUpdateStatus'] != 'Successful'):
        raise ValueError('Engine release incomplete: Lambda digest/state does not match')
    # Detect a concurrent gateway deployment during the Lambda update.
    if (gateway() != gateway_admission
            or admission_settings(config.get('Environment', {}).get('Variables', {})) != gateway_admission):
        raise ValueError('Admission settings changed during release verification')
    return {'image': expected, 'function': function, 'status': 'verified'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('image', 'account', 'region', 'environment'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--namespace', default='adp-gateway')
    parser.add_argument('--verify-only', action='store_true')
    print(json.dumps(synchronize(**vars(parser.parse_args())), sort_keys=True))


if __name__ == '__main__':
    main()
