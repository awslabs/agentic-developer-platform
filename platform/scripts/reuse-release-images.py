#!/usr/bin/env python3
"""Copy existing source-SHA images between operator-selected accounts unchanged."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

REPOSITORIES = ('adp-gateway', 'adp-agent-runtime', 'adp-agent-gateway', 'adp-chat-agent')


def run(command, *, env=None, input=None):
    result = subprocess.run(command, env=env, input=input, text=True, capture_output=True)
    if result.returncode:
        # Never include login input or arbitrary subprocess output in errors.
        raise RuntimeError(f'{command[0]} {command[1]} failed during image reuse')
    return result.stdout.strip()


def aws(env, region, *args):
    return json.loads(run(['aws', '--region', region, '--output', 'json', *args], env=env))


def image(env, region, repository, sha, *, optional=False):
    result = aws(env, region, 'ecr', 'batch-get-image', '--repository-name', repository,
                 '--image-ids', f'imageTag={sha}')
    if optional and not result.get('images') and len(result.get('failures', [])) == 1 \
            and result['failures'][0].get('failureCode') == 'ImageNotFound':
        return None
    if len(result.get('images', [])) != 1 or result.get('failures'):
        raise ValueError(f'Missing unambiguous source-SHA image: {repository}')
    row = result['images'][0]
    digest = row['imageId']['imageDigest']
    if row['imageId'].get('imageTag') != sha or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError(f'Invalid image identity: {repository}')
    return digest


def reuse(profile, account, region, sha, output, repositories=REPOSITORIES):
    if not re.fullmatch('[0-9a-f]{40}', sha) or not re.fullmatch('[0-9]{12}', account):
        raise ValueError('Expected full source SHA and target account')
    target = dict(os.environ)
    source = dict(target, AWS_PROFILE=profile, AWS_DEFAULT_PROFILE=profile)
    for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                'AWS_SECURITY_TOKEN', 'AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_SESSION_NAME'):
        source.pop(key, None)
    if aws(target, region, 'sts', 'get-caller-identity')['Account'] != account:
        raise ValueError('Target account changed before image reuse')
    source_account = aws(source, region, 'sts', 'get-caller-identity')['Account']
    if not re.fullmatch('[0-9]{12}', source_account) or source_account == account:
        raise ValueError('Select a different source account')
    mapping = {}
    # Validate every source and destination before any copy. A collision must
    # never overwrite an existing tag, even in a mutable ECR repository.
    for repository in repositories:
        digest = image(source, region, repository, sha)
        existing = image(target, region, repository, sha, optional=True)
        if existing is not None and existing != digest:
            raise ValueError(f'Destination tag has different bytes: {repository}')
        mapping[repository] = {'digest': digest, 'status': 'reused' if existing else 'pending'}
    receipt = dict(source_account=source_account, target_account=account,
                   region=region, source_sha=sha, images=mapping, status='running')
    def save():
        output.write_text(json.dumps(receipt, indent=2) + '\n')
        output.chmod(0o600)
    save()
    try:
        with tempfile.TemporaryDirectory(prefix='adp-image-auth-') as directory:
            registries = []
            authfiles = []
            for env, selected_account, name in ((source, source_account, 'source'), (target, account, 'target')):
                registry = f'{selected_account}.dkr.ecr.{region}.amazonaws.com'
                auth = str(Path(directory) / f'{name}.json')
                password = run(['aws', '--region', region, 'ecr', 'get-login-password'], env=env)
                run(['skopeo', 'login', '--authfile', auth, '--username', 'AWS', '--password-stdin', registry], input=password)
                registries.append(registry)
                authfiles.append(auth)
            for repository, row in mapping.items():
                # Recheck immediately before copying, in case another publisher
                # populated this tag since the initial inspection.
                existing = image(target, region, repository, sha, optional=True)
                if existing is not None and existing != row['digest']:
                    raise ValueError(f'Destination tag changed: {repository}')
                if existing is None:
                    run(['skopeo', 'copy', '--all', '--preserve-digests',
                         '--src-authfile', authfiles[0], '--dest-authfile', authfiles[1],
                         f'docker://{registries[0]}/{repository}@{row["digest"]}',
                         f'docker://{registries[1]}/{repository}:{sha}'])
                if image(target, region, repository, sha) != row['digest']:
                    raise ValueError(f'Destination digest verification failed: {repository}')
                row['status'] = 'verified'
                save()
                print(f'Verified unchanged release image: {repository}@{row["digest"]}', flush=True)
        receipt['status'] = 'complete'
    except BaseException:
        receipt['status'] = 'failed'
        raise
    finally:
        save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile', required=True)
    parser.add_argument('--target-account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gateway-only', action='store_true')
    args = parser.parse_args()
    try:
        reuse(args.source_profile, args.target_account, args.region, args.source_sha, args.output,
              ('adp-gateway',) if args.gateway_only else REPOSITORIES)
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        parser.exit(1, f'Image reuse failed: {error}\n')
