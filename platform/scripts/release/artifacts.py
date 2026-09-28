#!/usr/bin/env python3
"""Install verified release artifacts without running a compiler or image build."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from common import *
from storage import client, put_once

OVERRIDE = 'adp_release_override.tf.json'


def publish_image(ecr, repository, tag, digest, path, registry, auth):
    """Reuse a matching immutable transport tag; never overwrite another release."""
    from botocore.exceptions import ClientError

    try:
        found = ecr.describe_images(repositoryName=repository, imageIds=[{'imageTag': tag}])['imageDetails']
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ImageNotFoundException':
            raise
    else:
        if len(found) != 1 or found[0]['imageDigest'] != digest:
            raise ValueError(f'Existing release tag selects a different image: {repository}:{tag}')
        return
    run(['skopeo', 'copy', '--all', '--preserve-digests', '--dest-authfile', auth,
         f'dir:{path}', f'docker://{registry}/{repository}:{tag}'])
    found = ecr.describe_images(repositoryName=repository, imageIds=[{'imageTag': tag}])['imageDetails']
    if len(found) != 1 or found[0]['imageDigest'] != digest:
        raise ValueError(f'Destination image digest changed: {repository}:{tag}')


def key(manifest, name):
    return f"adp-releases/sha256/{manifest['files'][name]['sha256']}/{Path(name).name}"


def overrides(manifest, directory, account):
    """Terraform's native override files change code inputs only, not configuration.

    Keep these in the disposable checkout through all reconciliation passes.
    Neither these files nor the private upgrade journal belongs in git.
    """
    files = {}

    def block(module, kind, resource_type, name, attributes):
        files.setdefault(module, {}).setdefault(kind, {}).setdefault(resource_type, {})[name] = attributes

    for name, (module, resource, _) in LAMBDAS.items():
        package = directory / 'lambda' / f'{name}.zip'
        block(module, 'resource', 'aws_lambda_function', resource,
              {'filename': str(package), 'source_code_hash': code_hash(package)})
    for name, (module, resource, data) in LAYERS.items():
        artifact = f'layers/{name}.zip'
        block(module, 'data', 'aws_s3_object', data, {'key': key(manifest, artifact)})
        block(module, 'resource', 'aws_lambda_layer_version', resource,
              {'s3_key': key(manifest, artifact), 'source_code_hash': code_hash(directory / artifact),
               'skip_destroy': True, 'lifecycle': {'create_before_destroy': True}})
    module = 'modules/agent-factory/webhook-ingress/infra'
    for channel in ('github', 'gitlab'):
        artifact = f'lambda/webhook-{channel}.zip'
        block(module, 'data', 'aws_s3_object', f'{channel}_lambda_zip', {'key': key(manifest, artifact)})
        block(module, 'resource', 'aws_lambda_function', f'{channel}_webhook',
              {'s3_key': key(manifest, artifact), 'source_code_hash': code_hash(directory / artifact)})
    # Resolve the orchestration Lambda by digest too; never read a mutable tag.
    block('modules/gateway/infra', 'data', 'aws_ecr_image', 'orchestration_tick',
          {'image_tag': None, 'image_digest': manifest['images']['gateway']['digest']})
    return files


def prepare(directory, environment, output, *, source_checked=False):
    account = ACCOUNTS[environment]
    manifest = load(directory)
    if not source_checked:
        check_source(manifest)
    identity(account)
    # Refuse environments requiring artifacts this release contract does not cover.
    bucket = f'adp-terraform-state-{account}'
    s3 = client()
    for name, item in manifest['files'].items():
        if name.startswith(('lambda/', 'layers/')):
            put_once(s3, bucket, key(manifest, name), directory / name)
    with tempfile.TemporaryDirectory() as temp:
        registry = f'{account}.dkr.ecr.{REGION}.amazonaws.com'
        auth = Path(temp) / 'auth.json'
        password = run(['aws', 'ecr', 'get-login-password', '--region', REGION], capture=True)
        run(['skopeo', 'login', '--authfile', auth, '--username', 'AWS', '--password-stdin', registry], input=password, capture=True)
        import boto3
        ecr = boto3.client('ecr', region_name=REGION)
        for name in IMAGES:
            path = Path(temp) / name
            extract(directory / 'images' / f'{name}.zip', path)
            digest = manifest['images'][name]['digest']
            if 'sha256:' + sha256(path / 'manifest.json') != digest:
                raise ValueError(f'Image archive does not contain the selected manifest: {name}')
            repository = manifest['images'][name]['repository']
            # A unique transport tag is only for ECR publication. Workloads use @digest.
            tag = f"release-{manifest['release_id']}-{name}"
            publish_image(ecr, repository, tag, digest, path, registry, auth)
            shutil.rmtree(path)
    with zipfile.ZipFile(directory / 'terraform-locks.zip') as locks:
        expected = {module + '/.terraform.lock.hcl' for module in TERRAFORM_MODULES}
        if set(locks.namelist()) != expected:
            raise ValueError('Incomplete Terraform provider locks')
    extract(directory / 'terraform-locks.zip', ROOT)
    # Archive data sources still evaluate the sweeper's bundled source. Restore
    # it from the release instead of invoking npm/esbuild during promotion.
    extract(directory / 'lambda/session-sweeper.zip', ROOT / 'modules/agent-factory/infra/.build/session-sweeper')
    for module, value in overrides(manifest, directory, account).items():
        json_write(ROOT / module / OVERRIDE, value)
    json_write(output, {'account': account, 'release_id': manifest['release_id'],
                       'manifest_sha256': sha256(directory / 'manifest.json')})


def environment_values(directory, account):
    manifest = load(directory, verify=False)
    return {f'ADP_RELEASE_{name.upper().replace("-", "_")}_IMAGE': image_uri(manifest, name, account) for name in IMAGES}


def verify_prepared(directory):
    """Cheap subprocess guard, bound to the currently authenticated target."""
    prepared = Path(os.environ['ADP_RELEASE_PREPARED'])
    data = json.loads(prepared.read_text())
    identity(data['account'])
    if sha256(directory / 'manifest.json') != data['manifest_sha256']:
        raise ValueError('Release changed after preparation')
    return data


def broker(directory):
    data = verify_prepared(directory)
    manifest = load(directory, verify=False)
    name = 'bedrockgw-dev-github-auth-broker'
    aws('lambda', 'update-function-code', '--function-name', name,
        '--s3-bucket', f"adp-terraform-state-{data['account']}",
        '--s3-key', key(manifest, 'lambda/github-auth-broker.zip'))
    aws('lambda', 'wait', 'function-updated', '--function-name', name)
    config = aws('lambda', 'get-function-configuration', '--function-name', name)
    if config['CodeSha256'] != code_hash(directory / 'lambda/github-auth-broker.zip'):
        raise ValueError('Broker code does not match release')


def frontend(directory):
    verify_prepared(directory)

    def parameter(name):
        return aws('ssm', 'get-parameter', '--name', f'/adp/dev/gateway/{name}')['Parameter']['Value']

    settings = {'VITE_API_URL': '/api', 'VITE_COGNITO_REGION': REGION}
    for name, parameter_name in {
        'VITE_COGNITO_USER_POOL_ID': 'cognito-user-pool-id', 'VITE_COGNITO_CLIENT_ID': 'cognito-client-id',
        'VITE_COGNITO_DOMAIN': 'cognito-domain', 'VITE_GITHUB_AUTH_BROKER_URL': 'github-auth-broker-url',
        'VITE_AGENT_WS_URL': 'agent-ws-url',
    }.items():
        settings[name] = parameter(parameter_name)
        if not settings[name] or settings[name] == 'None':
            raise ValueError(f'Missing runtime setting {name}')
    bucket, distribution = parameter('frontend-bucket'), parameter('cloudfront-id')
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp)
        extract(directory / 'frontend.zip', path)
        (path / 'runtime-config.js').write_text(runtime_config(settings))
        # Hashed assets are uploaded first. Retain previous assets for clients
        # still using the previous index during an upgrade or recovery.
        run(['aws', 's3', 'sync', str(path), f's3://{bucket}/', '--exclude', 'index.html',
             '--exclude', 'runtime-config.js', '--cache-control', 'public,max-age=31536000,immutable', '--region', REGION])
        for name, content_type in [('runtime-config.js', 'application/javascript'), ('index.html', 'text/html')]:
            run(['aws', 's3', 'cp', path / name, f's3://{bucket}/{name}', '--content-type', content_type,
                 '--cache-control', 'no-store,max-age=0', '--region', REGION])
        invalidation = aws('cloudfront', 'create-invalidation', '--distribution-id', distribution, '--paths', '/*')
        aws('cloudfront', 'wait', 'invalidation-completed', '--distribution-id', distribution,
            '--id', invalidation['Invalidation']['Id'])


def runtime_config(settings):
    # JS serialization, not shell interpolation. These are public browser settings.
    return 'window.__ADP_CONFIG__ = ' + json.dumps(settings, sort_keys=True, ensure_ascii=True).replace('<', '\\u003c') + ';\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'verify-prepared', 'broker', 'frontend', 'env'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--environment', choices=ACCOUNTS)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.operation == 'prepare':
        prepare(directory, args.environment, args.output)
    elif args.operation == 'env':
        import shlex
        data = verify_prepared(directory)
        for name, value in environment_values(directory, data['account']).items():
            print(f'export {name}={shlex.quote(value)}')
    else:
        {'verify-prepared': verify_prepared, 'broker': broker, 'frontend': frontend}[args.operation](directory)
