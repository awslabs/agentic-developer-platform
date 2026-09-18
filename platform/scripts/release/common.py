"""Release contracts shared by building, publication, deployment and acceptance."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[3]
REGION = 'us-east-1'
ACCOUNTS = {'integration-test': '608380991969', 'pre-production': '615296308642'}
BUCKET = 'adp-release-artifacts-608380991969'
TERRAFORM_MODULES = ('platform/infra', 'modules/gateway/infra', 'modules/agent-factory/infra', 'modules/agent-factory/webhook-ingress/infra')
IMAGES = {
    'gateway': ('adp-gateway', 'gateway-build', ''),
    'agent-runtime': ('adp-agent-runtime', 'agent-runtime', ''),
    'codex-reviewer': ('adp-codex-reviewer', 'codex-reviewer', ''),
    'agent-gateway': ('adp-agent-gateway', 'agent-gateway', ''),
    'chat-agent': ('adp-agent-gateway', 'chat-agent', '-chat'),
}
# Terraform module directory, resource name, deployed function-name template.
LAMBDAS = {
    'api-authorizer': ('modules/gateway/infra/modules/lambda-authorizer', 'authorizer', 'bedrockgw-dev-api-authorizer'),
    'pre-token-generation': ('modules/gateway/infra/modules/cognito', 'pre_token_generation', 'bedrockgw-dev-pre-token-generation'),
    'pre-signup': ('modules/gateway/infra/modules/cognito', 'pre_signup', 'bedrockgw-dev-pre-signup-trigger'),
    'budget-usage-tracker': ('modules/gateway/infra/modules/budget-lambda', 'usage_tracker', 'bedrockgw-dev-budget-usage-tracker'),
    'pricing-refresh': ('modules/gateway/infra/modules/budget-lambda', 'pricing_refresh', 'bedrockgw-dev-pricing-refresh'),
    'ingest': ('modules/agent-factory/infra/modules/lambda-gateway', 'ingest', 'adp-dev-agent-gateway-ingest'),
    'response': ('modules/agent-factory/infra/modules/lambda-gateway', 'response', 'adp-dev-agent-gateway-response'),
    'pentest-actor-token': ('modules/agent-factory/infra/modules/lambda-pentest-actor-token', 'actor_token', 'adp-dev-agent-pentest-actor-token'),
    'session-sweeper': ('modules/agent-factory/infra', 'session_sweeper', 'adp-dev-chat-session-sweeper'),
}
LAYERS = {'pyjwt-py313': ('modules/gateway/infra/modules/lambda-authorizer', 'pyjwt', 'pyjwt_layer'),
          'psycopg2-py312': ('modules/gateway/infra/modules/budget-lambda', 'psycopg2', 'psycopg2_layer')}
REQUIRED_FILES = {f'images/{name}.zip' for name in IMAGES} | {f'lambda/{name}.zip' for name in LAMBDAS} | {
    'lambda/github-auth-broker.zip', 'lambda/webhook-github.zip', 'lambda/webhook-gitlab.zip',
    'layers/pyjwt-py313.zip', 'layers/psycopg2-py312.zip', 'frontend.zip', 'terraform-locks.zip'}


def run(parts, *, capture=False, input=None, cwd=ROOT, env=None):
    result = subprocess.run([str(x) for x in parts], cwd=cwd, env=env, input=input,
                            text=True, capture_output=capture, check=False)
    if result.returncode:
        # Keep credentials, authentication payloads and returned tokens out of diagnostics.
        raise RuntimeError(f'{parts[0]} {parts[1]} failed (exit {result.returncode})')
    return result.stdout if capture else None


def aws(*parts):
    output = run(['aws', *parts, '--region', REGION, '--output', 'json'], capture=True)
    return json.loads(output) if output.strip() else None


def identity(expected):
    actual = aws('sts', 'get-caller-identity')['Account']
    if actual != expected:
        raise ValueError(f'Expected account {expected}, received {actual}')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def json_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def valid_id(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,62}', value):
        raise ValueError('Release ID must be 1-63 lowercase letters, digits, dots or hyphens')
    return value


def validate_manifest(manifest):
    if manifest.get('schema_version') != 1 or manifest.get('repository') != 'aws-e/adp':
        raise ValueError('Unsupported release manifest')
    valid_id(manifest['release_id'])
    if not re.fullmatch(r'[0-9a-f]{40}', manifest['source_sha']):
        raise ValueError('Release source must be a full commit SHA')
    if manifest.get('region') != REGION or manifest.get('terraform_environment') != 'dev':
        raise ValueError('Release does not match the supported region/environment')
    if set(manifest['files']) != REQUIRED_FILES or set(manifest['images']) != set(IMAGES):
        raise ValueError('Incomplete or unexpected release artifacts')
    for name, item in manifest['files'].items():
        if not re.fullmatch(r'[0-9a-f]{64}', item['sha256']) or not isinstance(item['size'], int) or item['size'] <= 0:
            raise ValueError(f'Invalid artifact checksum/size: {name}')
    for name, item in manifest['images'].items():
        if item['repository'] != IMAGES[name][0] or not re.fullmatch(r'sha256:[0-9a-f]{64}', item['digest']):
            raise ValueError(f'Invalid image: {name}')
    if not isinstance(manifest.get('migrations'), dict) or not manifest['migrations']:
        raise ValueError('Release has no migration inventory')
    for name, digest in manifest['migrations'].items():
        if not re.fullmatch(r'modules/gateway/alembic/versions/[a-zA-Z0-9_]+\.py', name) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid migration inventory')
    return manifest


def load(directory, *, verify=True):
    directory = Path(directory).resolve()
    manifest = validate_manifest(json.loads((directory / 'manifest.json').read_text()))
    if verify:
        for name, item in manifest['files'].items():
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size != item['size'] or sha256(path) != item['sha256']:
                raise ValueError(f'Release artifact differs from manifest: {name}')
    return manifest


def check_source(manifest):
    if run(['git', 'rev-parse', 'HEAD'], capture=True).strip() != manifest['source_sha']:
        raise ValueError('Checkout does not match release source commit')
    # Backend rewrites and the deploy journal are generated later, after this guard.
    if run(['git', 'status', '--porcelain'], capture=True).strip():
        raise ValueError('Release requires an unmodified source checkout')
    forbidden = [p for p in ROOT.rglob('*') if p.is_file() and (p.name.endswith(('.auto.tfvars', '.auto.tfvars.json', '_override.tf', '_override.tf.json')) or p.name in ('override.tf', 'override.tf.json', 'deployment.yml', '.env.local', '.env.production', '.env.release')) and not any(x in p.parts for x in ('.git', 'node_modules', '.venv', '.terraform'))]
    if forbidden:
        raise ValueError('Release checkout contains local deployment overrides')
    actual = {str(p.relative_to(ROOT)): sha256(p) for p in sorted((ROOT / 'modules/gateway/alembic/versions').glob('*.py'))}
    if actual != manifest['migrations']:
        raise ValueError('Migration source differs from release inventory')


def image_uri(manifest, name, account):
    return f"{account}.dkr.ecr.{REGION}.amazonaws.com/{manifest['images'][name]['repository']}@{manifest['images'][name]['digest']}"


def extract(archive, destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        names = source.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate archive entries')
        for entry in source.infolist():
            path = destination / entry.filename
            if not path.resolve().is_relative_to(destination) or entry.filename.startswith('/') or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Unsafe release archive entry')
        source.extractall(destination)


def zip_files(output, entries):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with archive.open(info, 'w', force_zip64=True) as target, Path(path).open('rb') as source:
                shutil.copyfileobj(source, target, 1024 * 1024)


def tree(path):
    path = Path(path)
    return {str(p.relative_to(path)): p for p in path.rglob('*') if p.is_file()
            and not any(x in p.parts for x in ('__pycache__', '.pytest_cache', '.git')) and p.suffix != '.pyc'}


def code_hash(path):
    return base64.b64encode(bytes.fromhex(sha256(path))).decode()
