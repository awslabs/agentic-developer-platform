#!/usr/bin/env python3
"""Restore fixed release build modes without replaying shell credentials."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def resolve(root, environment, region, update):
    root = root.resolve()
    receipts = [p for p in (root.parent / 'release-install.json', root.parent / 'release-upgrade.json') if p.is_file()]
    if root.name != 'source' or not receipts:
        return 'source'
    if len(receipts) != 1:
        raise ValueError('Conflicting retained release receipts; inspect the original deployment')
    receipt = json.loads(receipts[0].read_text())
    if not isinstance(receipt, dict):
        raise ValueError('Retained release receipt must be a JSON object')
    mode = 'upgrade' if update else 'install'
    sha = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    expected = {'repository': 'aws-e/adp', 'source_sha': sha, 'environment': environment,
                'region': region, 'mode': mode, 'configuration': 'portable-release-defaults'}
    if receipts[0].name != f'release-{mode}.json' or any(receipt.get(k) != v for k, v in expected.items()):
        raise ValueError('Retained release source, target or install/update mode differs; use the original resume options')
    account = json.loads(subprocess.check_output(['aws', 'sts', 'get-caller-identity', '--output', 'json']))['Account']
    if receipt.get('account_id') != account:
        raise ValueError('AWS account differs from the retained release deployment')
    for key in ('ADP_RELEASE_BUILD', 'ADP_PORTABLE_RELEASE_CONFIG'):
        if key in os.environ and os.environ[key] != 'true':
            raise ValueError(f'{key} conflicts with the retained release build mode')
    # Output is a fixed selector, never shell code or values copied from a file.
    # The caller exports constants; ordinary source resumes retain their inputs.
    return 'github-release'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--env', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--update', choices=('true', 'false'), required=True)
    args = parser.parse_args()
    try:
        print(resolve(args.root, args.env, args.region, args.update == 'true'))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else 'Cannot verify retained release context'
        parser.exit(2, f'Release resume error: {message}\n')


if __name__ == '__main__':
    main()
