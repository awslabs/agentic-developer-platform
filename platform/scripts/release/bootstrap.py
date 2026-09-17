#!/usr/bin/env python3
"""Set up release storage/OIDC roles and protected GitHub environments."""
import argparse
import json
from pathlib import Path
import tempfile

from common import *


def github(method, endpoint, payload=None):
    with tempfile.NamedTemporaryFile(mode='w') as file:
        args = ['gh', 'api', '--method', method, endpoint]
        if payload is not None:
            json.dump(payload, file)
            file.flush()
            args += ['--input', file.name]
        result = subprocess.run(args, text=True, capture_output=True, check=False)
        if result.returncode:
            try:
                reason = json.loads(result.stdout).get('message', 'GitHub API request failed')
            except ValueError:
                reason = 'GitHub API request failed'
            raise RuntimeError(f'{method} {endpoint}: {reason}')
        return json.loads(result.stdout) if result.stdout.strip() else None


def validate_environment(value, name):
    branch_policy = value.get('deployment_branch_policy') or {}
    if branch_policy.get('custom_branch_policies') is not True:
        raise ValueError(f'{name} must restrict deployment branches')


def configure_github():
    for name in ('adp-release-build', 'integration-test', 'pre-production'):
        endpoint = f'repos/aws-e/adp/environments/{name}'
        payload = {'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}}
        value = github('PUT', endpoint, payload)
        policies = github('GET', endpoint + '/deployment-branch-policies')['branch_policies']
        if any(policy['name'] != 'main' or policy.get('type', 'branch') != 'branch' for policy in policies):
            raise ValueError(f'{name} has additional branch policies; refusing to weaken or silently remove existing rules')
        if not policies:
            github('POST', endpoint + '/deployment-branch-policies', {'name': 'main', 'type': 'branch'})
        validate_environment(value, name)
    print('Release environments configured; promotion requires the approved manual workflow dispatch')


def verify_github():
    for name in ('adp-release-build', 'integration-test', 'pre-production'):
        endpoint = f'repos/aws-e/adp/environments/{name}'
        validate_environment(github('GET', endpoint), name)
        policies = github('GET', endpoint + '/deployment-branch-policies')['branch_policies']
        if len(policies) != 1 or policies[0]['name'] != 'main' or policies[0].get('type', 'branch') != 'branch':
            raise ValueError(f'{name} must allow exactly the main branch')


def infrastructure(environment, apply=False):
    account = ACCOUNTS[environment]
    identity(account)
    providers = aws('iam', 'list-open-id-connect-providers')['OpenIDConnectProviderList']
    existing = [p['Arn'] for p in providers if p['Arn'].endswith('/token.actions.githubusercontent.com')]
    directory = ROOT / 'platform/release-infra'
    run(['terraform', 'init', '-input=false', '-reconfigure',
         f'-backend-config=bucket=adp-terraform-state-{account}', '-backend-config=key=release-pipeline/terraform.tfstate',
         '-backend-config=region=us-east-1', '-backend-config=encrypt=true', '-backend-config=dynamodb_table=adp-terraform-locks'], cwd=directory)
    import botocore.exceptions
    import storage
    try:
        state = json.loads(storage.client().get_object(Bucket=f'adp-terraform-state-{account}', Key='release-pipeline/terraform.tfstate')['Body'].read())
    except botocore.exceptions.ClientError as error:
        if error.response['Error']['Code'] != 'NoSuchKey':
            raise
        state = {}
    if any(r['type'] == 'aws_iam_openid_connect_provider' and r['name'] == 'github' and r.get('instances') for r in state.get('resources', [])):
        existing = []  # Continue managing a provider created by this bootstrap.
    with tempfile.TemporaryDirectory() as temp:
        plan = Path(temp) / 'plan'
        run(['terraform', 'plan', '-input=false', f'-var=environment={environment}',
             f'-var=existing_oidc_provider_arn={existing[0] if existing else ""}', f'-out={plan}'], cwd=directory)
        value = json.loads(run(['terraform', 'show', '-json', plan], capture=True, cwd=directory))
        if any(set(r['change']['actions']) & {'delete', 'forget'} for r in value.get('resource_changes', [])):
            raise ValueError('Release bootstrap refuses resource deletion/replacement')
        if apply:
            run(['terraform', 'apply', '-input=false', plan], cwd=directory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--environment', choices=ACCOUNTS)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--configure-github', action='store_true')
    parser.add_argument('--verify-github', action='store_true')
    args = parser.parse_args()
    if args.configure_github:
        configure_github()
    elif args.verify_github:
        verify_github()
    elif args.environment:
        infrastructure(args.environment, args.apply)
    else:
        parser.error('Choose an environment or a GitHub setup operation')
