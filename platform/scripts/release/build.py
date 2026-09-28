#!/usr/bin/env python3
"""Build a portable release. No Terraform apply, deployment, or credential snapshots."""
import argparse
import datetime
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
from common import *


def packages(destination):
    gateway = ROOT / 'modules/gateway'
    factory = ROOT / 'modules/agent-factory'
    run(['bash', 'platform/scripts/build-agent-factory-lambdas.sh'])
    run(['bash', gateway / 'scripts/deploy-broker.sh', '--package-only'])
    run(['bash', factory / 'webhook-ingress/scripts/package-lambdas.sh'])
    direct = {
        'api-authorizer': {'handler.py': gateway / 'lambda/api-authorizer/handler.py'},
        'pre-token-generation': {'pre_token_generation.py': gateway / 'infra/modules/cognito/lambda/pre_token_generation.py'},
        'pre-signup': {'pre_signup.py': gateway / 'lambda/pre-signup/handler.py',
                       'membership_eligibility.py': gateway / 'lambda/shared/membership_eligibility.py'},
        'session-sweeper': {'index.js': factory / 'infra/.build/session-sweeper/index.js'},
    }
    for name in ('ingest', 'response', 'pentest-actor-token'):
        direct[name] = tree(factory / 'gateway/lambdas' / name)
    spec = importlib.util.spec_from_file_location('budget_archives', gateway / 'scripts/build-budget-lambda-archives.py')
    budget = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(budget)
    for name in ('budget-usage-tracker', 'pricing-refresh'):
        direct[name] = budget.manifest(gateway, name)
    for name, entries in direct.items():
        zip_files(destination / 'lambda' / f'{name}.zip', entries)
    shutil.copyfile(gateway / 'lambda/github-auth-broker/broker.zip', destination / 'lambda/github-auth-broker.zip')
    for channel in ('github', 'gitlab'):
        shutil.copyfile(factory / 'webhook-ingress/dist' / f'{channel}.zip', destination / 'lambda' / f'webhook-{channel}.zip')
    for name, version, requirement in [('pyjwt-py313', '3.13', 'PyJWT[crypto]>=2.9.0'),
                                      ('psycopg2-py312', '3.12', 'psycopg2-binary==2.9.9')]:
        with tempfile.TemporaryDirectory() as temp:
            run(['python3', '-m', 'pip', 'install', '--target', Path(temp) / 'python', '--platform', 'manylinux2014_x86_64',
                 '--only-binary=:all:', '--implementation', 'cp', '--python-version', version, requirement, '--quiet'])
            zip_files(destination / 'layers' / f'{name}.zip', tree(temp))
    frontend = gateway / 'frontend'
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith('VITE_') and k != 'NODE_ENV'}
    run(['npm', 'ci', '--include=dev'], cwd=frontend, env=clean_env)
    run(['npm', 'run', 'build', '--', '--mode', 'release'], cwd=frontend, env=clean_env)
    entries = tree(frontend / 'dist')
    for version in ('v1', 'v2'):
        entries[f'cfn-templates/aws_role_{version}.yaml'] = gateway / 'src/auth/cfn_templates' / f'aws_role_{version}.yaml'
    entries['cfn-templates/aws_role_deploy_v1.yaml'] = (
        gateway / 'src/auth/cfn_templates/aws_role_deploy_v1.yaml'
    )
    zip_files(destination / 'frontend.zip', entries)


