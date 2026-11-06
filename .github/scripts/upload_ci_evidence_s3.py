#!/usr/bin/env python3
"""Retain CI evidence in private, write-once S3 objects using runner IRSA."""
import base64
import gzip
import glob
import hashlib
import json
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile

MAX_BYTES = 512 * 1024 * 1024


def collect_paths(value, workspace, temporary, missing='error'):
    """Collect explicit roots/globs, with the same confinement as a single path.

    File roots retain their basename; directory roots retain relative paths.
    Globs retain paths relative to their fixed directory prefix, so receipts
    from multiple service directories cannot overwrite one another.
    Conflicting names fail instead of silently replacing one source's evidence.
    """
    if missing not in {'error', 'warn', 'ignore'}:
        raise ValueError('Unknown missing-file policy')
    roots = [workspace.resolve(), temporary.resolve()]
    files = {}
    absent = []
    total = 0
    patterns = [line.strip() for line in value.splitlines() if line.strip()]
    if not patterns:
        raise ValueError('At least one evidence path is required')
    for pattern in patterns:
        if pattern.startswith('!'):
            raise ValueError('Evidence exclusions are not supported; use explicit allowlists')
        path = Path(pattern)
        if not path.is_absolute():
            path = workspace / path
        if not any(path.resolve().is_relative_to(root) for root in roots):
            raise ValueError('Evidence must be inside the checkout or runner temporary directory')
        glob_root = None
        if glob.has_magic(str(path)):
            glob_root = path.parent
            while glob.has_magic(str(glob_root)):
                glob_root = glob_root.parent
        matches = sorted(glob.glob(str(path), recursive=True)) if glob_root is not None else [str(path)]
        found = False
        for match in matches:
            candidate = Path(match)
            if not any(candidate.resolve().is_relative_to(root) for root in roots):
                raise ValueError('Evidence must be inside the checkout or runner temporary directory')
            if not candidate.exists() and not candidate.is_symlink():
                continue
            for source, name in collect(candidate, allow_empty=True):
                if glob_root is not None:
                    name = source.relative_to(glob_root).as_posix()
                previous = files.get(name)
                if previous is not None and previous != source:
                    raise ValueError('Evidence roots contain conflicting archive paths')
                if previous is None:
                    total += source.stat().st_size
                    if total > MAX_BYTES:
                        raise ValueError('Evidence exceeds the 512 MiB single-upload bound')
                    files[name] = source
                found = True
        if not found:
            absent.append(pattern)
    if absent and missing == 'error':
        raise ValueError('Required evidence path has no files')
    if absent and missing == 'warn':
        print('::warning::Some requested evidence paths contain no files; publication is partial or missing')
    return [(source, name) for name, source in sorted(files.items())], len(absent)


def runner_credentials():
    """Use only scoped runner IRSA in this process, never a deployment session.

    Changing this child process cannot alter credentials of later workflow steps.
    Offline jobs may intentionally disable metadata and ambient credentials for
    their test commands; only the uploader restores the explicit IRSA binding.
    """
    role = os.environ.get('CI_EVIDENCE_RUNNER_ROLE_ARN') or os.environ.get('AWS_ROLE_ARN')
    web_token = os.environ.get('CI_EVIDENCE_RUNNER_TOKEN_FILE') or os.environ.get('AWS_WEB_IDENTITY_TOKEN_FILE')
    if not role or not web_token:
        raise ValueError('CI evidence requires the existing runner IRSA binding')
    for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                'AWS_SECURITY_TOKEN', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE',
                'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
                'AWS_CONTAINER_AUTHORIZATION_TOKEN', 'AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE'):
        os.environ.pop(key, None)
    os.environ.update(AWS_ROLE_ARN=role, AWS_WEB_IDENTITY_TOKEN_FILE=web_token,
                      AWS_EC2_METADATA_DISABLED='true', AWS_CONFIG_FILE='/dev/null',
                      AWS_SHARED_CREDENTIALS_FILE='/dev/null', BOTO_CONFIG='/dev/null')


def aws(*args):
    result = subprocess.run(['aws', *args, '--output', 'json'], check=True, capture_output=True, text=True)
    return json.loads(result.stdout or '{}')


def token(value, pattern, label):
    if not re.fullmatch(pattern, value):
        raise ValueError(f'Invalid {label}')
    return value


def collect(path, *, allow_empty=False):
    """Reject links and special files rather than following them into credentials."""
    if not path.exists() or path.is_symlink():
        raise ValueError('Evidence path is absent or is a symbolic link')
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('Evidence path traverses a symbolic link')
    candidates = sorted(path.rglob('*')) if path.is_dir() else [path]
    files, total = [], 0
    for entry in candidates:
        mode = entry.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError('Evidence contains a symbolic link or special file')
        size = entry.stat().st_size
        total += size
        if total > MAX_BYTES:
            raise ValueError('Evidence exceeds the 512 MiB single-upload bound')
        name = entry.relative_to(path).as_posix() if path.is_dir() else entry.name
        files.append((entry, name))
    if not files and not allow_empty:
        raise ValueError('Evidence contains no files')
    return files


