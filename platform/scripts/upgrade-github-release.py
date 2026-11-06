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


def run(command, *, cwd=None, umask=-1):
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, umask=umask)
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
    image_profile = getattr(args, 'reuse_images_from', None)
    if image_profile and not args.update:
        raise ValueError('--reuse-images-from requires --update')
    factory_only = getattr(args, 'agent_factory_only', False)
    if factory_only and (not args.update or args.gateway_only):
        raise ValueError('--agent-factory-only requires --update and cannot combine with gateway-only scope')
    dev_box_only = getattr(args, 'dev_box_only', False)
    if dev_box_only and (not args.update or args.gateway_only or factory_only):
        raise ValueError('--dev-box-only requires --update and cannot combine with another scope')
    if dev_box_only and getattr(args, 'dev_box', None) == 'off':
        raise ValueError('--dev-box-only cannot combine with --dev-box off')
    release, sha = resolve(args.release)
    identity = json.loads(run(['aws', 'sts', 'get-caller-identity', '--output', 'json']))
    print(f"Release: {args.release}\nSource: {sha}\nAccount: {identity['Account']}\n"
          f"Caller: {identity['Arn']}\nRegion: {args.region}\nEnvironment: {args.env}", flush=True)
    mode = 'upgrade' if args.update else 'install'
    flags = (['--update'] if args.update else []) + ['--env', args.env, '--region', args.region]
    if getattr(args, 'confirm_destructive', False):
        if not args.update:
            raise ValueError('--confirm-destructive requires --update')
        flags.append('--confirm-destructive')
    if getattr(args, 'allow_known_claude_gap', False):
        flags.append('--allow-known-claude-gap')
    if args.local:
        flags.append('--local')
    if args.gateway_only:
        flags.append('--gateway-only')
    if factory_only:
        flags.append('--agent-factory-only')
    for option in ('chat', 'chat_exception', 'dev_box'):
        value = getattr(args, option, None)
        if value is not None:
            flags.extend(['--' + option.replace('_', '-'), value])
    if dev_box_only:
        flags.append('--dev-box-only')
    if args.dry_run:
        if image_profile:
            print('Would verify and copy existing release images from the selected AWS profile without rebuilding.')
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
    # The enclosing mkdtemp directory stays private (0700). Public source and
    # build artifacts need standard modes for non-root containers and Lambda;
    # do not inherit an operator's 0077 mask into their deployment packages.
    run(['git', 'checkout', '--quiet', '--detach', sha], cwd=source, umask=0o022)
    script = source / 'platform/scripts/deploy-all.sh'
    if not script.is_file():
        raise ValueError('This release does not contain deploy-all.sh')
    if args.update and '--update)' not in script.read_text():
        raise ValueError('This release does not contain an update-capable deploy-all.sh')
    if os.environ.get('ADP_WORKER_MIGRATION_EVIDENCE') and not (source / 'platform/scripts/upgrade-workers.py').is_file():
        raise ValueError('This release does not support scripted worker migration')
    for flag in ('--confirm-destructive', '--allow-known-claude-gap', '--agent-factory-only',
                 '--chat', '--chat-exception', '--dev-box', '--dev-box-only'):
        if flag in flags and flag + ')' not in script.read_text():
            raise ValueError(f'This release does not support {flag}; select a release containing this fix')
    receipt = dict(repository=REPOSITORY, release=args.release, release_id=release['id'],
                   source_sha=sha, account_id=identity['Account'], caller_arn=identity['Arn'],
                   environment=args.env, region=args.region,
                   scope='dev-box-only' if dev_box_only else 'agent-factory-only' if factory_only else 'gateway-only' if args.gateway_only else 'full',
                   mode=mode, status='running', started_at=datetime.now(timezone.utc).isoformat())
    receipt_path = directory / f'release-{mode}.json'

    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2) + '\n')

    save()
    try:
        child_env = dict(os.environ, AWS_REGION=args.region, ENVIRONMENT=args.env,
                         ADP_REGION=args.region, ADP_ENVIRONMENT=args.env,
                         ADP_RELEASE_BUILD='true')
        # CodeBuild must archive the selected commit, not the checkout's private
        # overlays or umask-derived modes (0600 source breaks non-root images).
        configuration = source / 'platform/scripts/prepare-release-config.py'
        if not configuration.is_file():
            raise ValueError('This release lacks portable configuration; select rc.2 or a later corrected release')
        subprocess.run(['python3', str(configuration), '--root', str(source),
                        '--env', args.env, '--region', args.region], cwd=source, check=True)
        child_env['ADP_PORTABLE_RELEASE_CONFIG'] = 'true'
        receipt['configuration'] = 'portable-release-defaults'
        save()
        if image_profile:
            image_receipt = directory / 'image-reuse.json'
            command = ['python3', str(Path(__file__).with_name('reuse-release-images.py')),
                       '--source-profile', image_profile, '--target-account', identity['Account'],
                       '--region', args.region, '--source-sha', sha, '--output', str(image_receipt)]
            if args.gateway_only:
                command.append('--gateway-only')
            receipt['image_reuse_receipt'] = str(image_receipt)
            save()
            subprocess.run(command, check=True)
        # deploy-all owns bootstrap after its preflight and administrator gates.
        # Running bootstrap separately would provision the backend before those
        # checks, duplicate the stage and bypass its recovery checkpoint.
        result = subprocess.run(['bash', str(script), *flags], cwd=source, env=child_env, umask=0o022)
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
    parser.add_argument('--reuse-images-from', help='Reuse unchanged source-SHA images from this AWS profile')
    parser.add_argument('--update', action='store_true', help='Upgrade an existing deployment; otherwise install fresh')
    parser.add_argument('--agent-factory-only', action='store_true', help='Upgrade platform, webhook and agent factory')
    parser.add_argument('--chat', choices=('retire', 'delegated', 'keep-legacy'))
    parser.add_argument('--chat-exception', help='Qualification/exception record for delegated chat')
    parser.add_argument('--dev-box', choices=('on', 'off'))
    parser.add_argument('--dev-box-only', action='store_true', help='Upgrade platform and the optional Dev Box module')
    parser.add_argument('--env', default=os.environ.get('ENVIRONMENT', 'dev'))
    parser.add_argument('--region', default=os.environ.get('AWS_REGION', 'us-east-1'))
    parser.add_argument('--confirm-destructive', action='store_true')
    parser.add_argument('--allow-known-claude-gap', action='store_true')
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