def images(destination, source_sha, release_id):
    registry = f'{ACCOUNTS["integration-test"]}.dkr.ecr.{REGION}.amazonaws.com'
    result = {}
    with tempfile.TemporaryDirectory() as temp:
        auth = Path(temp) / 'auth.json'
        password = run(['aws', 'ecr', 'get-login-password', '--region', REGION], capture=True)
        run(['skopeo', 'login', '--authfile', auth, '--username', 'AWS', '--password-stdin', registry], input=password, capture=True)
        for name, (repository, project, suffix) in IMAGES.items():
            # Shared publishers identify the archived source, not the release bundle.
            tag = source_sha
            env = dict(os.environ, AWS_REGION=REGION, STATE_BUCKET=f'adp-terraform-state-{ACCOUNTS["integration-test"]}', SOURCE_SHA=source_sha, ADP_RELEASE_BUILD='true')
            run(['bash', 'platform/scripts/codebuild-run.sh', f'adp-dev-{project}',
                 f'name=IMAGE_TAG,value={tag},type=PLAINTEXT', f'name=REGISTRY,value={registry}',
                 f'name=ACCOUNT_ID,value={ACCOUNTS["integration-test"]}', 'name=ENVIRONMENT,value=dev', 'name=PUBLISH_LATEST,value=false',
                 f'name=AWS_REGION,value={REGION}', f'name=STATE_BUCKET,value={env["STATE_BUCKET"]}'], env=env)
            details = aws('ecr', 'describe-images', '--repository-name', repository, '--image-ids', f'imageTag={tag}')['imageDetails']
            if len(details) != 1:
                raise ValueError('Built image is missing')
            digest = details[0]['imageDigest']
            directory = Path(temp) / name
            run(['skopeo', 'copy', '--all', '--preserve-digests', '--src-authfile', auth,
                 f'docker://{registry}/{repository}@{digest}', f'dir:{directory}'])
            if 'sha256:' + sha256(directory / 'manifest.json') != digest:
                raise ValueError('Image archive changed its digest')
            zip_files(destination / 'images' / f'{name}.zip', tree(directory))
            shutil.rmtree(directory)
            result[name] = {'repository': repository, 'digest': digest}
    return result


def build(destination, release_id, *, packages_only=False):
    valid_id(release_id)
    destination = destination.resolve()
    if destination.is_relative_to(ROOT):
        raise ValueError('Build release artifacts outside the source checkout')
    if os.environ.get('ADP_RELEASE_DIR'):
        raise ValueError('Cannot build from a prepared deployment environment')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Release build directory must be empty')
    destination.mkdir(parents=True, exist_ok=True)
    source_sha = run(['git', 'rev-parse', 'HEAD'], capture=True).strip()
    if run(['git', 'status', '--porcelain', '--untracked-files=no'], capture=True).strip():
        raise ValueError('Release builds require a clean source checkout')
    if not packages_only:
        identity(ACCOUNTS['integration-test'])
    if not packages_only:
        locks = {}
        for module in TERRAFORM_MODULES:
            run(['terraform', 'init', '-backend=false', '-input=false'], cwd=ROOT / module)
            run(['terraform', 'providers', 'lock', '-platform=linux_amd64', '-platform=darwin_arm64'], cwd=ROOT / module)
            locks[module + '/.terraform.lock.hcl'] = ROOT / module / '.terraform.lock.hcl'
        zip_files(destination / 'terraform-locks.zip', locks)
    built_images = {} if packages_only else images(destination, source_sha, release_id)
    packages(destination)
    if packages_only:
        return
    manifest = {'schema_version': 1, 'repository': 'aws-e/adp', 'release_id': release_id, 'source_sha': source_sha,
                'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'region': REGION,
                'terraform_environment': 'dev', 'images': built_images,
                'migrations': {str(p.relative_to(ROOT)): sha256(p) for p in sorted((ROOT / 'modules/gateway/alembic/versions').glob('*.py'))},
                'files': {name: {'sha256': sha256(destination / name), 'size': (destination / name).stat().st_size}
                          for name in sorted(REQUIRED_FILES)}}
    validate_manifest(manifest)
    json_write(destination / 'manifest.json', manifest)
    print(f'Release {release_id} built: {sha256(destination / "manifest.json")}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--release-id', required=True)
    parser.add_argument('--packages-only', action='store_true', help='Local package verification; does not produce a promotable release')
    args = parser.parse_args()
    build(args.directory, args.release_id, packages_only=args.packages_only)
