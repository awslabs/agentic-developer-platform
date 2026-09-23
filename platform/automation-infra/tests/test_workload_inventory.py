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
