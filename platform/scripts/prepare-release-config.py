#!/usr/bin/env python3
"""Materialize portable release inputs in an isolated source checkout."""
import argparse
import json
from pathlib import Path
import re


def prepare(root, environment, region):
    root = Path(root).resolve()
    if environment not in ('dev', 'staging', 'prod'):
        raise ValueError('Release environment must be dev, staging or prod')
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', region):
        raise ValueError('Invalid AWS region')
    templates = root / 'config/release-defaults'
    destination = root / 'environments' / environment
    if not (destination / 'backend.tfvars').is_file():
        raise ValueError('Release has no backend configuration for this environment')
    writes = []
    for module in ('platform', 'gateway', 'webhook-ingress'):
        source = templates / f'{module}.tfvars'
        content = source.read_text()
        if re.search(r'(?<![0-9])[0-9]{12}(?![0-9])', content):
            raise ValueError(f'Portable {module} defaults contain an AWS account ID')
        target = destination / ('platform.tfvars' if module == 'platform' else f'modules/{module}.tfvars')
        alternate = target.with_suffix(target.suffix + '.json')
        if alternate.exists():
            raise ValueError(f'Ambiguous release configuration: {alternate.name}')
        content += f'\nenvironment = {json.dumps(environment)}\naws_region = {json.dumps(region)}\n'
        writes.append((target, content))
    # Validate every template before replacing any inputs. Original repository
    # settings are retained outside the environment tree for troubleshooting.
    backup = root.parent / 'original-environment-config'
    backup.mkdir(mode=0o700, exist_ok=True)
    for target, content in writes:
        if target.exists():
            archived = backup / target.name
            if not archived.exists():
                archived.write_bytes(target.read_bytes())
                archived.chmod(0o600)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    print('Prepared portable release configuration; target state overrides defaults during upgrades.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--env', required=True)
    parser.add_argument('--region', required=True)
    args = parser.parse_args()
    prepare(args.root, args.env, args.region)
