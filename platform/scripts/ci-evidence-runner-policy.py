#!/usr/bin/env python3
"""Prepare/apply a guarded CI-object-prefix addition to existing runner policies."""
import argparse
import copy
import json
from pathlib import Path
import re

import boto3


def updated(document, sid, field, source, evidence):
    result = copy.deepcopy(document)
    rows = [row for row in result['Statement'] if row.get('Sid') == sid]
    if len(rows) != 1 or set(rows[0]['Action']) != {'s3:PutObject', 's3:GetObject'}:
        raise ValueError('Runner object policy does not match the reviewed runtime ceiling')
    if rows[0][field] not in ([source], [source, evidence]):
        raise ValueError('Unexpected runner object resources')
    rows[0][field] = [source, evidence]
    if len(json.dumps(result, separators=(',', ':'))) > 6144:
        raise ValueError('Updated policy exceeds IAM managed-policy size limit')
    return result


def save(path, value):
    path = Path(path)
    with path.open('w') as stream:
        path.chmod(0o600)
        json.dump(value, stream, indent=2, default=str)
        stream.write('\n')


def verify_plan(plan, account):
    if plan['account_id'] != account or not re.fullmatch(r'[a-z][a-z0-9-]*', plan['environment']):
        raise ValueError('Account/environment mismatch')
    environment = plan['environment']
    source = f'arn:aws:s3:::adp-terraform-state-{account}/codebuild/src/adp-{environment}-gateway-build-pr/*'
    evidence = f'arn:aws:s3:::adp-{environment}-ci-evidence-{account}/artifacts/*'
    for item in plan['policies']:
        if item['after'] != updated(item['before'], item['sid'], item['field'], source, evidence):
            raise ValueError('Plan contains changes outside the CI object prefix')


def prepare(iam, role_name, account, environment):
    source = f'arn:aws:s3:::adp-terraform-state-{account}/codebuild/src/adp-{environment}-gateway-build-pr/*'
    evidence = f'arn:aws:s3:::adp-{environment}-ci-evidence-{account}/artifacts/*'
    role = iam.get_role(RoleName=role_name)['Role']
    boundary = role['PermissionsBoundary']['PermissionsBoundaryArn']
    attached = iam.list_attached_role_policies(RoleName=role_name)['AttachedPolicies']
    grants = [p['PolicyArn'] for p in attached if p['PolicyName'].endswith(('-runner-base', '-runner-services'))]
    if len(grants) != 2:
        raise ValueError('Expected the two canonical runner grant policies')
    plan = {'account_id': account, 'environment': environment, 'role_name': role_name, 'policies': []}
    for arn in [*sorted(grants), boundary]:
        policy = iam.get_policy(PolicyArn=arn)['Policy']
        version = policy['DefaultVersionId']
        before = iam.get_policy_version(PolicyArn=arn, VersionId=version)['PolicyVersion']['Document']
        sid, field = ('DenyOtherOwnSmokeSourceResources', 'NotResource') if arn == boundary else ('OwnSmokeSource', 'Resource')
        after = updated(before, sid, field, source, evidence)
        versions = iam.list_policy_versions(PolicyArn=arn)['Versions']
        remove = None
        if len(versions) >= 5 and before != after:
            oldest = min((v for v in versions if not v['IsDefaultVersion']), key=lambda v: v['CreateDate'])
            remove = {'version': oldest['VersionId'], 'document': iam.get_policy_version(PolicyArn=arn, VersionId=oldest['VersionId'])['PolicyVersion']['Document']}
        plan['policies'].append({'arn': arn, 'default_version': version, 'sid': sid, 'field': field,
                                 'before': before, 'after': after, 'remove_old_nondefault': remove})
    verify_plan(plan, account)
    return plan


def apply(iam, plan, account, receipt_path):
    verify_plan(plan, account)
    current = prepare(iam, plan['role_name'], account, plan['environment'])
    if current != plan:
        raise ValueError('Live policy changed; prepare and review a fresh plan')
    receipt = {'account_id': account, 'role_name': plan['role_name'], 'updates': []}
    save(receipt_path, receipt)
    for item in plan['policies']:
        if item['before'] == item['after']:
            continue
        # Retain the previous default for rollback. Only an archived oldest
        # nondefault version can be removed to make space in IAM's five slots.
        if iam.get_policy(PolicyArn=item['arn'])['Policy']['DefaultVersionId'] != item['default_version']:
            raise ValueError('Policy default changed during apply')
        remove = item['remove_old_nondefault']
        if remove:
            iam.delete_policy_version(PolicyArn=item['arn'], VersionId=remove['version'])
        result = iam.create_policy_version(PolicyArn=item['arn'], PolicyDocument=json.dumps(item['after']), SetAsDefault=True)
        new = result['PolicyVersion']['VersionId']
        receipt['updates'].append({'arn': item['arn'], 'before_version': item['default_version'], 'after_version': new})
        save(receipt_path, receipt)
        actual = iam.get_policy_version(PolicyArn=item['arn'], VersionId=new)['PolicyVersion']['Document']
        if actual != item['after']:
            raise ValueError('Policy read-back differs from reviewed plan')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', metavar='PRIVATE_JSON')
    mode.add_argument('--apply', metavar='PRIVATE_JSON')
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--environment', default='dev')
    parser.add_argument('--role-name')
    args = parser.parse_args()
    account = boto3.client('sts').get_caller_identity()['Account']
    if account != args.account_id or not re.fullmatch(r'[a-z][a-z0-9-]*', args.environment):
        raise ValueError('Unexpected deployment account/environment')
    iam = boto3.client('iam')
    if args.prepare:
        if not args.role_name:
            parser.error('--role-name is required with --prepare')
        plan = prepare(iam, args.role_name, account, args.environment)
        save(args.prepare, plan)
        print(json.dumps({'plan': args.prepare, 'policies': len(plan['policies']), 'new_scope': f'adp-{args.environment}-ci-evidence-{account}/artifacts/*'}))
    else:
        plan = json.loads(Path(args.apply).read_text())
        receipt = apply(iam, plan, account, args.apply + '.receipt.json')
        print(json.dumps(receipt))


if __name__ == '__main__':
    main()
