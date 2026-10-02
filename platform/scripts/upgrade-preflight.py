#!/usr/bin/env python3
"""Read-only compatibility checks before model agreements, network changes or applies.

Snapshots are private local upgrade evidence. No resources are imported or
adopted: untracked operator access and account-setting ownership require review.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[2]
state_tools = load(Path(__file__).with_name('upgrade-state.py'), 'upgrade_state_preflight')
engine = load(ROOT / 'modules/gateway/scripts/sync-gateway-engine.py', 'upgrade_engine_preflight')


def aws(region, *args):
    return json.loads(subprocess.check_output(['aws', *args, '--region', region, '--output', 'json'],
                                            text=True, stderr=subprocess.PIPE))


def account_settings(state):
    """Recover ownership, not repository defaults; keep partial logging resources."""
    rows = list(state_tools.resources(state))
    logging_module = 'module.bedrock_invocation_logging[0]'
    return {
        'manage_ecr_registry_scanning': any(r['type'] == 'aws_ecr_registry_scanning_configuration'
                                          and r.get('module') == 'module.ecr' for r, _ in rows),
        'manage_bedrock_invocation_logging': any(r.get('module') == logging_module for r, _ in rows),
        'bedrock_invocation_logging_enabled': any(r['type'] == 'aws_bedrock_model_invocation_logging_configuration'
                                                and r.get('module') == logging_module for r, _ in rows),
    }


def check_settings(state, region, read=aws):
    settings = account_settings(state)
    if settings['manage_ecr_registry_scanning']:
        live = read(region, 'ecr', 'get-registry-scanning-configuration')['scanningConfiguration']
        if live['scanType'] != 'BASIC':
            raise ValueError('ECR scanning is externally configured but still tracked by ADP. '
                             'Relinquish registry scanning ownership without deletion before upgrading; see platform_upgrades.md.')
    if settings['bedrock_invocation_logging_enabled']:
        live = read(region, 'bedrock', 'get-model-invocation-logging-configuration').get('loggingConfig', {})
        owned = next(a for r, a in state_tools.resources(state, 'aws_bedrock_model_invocation_logging_configuration')
                     if r.get('module') == 'module.bedrock_invocation_logging[0]')
        config = owned['logging_config'][0]
        # Compare destinations rather than provider defaults or delivery toggles.
        for terraform_key, api_key, fields in (
            ('cloudwatch_config', 'cloudWatchConfig', {'log_group_name': 'logGroupName', 'role_arn': 'roleArn'}),
            ('s3_config', 's3Config', {'bucket_name': 'bucketName', 'key_prefix': 'keyPrefix'}),
        ):
            previous = (config.get(terraform_key) or [{}])[0]
            current = live.get(api_key, {})
            if any((previous.get(k) or '') != (current.get(v) or '') for k, v in fields.items()):
                raise ValueError('Bedrock logging destinations differ from ADP state. '
                                 'Resolve ownership without deleting organization logging before upgrading; see platform_upgrades.md.')
    return settings


def check_operator(state, identity, region, cluster, read=aws):
    arn = identity['Arn']
    match = re.fullmatch(r'arn:[^:]+:sts::[0-9]+:assumed-role/([^/]+)/.+', arn)
    if match:
        arn = read(region, 'iam', 'get-role', '--role-name', match[1])['Role']['Arn']
    # These principals are deliberately owned by another platform state.
    if arn.rsplit('/', 1)[-1] in ('adp-release-deploy', cluster.removesuffix('-eks-cluster') + '-trusted-deployment'):
        return
    entries = read(region, 'eks', 'list-access-entries', '--cluster-name', cluster)['accessEntries']
    if arn not in entries:
        return  # Legacy cluster-creator/aws-auth access is tested by kubectl.
    managed = list(state_tools.resources(state, 'aws_eks_access_entry'))
    if not any(r.get('module') == 'module.eks' and r['name'] == 'admins'
               and a.get('principal_arn') == arn for r, a in managed):
        address = 'module.eks.aws_eks_access_entry.admins[' + json.dumps(arn) + ']'
        raise ValueError('Operator EKS access exists outside platform state. Review ownership and import '
                         + address + ' with ID ' + cluster + ':' + arn
                         + ', and its matching cluster-admin policy association, before upgrading. '
                         'Do not import namespace-scoped or externally owned access as cluster-admin.')
    policies = read(region, 'eks', 'list-associated-access-policies', '--cluster-name', cluster,
                    '--principal-arn', arn)['associatedAccessPolicies']
    for policy in policies:
        if policy['policyArn'].endswith('/AmazonEKSClusterAdminPolicy'):
            if policy.get('accessScope', {}).get('type') != 'cluster':
                raise ValueError('Operator access is namespace-scoped; do not promote it to cluster-admin through an upgrade')
            owned = list(state_tools.resources(state, 'aws_eks_access_policy_association'))
            if not any(r.get('module') == 'module.eks' and r['name'] == 'admins'
                       and a.get('principal_arn') == arn and a.get('policy_arn') == policy['policyArn'] for r, a in owned):
                raise ValueError('Operator cluster-admin policy exists outside platform state. Review and import '
                                 'module.eks.aws_eks_access_policy_association.admins[' + json.dumps(arn) + ']'
                                 ' before upgrading; see platform_upgrades.md.')


def prepare(directory, account, region, environment, gateway=True):
    directory = Path(directory)
    identity = aws(region, 'sts', 'get-caller-identity')
    if identity['Account'] != account:
        raise ValueError('Preflight AWS account differs from upgrade target')

    def current_state(module):
        # Resume retains the original integration baseline, but ownership must
        # come from current state after a partial apply or completed import.
        path = directory / (module + '-preflight.tfstate')
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        key = f'{environment}/' + ('platform' if module == 'platform' else 'modules/' + module) + '/terraform.tfstate'
        aws(region, 's3api', 'get-object', '--bucket', f'adp-terraform-state-{account}', '--key', key, str(path))
        return json.loads(path.read_text())

    platform = current_state('platform')
    settings = check_settings(platform, region)
    check_operator(platform, identity, region, f'adp-{environment}-eks-cluster')
    target = directory / 'platform.tfvars.json'
    preserved = json.loads(target.read_text())
    preserved.update(settings)
    state_tools.write_json(target, preserved)
    if gateway:
        installed = current_state('gateway')
        tracked = any(r.get('module') == 'module.orchestration_tick[0]' and r['type'] == 'aws_lambda_function'
                      for r, _ in state_tools.resources(installed))
        digest = engine.current_image_digest(account=account, region=region, environment=environment,
                                             allow_missing=not tracked)
        if digest is not None and not tracked:
            raise ValueError('Engine exists outside gateway state; review and import it before upgrading')
        target = directory / 'gateway.tfvars.json'
        preserved = json.loads(target.read_text())
        saved = state_tools.output(installed, 'release_configuration', {}) or {}
        evidence = directory / 'engine-before.json'
        original = json.loads(evidence.read_text()) if evidence.exists() else {}
        if 'desired_schedule_enabled' in original:
            desired = original['desired_schedule_enabled']
        elif state_tools.output(installed, 'orchestration_tick_upgrade_hold', False):
            desired = saved.get('orchestration_tick_schedule_enabled', False)
        elif tracked:
            # An operator can disable the EventBridge rule without applying
            # Terraform (the documented emergency stop). Preserve the live state.
            rule = aws(region, 'events', 'describe-rule', '--name', f'adp-{environment}-orchestration-tick')
            if rule['State'] not in ('ENABLED', 'DISABLED'):
                raise ValueError('Unexpected engine schedule state')
            desired = rule['State'] == 'ENABLED'
        else:
            desired = True
        if not isinstance(desired, bool):
            raise ValueError('Retained engine schedule setting must be a boolean')
        preserved['orchestration_tick_schedule_enabled'] = desired
        state_tools.write_json(target, preserved)
        state_tools.write_json(evidence, {'missing': digest is None, 'desired_schedule_enabled': desired})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('directory', 'account', 'region', 'environment'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--skip-gateway', action='store_true')
    args = vars(parser.parse_args())
    args['gateway'] = not args.pop('skip_gateway')
    try:
        prepare(**args)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit('Upgrade compatibility preflight failed: ' + str(error))
