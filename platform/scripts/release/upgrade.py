#!/usr/bin/env python3
"""One release, full upgrade, mandatory acceptance, then record success."""
import argparse
import datetime
import json
import os
import signal
import sys
from pathlib import Path
import subprocess
import tempfile
import uuid

from common import *
import artifacts
import acceptance
import storage


def release_lock(dynamo, lock, owner, deployment_error=None):
    """Delete only this run's lock without hiding an earlier deployment error."""
    try:
        dynamo.delete_item(
            TableName='adp-terraform-locks',
            Key=lock,
            ConditionExpression='#owner = :owner',
            ExpressionAttributeNames={'#owner': 'Owner'},
            ExpressionAttributeValues={':owner': {'S': owner}},
        )
    except Exception as error:
        if deployment_error is None:
            raise
        print(f'Release lock cleanup failed after deployment error: {error}', flush=True)


def integration_gate(manifest, manifest_sha, evidence):
    if not (evidence.get('status') == 'passed' and evidence.get('account') == ACCOUNTS['integration-test']
            and evidence.get('manifest_sha256') == manifest_sha and evidence.get('source_sha') == manifest['source_sha']
            and evidence.get('release_id') == manifest['release_id']):
        raise ValueError('Pre-production requires successful integration evidence for this exact release')


def check_module_scope(s3, bucket):
    """Refuse installed modules that the core update would touch without artifacts."""
    keys = [item['Key'] for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix='dev/') for item in page.get('Contents', [])]
    for key in keys:
        # Superplane is deployed from its own module and is outside deploy-all.
        # Agent context is in deploy-all's installed-module update scope.
        if key.endswith('terraform.tfstate') and '/agent-context/' in key:
            state = json.loads(s3.get_object(Bucket=bucket, Key=key)['Body'].read())
            if state.get('resources'):
                raise ValueError('Release contract does not cover installed agent-context')


