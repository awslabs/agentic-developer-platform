#!/usr/bin/env python3
"""Retain release evidence with the CI uploader; retrieve a successful run's acceptance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

import upload_ci_evidence_s3 as evidence


def prefix(run_id, attempt, job, name):
    for value in (run_id, attempt):
        evidence.token(str(value), r'[1-9][0-9]*', 'run/attempt')
    evidence.token(job, r'[A-Za-z0-9_-]+', 'job')
    evidence.token(name, r'(release-manifest|acceptance-(integration-test|pre-production))', 'release evidence name')
    return f'artifacts/90/aws-e/adp/{run_id}/{attempt}/{job}/{name}'


def runner_bucket():
    # Deployment credentials must not redirect CI evidence to the target account.
    # Keep the runner's AWS_ROLE_ARN/AWS_WEB_IDENTITY_TOKEN_FILE (scoped IRSA).
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                 'AWS_SECURITY_TOKEN', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE'):
        os.environ.pop(name, None)
    account = evidence.token(evidence.aws('sts', 'get-caller-identity')['Account'], r'[0-9]{12}', 'runner account')
    return f'adp-dev-ci-evidence-{account}'


def upload(name, path, bucket):
    if os.environ['GITHUB_REPOSITORY'] != 'aws-e/adp':
        raise ValueError('Release evidence must belong to aws-e/adp')
    base = prefix(os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT'], os.environ['GITHUB_JOB'], name)
    os.environ.update(CI_EVIDENCE_NAME=name, CI_EVIDENCE_PATH=str(path),
                      CI_EVIDENCE_RETENTION_DAYS='90', CI_EVIDENCE_ENVIRONMENT='dev')
    receipt = evidence.main()
    if receipt['bucket'] != bucket or not receipt['key'].startswith(base + '/'):
        raise ValueError('Unexpected CI evidence location')
    # A deterministic, write-once receipt allows retrieval without ListBucket.
    with tempfile.TemporaryDirectory(dir=os.environ['RUNNER_TEMP']) as temp:
        pointer = Path(temp) / 'receipt.json'
        pointer.write_text(json.dumps(receipt, sort_keys=True) + '\n')
        evidence.upload(bucket, base + '/receipt.json', pointer)


def read_object(bucket, key, destination, receipt=None):
    result = evidence.aws('s3api', 'get-object', '--bucket', bucket, '--key', key,
                          '--checksum-mode', 'ENABLED', str(destination))
    data = destination.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    checksum = evidence.base64.b64encode(bytes.fromhex(digest)).decode()
    if (not result.get('VersionId') or result['VersionId'] == 'null'
            or result.get('ServerSideEncryption') != 'AES256'
            or result.get('ChecksumSHA256') != checksum):
        raise ValueError('Invalid S3 evidence checksum/version/encryption')
    if receipt and (receipt['bucket'] != bucket or receipt['key'] != key
                    or receipt['version_id'] != result['VersionId']
                    or receipt['sha256'] != digest or receipt['size'] != len(data)):
        raise ValueError('S3 evidence differs from its immutable receipt')
    return data


def integration_run(run_id):
    evidence.token(run_id, r'[1-9][0-9]*', 'run ID')
    run = json.loads(subprocess.check_output(['gh', 'api', f'repos/aws-e/adp/actions/runs/{run_id}'], text=True))
    if not (str(run['id']) == run_id and run['conclusion'] == 'success' and run['status'] == 'completed'
            and run['event'] == 'workflow_dispatch' and run['head_branch'] == 'main'
            and run['repository']['full_name'] == 'aws-e/adp'
            and run['path'] == '.github/workflows/adp-release.yml'):
        raise ValueError('S3 acceptance requires a successful integration release run on main')
    return run


def download(run_id, directory, bucket):
    run = integration_run(run_id)
    base = prefix(run_id, run['run_attempt'], 'upgrade', 'acceptance-integration-test')
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        receipt = json.loads(read_object(bucket, base + '/receipt.json', temp / 'receipt.json'))
        if (receipt['bucket'] != bucket
                or not re.fullmatch(re.escape(base) + r'/[0-9a-f]{64}\.manifest\.json', receipt['key'])):
            raise ValueError('Acceptance manifest outside the selected run')
        manifest = json.loads(read_object(bucket, receipt['key'], temp / 'manifest.json', receipt))
        expected = {'schema_version': 1, 'repository': 'aws-e/adp', 'run_id': run_id,
                    'run_attempt': str(run['run_attempt']), 'job': 'upgrade',
                    'name': 'acceptance-integration-test', 'retention_days': 90,
                    'workflow_revision': run['head_sha']}
        if any(manifest.get(k) != v for k, v in expected.items()):
            raise ValueError('Acceptance metadata differs from the successful workflow attempt')
        artifact = manifest['artifact']
        if (artifact['bucket'] != bucket or artifact['size'] > 1024 * 1024
                or not re.fullmatch(re.escape(base) + r'/[0-9a-f]{64}\.tar\.gz', artifact['key'])):
            raise ValueError('Invalid acceptance archive location/size')
        archive = temp / 'evidence.tar.gz'
        read_object(bucket, artifact['key'], archive, artifact)
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            if (len(members) != 1 or members[0].name != 'acceptance.json'
                    or not members[0].isreg() or members[0].size > 1024 * 1024):
                raise ValueError('Acceptance archive must contain only acceptance.json')
            body = tar.extractfile(members[0]).read()
        if manifest['files'] != [{'path': 'acceptance.json', 'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body)}]:
            raise ValueError('Acceptance file checksum mismatch')
        acceptance = json.loads(body)
        if (acceptance.get('status') != 'passed' or acceptance.get('environment') != 'integration-test'
                or acceptance.get('source_sha') != manifest['checked_out_revision']):
            raise ValueError('Acceptance did not pass for the recorded source')
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'acceptance.json').write_bytes(body)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    push = sub.add_parser('upload')
    push.add_argument('--name', required=True)
    push.add_argument('--path', type=Path, required=True)
    pull = sub.add_parser('download')
    pull.add_argument('--run-id', required=True)
    pull.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    bucket = runner_bucket()
    if args.operation == 'upload':
        upload(args.name, args.path, bucket)
    else:
        download(args.run_id, args.directory, bucket)
