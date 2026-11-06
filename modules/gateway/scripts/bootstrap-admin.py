#!/usr/bin/env python3
"""Bootstrap an explicitly selected operator administrator without shared passwords."""
import argparse
import json
import os
import re
import subprocess
import sys


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env', default=os.environ.get('ADP_ENV', 'dev'))
    p.add_argument('--region', default=os.environ.get('AWS_REGION', 'us-east-1'))
    p.add_argument('--email', default=os.environ.get('ADP_BOOTSTRAP_ADMIN_EMAIL', ''))
    p.add_argument('--pool-id', default=os.environ.get('ADP_BOOTSTRAP_ADMIN_POOL_ID', ''))
    p.add_argument('--use-test-admin', action='store_true',
                   default=os.environ.get('ADP_BOOTSTRAP_USE_TEST_ADMIN') == 'true')
    p.add_argument('--org', default='platform-admin')
    p.add_argument('--role', choices=('platform_admin', 'org_admin'), default='platform_admin')
    p.add_argument('--namespace', default='adp-gateway')
    p.add_argument('--deploy', default='bedrockgateway')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--check-config', action='store_true', help='Validate selection without contacting AWS')
    return p


def validate_selection(args):
    if args.env not in ('dev', 'test', 'staging', 'prod'):
        raise ValueError('Unsupported deployment environment')
    if args.use_test_admin:
        if args.env not in ('dev', 'test'):
            raise ValueError('--use-test-admin is allowed only in dev/test')
        if args.email:
            raise ValueError('Select either an operator email or --use-test-admin, not both')
    elif not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', args.email):
        raise ValueError('Set ADP_BOOTSTRAP_ADMIN_EMAIL or --email to the operator administrator email. '
                         'Dev/test fixtures require explicit --use-test-admin and create_test_users=true.')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,62}[a-z0-9]', args.org) or args.org in ('admin', 'system', 'api', 'root', 'internal', 'console', 'platform', 'null'):
        raise ValueError('Use a valid, unreserved organization slug (default: platform-admin)')


def aws(args, *command):
    # Never echo stdout: admin-create-user and Secrets Manager can return passwords.
    return subprocess.run(['aws', *command, '--region', args.region, '--output', 'json', '--no-cli-pager'],
                          capture_output=True, text=True, check=True)


def attributes(user):
    return {item['Name']: item['Value'] for item in user.get('UserAttributes', user.get('Attributes', []))}


def bootstrap(args):
    validate_selection(args)
    if args.check_config:
        return
    if args.use_test_admin:
        secret = json.loads(json.loads(aws(args, 'secretsmanager', 'get-secret-value', '--secret-id',
                                          f'adp/{args.env}/gateway/test-admin-credentials').stdout)['SecretString'])
        args.email = secret['username']
        args.pool_id = args.pool_id or secret['cognito_user_pool_id']
    if not args.pool_id:
        args.pool_id = json.loads(aws(args, 'ssm', 'get-parameter', '--name',
                                     f'/adp/{args.env}/gateway/cognito-user-pool-id').stdout)['Parameter']['Value']
    try:
        user = json.loads(aws(args, 'cognito-idp', 'admin-get-user', '--user-pool-id', args.pool_id,
                              '--username', args.email).stdout)
    except subprocess.CalledProcessError as exc:
        if 'UserNotFoundException' not in exc.stderr or args.use_test_admin:
            raise
        if args.dry_run:
            print('Would invite the selected administrator, seed database records and verify Cognito claims.')
            return
        # Cognito generates and emails a temporary password. The operator must
        # change it on first sign-in; no permanent password or secret is created.
        user = json.loads(aws(args, 'cognito-idp', 'admin-create-user', '--user-pool-id', args.pool_id,
                              '--username', args.email, '--desired-delivery-mediums', 'EMAIL',
                              '--user-attributes', json.dumps([{'Name': 'email', 'Value': args.email},
                                                             {'Name': 'name', 'Value': args.email}])).stdout)['User']
    sub = attributes(user).get('sub')
    if not sub or user.get('Enabled') is False:
        raise ValueError('The selected Cognito user must exist, be enabled and have a subject ID')
    if args.dry_run:
        print('Would seed the selected administrator database records and verify Cognito claims; no changes made.')
        return
    # All operator values are separate argv entries, never evaluated by a shell.
    result = subprocess.run(['kubectl', 'exec', f'deploy/{args.deploy}', '-n', args.namespace, '--',
                             'python', '-m', 'src.admin.onboarding.bootstrap_admin', '--cognito-sub', sub,
                             '--email', args.email, '--role', args.role, '--org', args.org],
                            capture_output=True, text=True, check=True)
    seeded = json.loads(result.stdout)
    if (seeded.get('status') not in ('bootstrapped', 'already-bootstrapped')
            or seeded.get('org_id') != args.org or seeded.get('role') != args.role):
        raise ValueError('Database bootstrap did not confirm the requested administrator role and organization. '
                         'Deploy the current gateway image before retrying; no Cognito role claims were written.')
    claims = [{'Name': 'custom:role', 'Value': args.role}, {'Name': 'custom:org_id', 'Value': args.org}]
    aws(args, 'cognito-idp', 'admin-update-user-attributes', '--user-pool-id', args.pool_id,
        '--username', args.email, '--user-attributes', json.dumps(claims))
    actual = attributes(json.loads(aws(args, 'cognito-idp', 'admin-get-user', '--user-pool-id', args.pool_id,
                                       '--username', args.email).stdout))
    if any(actual.get(item['Name']) != item['Value'] for item in claims):
        raise ValueError('Cognito administrator claims did not match after bootstrap; retry bootstrap before signing in')
    print('Administrator bootstrap verified. Sign in with the selected email and complete any invitation password change.')


def main():
    try:
        bootstrap(parser().parse_args())
    except subprocess.CalledProcessError as exc:
        # AWS and kubectl output may contain credentials; show only the error code.
        match = re.search(r'\(([A-Za-z0-9]+Exception)\)', exc.stderr or '')
        code = match.group(1) if match else f'exit {exc.returncode}'
        print(f'Bootstrap failed ({code}) during {exc.cmd[0]}. Check operator permissions and deployment health; '
              'retry with the same selected administrator. No password was printed.', file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError) as exc:
        # Do not include malformed command output in the diagnostic.
        message = str(exc) if type(exc) is ValueError else 'Invalid response or unavailable bootstrap dependency'
        print(f'Bootstrap failed: {message}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