def archive(files, destination):
    records = []
    with destination.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0, filename='') as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as tar:
            for source, name in files:
                # Hash the exact bytes archived, not a separate read of a changing file.
                data = source.read_bytes()
                if len(data) > MAX_BYTES or sum(row['size'] for row in records) + len(data) > MAX_BYTES:
                    raise ValueError('Evidence grew beyond the upload bound')
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), 0o600
                tar.addfile(info, io.BytesIO(data))
                records.append({'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    return records


def upload(bucket, key, path):
    digest = hashlib.sha256(path.read_bytes()).digest()
    checksum = base64.b64encode(digest).decode()
    try:
        result = aws('s3api', 'put-object', '--bucket', bucket, '--key', key,
                     '--body', str(path), '--server-side-encryption', 'AES256',
                     '--checksum-algorithm', 'SHA256', '--checksum-sha256', checksum,
                     '--if-none-match', '*')
    except subprocess.CalledProcessError as error:
        if 'PreconditionFailed' not in (error.stderr or ''):
            raise
        # Reusing the exact same bytes is safe; never overwrite a conflicting key.
        result = {}
    head = aws('s3api', 'head-object', '--bucket', bucket, '--key', key, '--checksum-mode', 'ENABLED')
    version = result.get('VersionId', head.get('VersionId'))
    if not version or version == 'null':
        raise ValueError('Evidence bucket must have versioning enabled')
    if (head.get('ChecksumSHA256') != checksum or head.get('ContentLength') != path.stat().st_size
            or head.get('VersionId') != version or head.get('ServerSideEncryption') != 'AES256'):
        raise ValueError('Uploaded evidence read-back mismatch')
    return {'bucket': bucket, 'key': key, 'version_id': version, 'sha256': digest.hex(), 'size': path.stat().st_size}


def main():
    env = os.environ
    name = token(env['CI_EVIDENCE_NAME'], r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}', 'artifact name')
    repository = token(env['GITHUB_REPOSITORY'], r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', 'repository')
    run_id = token(env['GITHUB_RUN_ID'], r'[1-9][0-9]*', 'run ID')
    attempt = token(env['GITHUB_RUN_ATTEMPT'], r'[1-9][0-9]*', 'run attempt')
    job = token(env['GITHUB_JOB'], r'[A-Za-z0-9_-]+', 'job')
    retention = token(env.get('CI_EVIDENCE_RETENTION_DAYS', '90'), r'7|14|90', 'retention')
    environment = token(env.get('CI_EVIDENCE_ENVIRONMENT', 'dev'), r'[a-z][a-z0-9-]*', 'environment')
    workspace = Path(env['GITHUB_WORKSPACE'])
    files, missing = collect_paths(env['CI_EVIDENCE_PATH'], workspace, Path(env['RUNNER_TEMP']),
                                   env.get('CI_EVIDENCE_IF_NO_FILES_FOUND', 'error'))
    if not files:
        summary = env.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a') as stream:
                stream.write(f'\nCI evidence `{name}`: no matching files; no S3 artifact published.\n')
        return None
    source = Path(env.get('CI_EVIDENCE_SOURCE_DIRECTORY', str(workspace)))
    if not source.is_absolute():
        source = workspace / source
    if not source.resolve().is_relative_to(workspace.resolve()):
        raise ValueError('Source checkout must be inside the workspace')
    checked_out = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    token(checked_out, r'[0-9a-f]{40}', 'checked-out revision')
    workflow_sha = token(env['GITHUB_WORKFLOW_SHA'], r'[0-9a-f]{40}', 'workflow revision')
    account = token(aws('sts', 'get-caller-identity')['Account'], r'[0-9]{12}', 'AWS account')
    bucket = f'adp-{environment}-ci-evidence-{account}'
    prefix = f'artifacts/{retention}/{repository}/{run_id}/{attempt}/{job}/{name}'
    with tempfile.TemporaryDirectory(prefix='ci-evidence-', dir=env['RUNNER_TEMP']) as temporary:
        packed = Path(temporary) / 'evidence.tar.gz'
        records = archive(files, packed)
        sha = hashlib.sha256(packed.read_bytes()).hexdigest()
        artifact = upload(bucket, f'{prefix}/{sha}.tar.gz', packed)
        manifest = {'schema_version': 1, 'repository': repository, 'run_id': run_id,
                    'run_attempt': attempt, 'job': job, 'name': name, 'retention_days': int(retention),
                    'checked_out_revision': checked_out, 'workflow_revision': workflow_sha,
                    'artifact': artifact, 'files': records}
        if missing:
            manifest['missing_path_count'] = missing
        manifest_file = Path(temporary) / 'manifest.json'
        manifest_file.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        manifest_sha = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
        receipt = upload(bucket, f'{prefix}/{manifest_sha}.manifest.json', manifest_file)
    uri = f's3://{bucket}/{receipt["key"]}'
    print(json.dumps({'manifest': receipt, 'artifact': artifact, 'checked_out_revision': checked_out}))
    summary = env.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write(f'\n### CI evidence: {name}\n\nPrivate manifest: `{uri}`\n\n'
                         f'Version: `{receipt["version_id"]}` · SHA-256: `{receipt["sha256"]}`\n\n'
                         f'Checked-out revision: `{checked_out}` · retention: {retention} days.\n')
    output = env.get('GITHUB_OUTPUT')
    if output:
        with open(output, 'a') as stream:
            stream.write(f'manifest-uri={uri}\nmanifest-version={receipt["version_id"]}\n')
    return receipt


if __name__ == '__main__':
    runner_credentials()
    main()
