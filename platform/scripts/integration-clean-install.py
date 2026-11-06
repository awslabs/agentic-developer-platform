#!/usr/bin/env python3
"""Run a confirmed integration reset using only the public teardown/install scripts.

Private evidence goes to the target state bucket; no GitHub artifact storage.
A failed installation is never resumed here: a new attempt repeats teardown.
"""
import datetime
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import tempfile

import boto3

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = '608380991969'
REGION = 'us-east-1'


def commands(tag, dry_run, allow_gap):
    if not re.fullmatch(r'v\d{4}\.\d{2}\.\d{2}-rc\.\d+', tag):
        raise ValueError('An exact release candidate is required')
    teardown = ['bash', str(ROOT / 'platform/scripts/undeploy.sh'), '--environment', 'dev',
                '--region', REGION, '--purge-deleted-secrets', '--yes']
    if dry_run:
        return [('teardown-plan', teardown + ['--dry-run'])]
    install = ['bash', str(ROOT / 'deploy.sh'), '--release', tag, '--env', 'dev', '--region', REGION]
    if allow_gap:
        install.append('--allow-known-claude-gap')
    return [('teardown', teardown), ('install', install)]


def main():
    os.umask(0o077)
    if os.environ.get('CONFIRMED_ACCOUNT') != ACCOUNT:
        raise ValueError('Integration account confirmation required')
    session = boto3.Session(region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise ValueError('AWS identity differs from integration-test')
    tag = os.environ['RELEASE_TAG']
    dry_run = os.environ['CLEAN_INSTALL_DRY_RUN'] == 'true'
    for key in ('CLEAN_INSTALL_DRY_RUN', 'CLEAN_INSTALL_ALLOW_CLAUDE_GAP'):
        if os.environ[key] not in ('true', 'false'):
            raise ValueError('Invalid workflow boolean')
    steps = commands(tag, dry_run, os.environ['CLEAN_INSTALL_ALLOW_CLAUDE_GAP'] == 'true')
    if not dry_run:
        # Validate before teardown, not after deleting the previous installation.
        subprocess.run([sys.executable, str(ROOT / 'modules/gateway/scripts/bootstrap-admin.py'),
                        '--check-config', '--env', 'dev'], check=True)
    run = os.environ['GITHUB_RUN_ID']
    attempt = os.environ['GITHUB_RUN_ATTEMPT']
    if not run.isdigit() or not attempt.isdigit():
        raise ValueError('Invalid workflow run coordinates')
    private = Path(tempfile.mkdtemp(prefix='adp-integration-clean-', dir=os.environ['RUNNER_TEMP']))
    teardown_dir = private / 'teardown'
    work = private / 'work'
    work.mkdir()
    env = dict(os.environ, ADP_TEARDOWN_DIR=str(teardown_dir), ADP_ACCOUNT_ID=ACCOUNT,
               AWS_REGION=REGION, AWS_DEFAULT_REGION=REGION, TMPDIR=str(work))
    bucket = f'adp-terraform-state-{ACCOUNT}'
    prefix = f'adp-clean-install/integration-test/{run}-{attempt}'
    s3 = session.client('s3')
    receipt = {'account': ACCOUNT, 'region': REGION, 'environment': 'dev', 'release': tag,
               'source_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'dry_run': dry_run, 'phases': {}, 'status': 'running'}

    def upload(path):
        s3.upload_file(str(path), bucket, f'{prefix}/{path.name}', ExtraArgs={'ServerSideEncryption': 'AES256'})

    def save():
        receipt['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        path = private / 'run.json'
        path.write_text(json.dumps(receipt, indent=2) + '\n')
        upload(path)

    def interrupted(signum, frame):
        raise KeyboardInterrupt('Workflow interrupted')

    signal.signal(signal.SIGTERM, interrupted)
    process = None
    log_path = None
    phase = None
    save()  # Verify evidence access before starting teardown.
    print(f'Private evidence: s3://{bucket}/{prefix}/', flush=True)
    try:
        for phase, command in steps:
            receipt['phases'][phase] = 'running'
            save()
            log_path = private / f'{phase}.log'
            with log_path.open('w') as log:
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                           stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
                process.stdin.write(ACCOUNT + '\n')
                process.stdin.close()
                while True:
                    try:
                        process.wait(timeout=45)
                        break
                    except subprocess.TimeoutExpired:
                        upload(log_path)
                        print(f'{phase}: running; private log uploaded', flush=True)
            upload(log_path)
            if process.returncode:
                raise RuntimeError(f'{phase} script failed with exit {process.returncode}; inspect private evidence')
            receipt['phases'][phase] = 'complete'
            save()
            print(f'{phase}: complete', flush=True)
        receipt['status'] = 'passed'
    except BaseException:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        receipt['status'] = 'failed'
        if phase:
            receipt['phases'][phase] = 'failed'
        raise
    finally:
        if log_path and log_path.exists():
            upload(log_path)
        # Preserve teardown plans/state for recovery in the same private state bucket.
        if teardown_dir.exists():
            archive = private / 'teardown-evidence.tar.gz'
            with tarfile.open(archive, 'w:gz') as output:
                output.add(teardown_dir, arcname='teardown')
            upload(archive)
        for path in work.glob('adp-github-release-*/release-install.json'):
            upload(path)
        save()


if __name__ == '__main__':
    main()
