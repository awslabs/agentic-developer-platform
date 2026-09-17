#!/usr/bin/env python3
"""Validate workflow inputs and expose only verified manifest coordinates."""
import argparse
import os
from pathlib import Path

from common import *

# Initial designated approver. Changes to this list are reviewed code changes.
APPROVER_IDS = {'20402445'}  # PranavSharma1000


def approver(env):
    if not (env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
            and env.get('GITHUB_REF') == 'refs/heads/main'
            and env.get('GITHUB_WORKFLOW_REF') == 'aws-e/adp/.github/workflows/adp-release-promote.yml@refs/heads/main'
            and env.get('GITHUB_ACTOR_ID') in APPROVER_IDS
            and env.get('GITHUB_TRIGGERING_ACTOR') == env.get('GITHUB_ACTOR')):
        raise ValueError('Pre-production requires a manual promotion dispatch by a designated approver')


def integration_run(run_id):
    if not re.fullmatch(r'[1-9][0-9]*', run_id):
        raise ValueError('Integration run ID must be numeric')
    value = json.loads(run(['gh', 'api', f'repos/aws-e/adp/actions/runs/{run_id}'], capture=True))
    if not (value['conclusion'] == 'success' and value['status'] == 'completed'
            and value['event'] == 'workflow_dispatch' and value['head_branch'] == 'main'
            and value['repository']['full_name'] == 'aws-e/adp'
            and value['path'] == '.github/workflows/adp-release.yml'):
        raise ValueError('Promotion requires a successful integration release workflow on main')


def evidence_coordinates(path):
    value = json.loads(path.read_text())
    if value.get('status') != 'passed' or value.get('account') != ACCOUNTS['integration-test']:
        raise ValueError('Integration acceptance did not pass')
    valid_id(value['release_id'])
    if not re.fullmatch('[0-9a-f]{40}', value['source_sha']) or not re.fullmatch('[0-9a-f]{64}', value['manifest_sha256']):
        raise ValueError('Invalid integration release coordinates')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        for key in ('release_id', 'source_sha', 'manifest_sha256'):
            output.write(f'{key}={value[key]}\n')


def target(environment, account):
    if ACCOUNTS.get(environment) != account:
        raise ValueError('Unsupported environment/account pairing')


def coordinates(directory, checkout=False):
    manifest = load(directory)
    source = manifest['source_sha']
    run(['git', 'merge-base', '--is-ancestor', source, 'origin/main'], capture=True)
    if checkout:
        if source != os.environ['SOURCE_SHA']:
            raise ValueError('Release source differs from workflow source')
        run(['git', 'checkout', '--detach', source])
        check_source(manifest)
    else:
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            for key in ('release_id', 'source_sha'):
                output.write(f'{key}={manifest[key]}\n')
            output.write(f'manifest_sha256={sha256(directory / "manifest.json")}\n')
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write(f"Release `{manifest['release_id']}`\n\nSource: `{source}`\n\nManifest SHA256: `{sha256(directory / 'manifest.json')}`\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-target', action='store_true')
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--checkout', action='store_true')
    parser.add_argument('--check-approver', action='store_true')
    parser.add_argument('--check-integration-run', action='store_true')
    parser.add_argument('--integration-evidence', type=Path)
    args = parser.parse_args()
    if args.check_approver:
        approver(os.environ)
    elif args.check_integration_run:
        integration_run(os.environ['INTEGRATION_RUN_ID'])
    elif args.integration_evidence:
        evidence_coordinates(args.integration_evidence)
    elif args.check_target:
        target(os.environ['TARGET_ENVIRONMENT'], os.environ['TARGET_ACCOUNT'])
    elif args.directory:
        coordinates(args.directory, args.checkout)
    else:
        parser.error('Choose target validation or a release directory')
