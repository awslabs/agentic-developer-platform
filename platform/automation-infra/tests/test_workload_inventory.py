import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('inventory', Path(__file__).resolve().parents[1] / 'verify-workload-inventory.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)
ROLE = 'arn:aws:iam::123456789012:role/adp-test-worker'
TARGET = 'arn:aws:lambda:us-east-1:123456789012:function:adp-test-worker'


def policy(*actions, resource='*'):
    return {'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':list(actions),'Resource':resource}, {'Effect':'Deny','NotAction':list(actions),'Resource':'*'}, {'Effect':'Deny','Action':list(actions),'NotResource':resource}]}


@pytest.mark.parametrize('action', ['iam:CreateRole', 'iam:PutRolePolicy', 'iam:AttachRolePolicy', 'iam:CreatePolicyVersion', 'iam:UpdateAssumeRolePolicy', 'sts:AssumeRole', 'sts:AssumeRoleWithWebIdentity'])
def test_administrator_identity_cannot_fit_under_workload_ceiling(action):
    with pytest.raises(AssertionError, match='identity mutation/role chaining'):
        inventory.verify_ceiling(policy(action, resource=ROLE), {ROLE}, {TARGET})


def test_unconditional_finite_boundary_caps_even_an_admin_identity_policy():
    boundary = policy('*')
    boundary['Statement'].append({'Effect':'Deny','NotAction':['logs:CreateLogStream','logs:PutLogEvents'],'Resource':'*'})
    inventory.verify_ceiling(boundary, {ROLE}, {TARGET})
    assert inventory.maximum_resources(boundary, 'iam:CreateRole') == set()


def test_an_open_ended_boundary_is_not_a_security_ceiling():
    with pytest.raises(AssertionError, match='enumerate actions'):
        inventory.verify_ceiling(policy('*'), {ROLE}, {TARGET})


@pytest.mark.parametrize('action', ['lambda:UpdateFunctionCode','codebuild:StartBuild','ssm:SendCommand','iam:PassRole'])
def test_existing_service_role_or_passrole_cannot_escape_inventory(action):
    with pytest.raises(AssertionError):
        inventory.verify_ceiling(policy(action), {ROLE}, {TARGET})


def test_scoped_execution_and_passrole_stay_within_verified_graph():
    boundary = policy('lambda:UpdateFunctionCode', 'iam:PassRole', resource=[TARGET, ROLE])
    boundary['Statement'] += [{'Effect':'Deny','Action':['iam:PassRole'],'NotResource':ROLE}, {'Effect':'Deny','Action':['lambda:UpdateFunctionCode'],'NotResource':TARGET}]
    inventory.verify_ceiling(boundary, {ROLE}, {TARGET})


def test_conditional_denies_do_not_prove_an_immutable_ceiling():
    boundary=policy('iam:CreateRole')
    boundary['Statement'].append({'Effect':'Deny','Action':['iam:CreateRole'],'Resource':'*','Condition':{'StringEquals':{'aws:PrincipalTag/safe':'true'}}})
    with pytest.raises(AssertionError):
        inventory.verify_ceiling(boundary,{ROLE},{TARGET})


def test_glob_intersections_cannot_falsely_erase_an_allow():
    boundary=policy('lambda:UpdateFunctionCode',resource=TARGET)
    boundary['Statement'].append({'Effect':'Deny','Action':['lambda:UpdateFunctionCode'],'NotResource':TARGET+'*'})
    assert inventory.maximum_resources(boundary,'lambda:UpdateFunctionCode') == {TARGET}


def test_implicit_boundary_denies_do_not_contain_direct_session_grants():
    boundary = {'Statement':[{'Effect':'Allow','Action':['logs:PutLogEvents'],'Resource':'*'}]}
    with pytest.raises(AssertionError, match='identity mutation/role chaining'):
        inventory.verify_ceiling(boundary, {ROLE}, {TARGET})


@pytest.mark.parametrize('action', ['glue:StartJobRun', 'states:StartExecution', 'sagemaker:CreatePresignedNotebookInstanceUrl', 'future-service:RunAsExistingRole'])
def test_unmodeled_execution_cannot_hide_in_a_finite_api_list(action):
    with pytest.raises(AssertionError, match='Unsupported workload API'):
        inventory.verify_ceiling(policy(action, resource=TARGET), {ROLE}, {TARGET})


def test_unmodeled_action_is_safe_when_explicitly_denied():
    boundary = policy('logs:PutLogEvents', 'glue:StartJobRun')
    boundary['Statement'].append({'Effect':'Deny','Action':'glue:StartJobRun','Resource':'*'})
    inventory.verify_ceiling(boundary, {ROLE}, {TARGET})


@pytest.mark.parametrize('kind,usage', [('PolicyUsers','PermissionsPolicy'), ('PolicyGroups','PermissionsPolicy'), ('PolicyRoles','PermissionsPolicy'), ('PolicyRoles','PermissionsBoundary')])
def test_mutable_policy_cannot_reach_unbounded_identities_or_a_boundary(kind, usage):
    arn = 'arn:aws:iam::123456789012:policy/adp-mutable'
    config = {'account_id':'123456789012','deployment_managed_policy_arns':[arn]}
    def aws(*args):
        if args[1] == 'list-policies':
            return {'Policies':[{'Arn':arn}]}
        if args[1] == 'list-entities-for-policy':
            return {kind:[{'RoleName':'unbounded'}]} if args[-1] == usage else {}
        assert args[1] == 'get-role'
        return {'Role':{'Arn':ROLE+'-unbounded'}}
    with pytest.raises(AssertionError, match='Mutable policy'):
        inventory.verify_mutable_policies(config, {ROLE:'arn:aws:iam::123456789012:policy/ceiling'}, aws)


def test_mutable_policy_only_on_admitted_roles_is_allowed():
    arn = 'arn:aws:iam::123456789012:policy/adp-mutable'
    def aws(*args):
        if args[1] == 'list-policies':
            return {'Policies':[{'Arn':arn}]}
        if args[1] == 'list-entities-for-policy':
            return {'PolicyRoles':[{'RoleName':'adp-test-worker'}]} if args[-1] == 'PermissionsPolicy' else {}
        return {'Role':{'Arn':ROLE}}
    inventory.verify_mutable_policies({'account_id':'123456789012','deployment_managed_policy_arns':[arn]}, {ROLE:'arn:aws:iam::123456789012:policy/ceiling'}, aws)