def upgrade(directory, environment, evidence_directory, integration_evidence=None):
    import boto3
    import botocore.exceptions
    directory = directory.resolve()
    account = ACCOUNTS[environment]
    manifest = load(directory)
    check_source(manifest)
    identity(account)
    if environment == 'pre-production':
        if not integration_evidence:
            raise ValueError('Pre-production requires --integration-evidence')
        integration_gate(manifest, sha256(directory / 'manifest.json'), json.loads(integration_evidence.read_text()))
    s3 = storage.client()
    bucket = f'adp-terraform-state-{account}'
    # This contract covers platform, gateway, factory and webhook. Optional
    # modules with their own deploy flows remain outside this release.
    check_module_scope(s3, bucket)
    owner = str(uuid.uuid4())
    lock = {'LockID': {'S': f'adp-release-upgrade/{account}'}}
    dynamo = boto3.client('dynamodb', region_name=REGION)
    dynamo.put_item(TableName='adp-terraform-locks', Item=dict(lock, Owner={'S': owner}, Release={'S': manifest['release_id']}), ConditionExpression='attribute_not_exists(LockID)')
    evidence_directory.mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix=f'adp-release-upgrade-{account}-'))
    os.chmod(private, 0o700)
    journal = {'account_id': account, 'environment': 'dev', 'source_head': manifest['source_sha'],
               'release_id': manifest['release_id'], 'upgrade': {'run_directory': str(private)}, 'phases': {}}
    result = {'status': 'failed', 'account': account, 'environment': environment,
              'release_id': manifest['release_id'], 'source_sha': manifest['source_sha'],
              'manifest_sha256': sha256(directory / 'manifest.json')}
    process = None
    previous_key = f'adp-release-status/{environment}/current.json'
    try:
        try:
            previous = json.loads(s3.get_object(Bucket=bucket, Key=previous_key)['Body'].read())
        except botocore.exceptions.ClientError as error:
            if error.response['Error']['Code'] != 'NoSuchKey':
                raise
            previous = None
        result['previous_release'] = previous
        journal['phases']['release_artifacts'] = {'status': 'running'}
        json_write(ROOT / '.adp-deploy-state.json', journal)
        # Journal is tracked in the legacy repo; prepare checks source before
        # journal generation. The source guard above is the authoritative one.
        artifacts.prepare(directory, environment, private / 'prepared.json', source_checked=True)
        journal['phases']['release_artifacts'] = {'status': 'complete'}
        journal['phases']['upgrade'] = {'status': 'running'}
        json_write(ROOT / '.adp-deploy-state.json', journal)
        env = dict(os.environ, AWS_REGION=REGION, AWS_DEFAULT_REGION=REGION,
                   ADP_RELEASE_DIR=str(directory), ADP_RELEASE_PREPARED=str(private / 'prepared.json'),
                   UPGRADE_RUN_DIR=str(private), KUBECONFIG=str(private / 'kubeconfig'), TF_CLI_ARGS_init='-lockfile=readonly')
        env.update(artifacts.environment_values(directory, account))
        # Terraform's human-readable output can include private config. Keep it
        # local; publish only the acceptance allowlist to GitHub Actions.
        log_path = private / 'upgrade.log'
        print(f"Upgrading {environment} ({account}) to {manifest['release_id']}; private log: {log_path}", flush=True)
        with log_path.open('w') as log:
            process = subprocess.Popen(['bash', str(ROOT / 'platform/scripts/deploy-all.sh'), '--update', '--env', 'dev', '--region', REGION],
                                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            while True:
                try:
                    process.wait(timeout=45)
                    break
                except subprocess.TimeoutExpired:
                    print(f'{environment}: upgrade running; private log updated', flush=True)
        if process.returncode:
            raise RuntimeError(f'Full upgrade failed (exit {process.returncode}); inspect the private log')
        journal['phases']['upgrade'] = {'status': 'complete'}
        journal['phases']['verification'] = {'status': 'running'}
        json_write(ROOT / '.adp-deploy-state.json', journal)
        os.environ['KUBECONFIG'] = env['KUBECONFIG']
        result.update(acceptance.check(directory, environment, private))
        result['checked_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        json_write(evidence_directory / 'acceptance.json', result)
        # Success is recorded only after all acceptance checks. Retain the prior
        # release ID/hash, not a recursively growing chain of evidence.
        storage.put_once(s3, bucket, f'adp-release-status/{environment}/attempts/{owner}.json', evidence_directory / 'acceptance.json')
        s3.put_object(Bucket=bucket, Key=previous_key,
                      Body=json.dumps({k: result[k] for k in ('release_id', 'source_sha', 'manifest_sha256', 'checked_at')}).encode(),
                      ServerSideEncryption='AES256')
        journal['phases']['verification'] = {'status': 'complete'}
        print(f'{environment}: all release acceptance checks passed', flush=True)
    except BaseException:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        result['status'] = 'failed'
        for phase in journal['phases'].values():
            if phase['status'] == 'running':
                phase['status'] = 'failed'
        raise
    finally:
        # Retain private diagnostics in the target state bucket, never GitHub
        # artifacts. State/plan files are intentionally not uploaded here.
        log_path = private / 'upgrade.log'
        if log_path.exists():
            try:
                s3.upload_file(str(log_path), bucket, f'adp-release-status/{environment}/private/{owner}/upgrade.log',
                               ExtraArgs={'ServerSideEncryption': 'AES256'})
            except Exception:
                print(f'Private log upload failed; local log remains at {log_path}', flush=True)
        json_write(evidence_directory / 'acceptance.json', result)
        json_write(ROOT / '.adp-deploy-state.json', journal)
        release_lock(dynamo, lock, owner, sys.exc_info()[1])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--environment', required=True, choices=ACCOUNTS)
    parser.add_argument('--evidence-directory', required=True, type=Path)
    parser.add_argument('--integration-evidence', type=Path)
    args = parser.parse_args()
    upgrade(args.directory, args.environment, args.evidence_directory, args.integration_evidence)
