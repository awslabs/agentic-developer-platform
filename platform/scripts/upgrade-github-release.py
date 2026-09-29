#!/usr/bin/env python3
"""Install or upgrade from an explicitly selected, published GitHub Release."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote

REPOSITORY = 'aws-e/adp'


def run(command, *, cwd=None):
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f'{command[0]} failed while resolving/preparing the release. '
                           'Check GitHub authentication (gh auth status), repository access and the release tag.')
    return result.stdout.strip()


def resolve(tag):
    if not tag or tag.startswith('-') or any(c.isspace() for c in tag):
        raise ValueError('Specify an exact GitHub Release tag, for example v1.2.0')
    run(['git', 'check-ref-format', f'refs/tags/{tag}'])
    encoded = quote(tag, safe='')
    release = json.loads(run(['gh', 'api', f'repos/{REPOSITORY}/releases/tags/{encoded}']))
    if release.get('draft') or not release.get('published_at') or release.get('tag_name') != tag:
        raise ValueError('The requested tag must have a published GitHub Release')
    # A release's target_commitish may be a moving branch. Resolve the tag itself.
    sha = json.loads(run(['gh', 'api', f'repos/{REPOSITORY}/commits/{encoded}']))['sha']
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('GitHub did not return a full source commit SHA')
    return release, sha


def upgrade(args):
    release, sha = resolve(args.release)
    identity = json.loads(run(['aws', 'sts', 'get-caller-identity', '--output', 'json']))
    print(f"Release: {args.release}\nSource: {sha}\nAccount: {identity['Account']}\n"
          f"Caller: {identity['Arn']}\nRegion: {args.region}\nEnvironment: {args.env}", flush=True)
    mode = 'upgrade' if args.update else 'install'
    flags = (['--update'] if args.update else []) + ['--env', args.env, '--region', args.region]
    if args.local:
        flags.append('--local')
    if args.gateway_only:
        flags.append('--gateway-only')
    if args.dry_run:
        if not args.update:
            print('Dry run: would bootstrap the Terraform state backend using the selected release.')
        print('Dry run: would fetch this source into a clean checkout and run deploy-all.sh ' + ' '.join(flags))
        print('No deployment or Terraform plan was executed.')
        return 0

    # Retain the checkout, journal and diagnostics on failure AND success. Never
    # copy local overrides or dirty files into a release build.
    directory = Path(tempfile.mkdtemp(prefix='adp-github-release-'))
    source = directory / 'source'
    print(f'Release checkout and receipt: {directory}', flush=True)
    run(['git', 'init', '--quiet', str(source)])
    run(['git', '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential',
         'fetch', '--no-tags', '--depth=1', f'https://github.com/{REPOSITORY}.git',
         f'refs/tags/{args.release}'], cwd=source)
    fetched = run(['git', 'rev-parse', 'FETCH_HEAD^{commit}'], cwd=source)
    if fetched != sha:
        raise ValueError('Release tag changed during download; refusing to deploy. Retry after reviewing the release.')
    run(['git', 'checkout', '--quiet', '--detach', sha], cwd=source)
    script = source / 'platform/scripts/deploy-all.sh'
    if not script.is_file():
        raise ValueError('This release does not contain deploy-all.sh')
    if args.update and '--update)' not in script.read_text():
        raise ValueError('This release does not contain an update-capable deploy-all.sh')
    bootstrap = source / 'platform/scripts/bootstrap.sh'
    if not args.update and not bootstrap.is_file():
        raise ValueError('This release does not contain bootstrap.sh for fresh installation')
    receipt = dict(repository=REPOSITORY, release=args.release, release_id=release['id'],
                   source_sha=sha, account_id=identity['Account'], caller_arn=identity['Arn'],
                   environment=args.env, region=args.region, scope='gateway-only' if args.gateway_only else 'full',
                   mode=mode, status='running', started_at=datetime.now(timezone.utc).isoformat())
    receipt_path = directory / f'release-{mode}.json'

    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')

    save()
    try:
        child_env = dict(os.environ, AWS_REGION=args.region, ENVIRONMENT=args.env,
                         ADP_REGION=args.region, ADP_ENVIRONMENT=args.env)
        configuration = source / 'platform/scripts/prepare-release-config.py'
        if not configuration.is_file():
            raise ValueError('This release lacks portable configuration; select rc.2 or a later corrected release')
        subprocess.run(['python3', str(configuration), '--root', str(source),
                        '--env', args.env, '--region', args.region], cwd=source, check=True)
        child_env['ADP_PORTABLE_RELEASE_CONFIG'] = 'true'
        receipt['configuration'] = 'portable-release-defaults'
        save()
        if not args.update:
            result = subprocess.run(['bash', str(bootstrap)], cwd=source, env=child_env)
            if result.returncode:
                receipt['status'] = 'failed'
                receipt['failed_stage'] = 'bootstrap'
                receipt['exit_code'] = result.returncode
                return result.returncode if result.returncode >= 0 else 128 - result.returncode
        result = subprocess.run(['bash', str(script), *flags], cwd=source, env=child_env)
        receipt['status'] = 'complete' if result.returncode == 0 else 'failed'
        receipt['exit_code'] = result.returncode
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    except BaseException as error:
        receipt['status'] = 'failed' if isinstance(error, Exception) else 'interrupted'
        raise
    finally:
        receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
        print(f"Release {mode} {receipt['status']}; receipt: {receipt_path}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True)
    parser.add_argument('--update', action='store_true', help='Upgrade an existing deployment; otherwise install fresh')
    parser.add_argument('--env', default=os.environ.get('ENVIRONMENT', 'dev'))
    parser.add_argument('--region', default=os.environ.get('AWS_REGION', 'us-east-1'))
    parser.add_argument('--local', action='store_true')
    parser.add_argument('--gateway-only', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        return upgrade(args)
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Release deployment failed: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
