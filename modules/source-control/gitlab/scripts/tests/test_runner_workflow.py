"""Exercise live runner gates without AWS/GitLab credentials or job capacity."""
import asyncio
import base64
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
import sys
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runner_workflow as runtime
import runner_delivery as delivery


def fixture():
    account = "123456789012"; region = "us-east-1";group = 42
    secret = f"arn:aws:secretsmanager:{region}:{account}:secret:adp/dev/"
    artifact = {"bucket": f"adp-dev-deployment-evidence-{account}", "key": "gitlab-runners/inputs/approved/plan", "version_id": "version-1", "sha256": "a" * 64}
    config = {"schema_version": 1, "account_id": account, "region": region, "environment": "dev", "source_revision": "b" * 40,
        "correlation": "orch:accepted", "registrar_role_arn": f"arn:aws:iam::{account}:role/registrar", "exporter_role_arn": f"arn:aws:iam::{account}:role/exporter",
        "backend": {"bucket": f"adp-terraform-state-{account}", "key": "dev/modules/gitlab-runners/terraform.tfstate", "region": region, "encrypt": True, "use_lockfile": True},
        "artifacts": {"tfplan": copy.deepcopy(artifact), "release_lock": copy.deepcopy(artifact)}, "plan_sha256": "c" * 64,
        "release_lock_sha256": "a" * 64, "product": {"tenant_id": "tenant", "group_id": group, "product_id": "p", "product_group_id": 43,
            "project_id": 44, "tag_list": ["adp-group-42"], "access_level": "not_protected", "operation_id": "00000000-0000-4000-8000-000000000001", "token_secret_arn": secret + "gitlab-runner/42-AbCdEf"},
        "admin_secret_arn": secret + "gitlab-admin-token-AbCdEf", "admin_user_id": 4, "ca_secret_arn": secret + "gitlab/ca-chain-AbCdEf",
        "ca_version": "ca-version", "ca_sha256": "d" * 64, "instance_url": "https://public.example/gitlab", "runner_url": "https://private.example/gitlab",
        "developer_user_id": 5, "cluster": "cluster", "vpc_id": "vpc-0123456789abcdef0",
        "asg_arn": f"arn:aws:autoscaling:{region}:{account}:autoScalingGroup:uuid:autoScalingGroupName/adp-dev-gitlab-job-42",
        "asg_name": "adp-dev-gitlab-job-42", "launch_template_id": "lt-0123456789abcdef0", "ami_id": "ami-0123456789abcdef0",
        "smoke": {"branch": "owned-smoke", "commit": "e" * 40, "job": "adp_sso_smoke", "artifact": "adp-sso-smoke.json"}}
    env = {e:config[k] for k,e in (("account_id","ACCOUNT_ID"),("region","AWS_REGION"),("environment","ENVIRONMENT"),
        ("source_revision","ADP_SOURCE_REVISION"),("correlation","ADP_CORRELATION"),("registrar_role_arn","ADP_DEPLOY_ROLE_ARN"))}
    env.update(GITHUB_RUN_ID="123",GITHUB_RUN_ATTEMPT="1",GITHUB_REPOSITORY_ID="456",ADP_RUNNER_CONFIG_JSON=json.dumps(config))
    return config,env


def test_protected_runtime_config_binds_original_backend_and_lifecycle():
    config,env=fixture();assert runtime.validate(config,env) is config
    env.update(LIFECYCLE_SOURCE=config['source_revision'],LIFECYCLE_GROUP='42',LIFECYCLE_OPERATION=config['product']['operation_id'],LIFECYCLE_PLAN=config['plan_sha256'])
    assert runtime.validate(config,env) is config


@pytest.mark.parametrize("replacement", [False, True])
def test_manager_list_is_one_unpaginated_request_bound_to_one_owned_pod(replacement):
    integration = runtime.InlineIntegration.__new__(runtime.InlineIntegration)
    integration.config, _ = fixture()
    integration.lock = {"chart": {"runner_version": "19.5.2"}}
    now = datetime.now(timezone.utc)
    registration = {"runner_id": 7, "system_id": "r_" + "a" * 12}
    pod = {"metadata": {"uid": "pod-current", "creationTimestamp": (now - timedelta(seconds=20)).isoformat(),
                        "ownerReferences": [{"kind": "ReplicaSet", "controller": True, "name": "manager-rs", "uid": "rs-uid"}]},
           "spec": {"serviceAccountName": "gitlab-runner"},
           "status": {"phase": "Running", "containerStatuses": [{"ready": True}, {"ready": True}]}}
    deployment = {"metadata": {"generation": 2, "uid": "deployment-uid"},
                  "status": dict(observedGeneration=2, replicas=1, updatedReplicas=1, readyReplicas=1, availableReplicas=1),
                  "spec": {"template": {"metadata": {"annotations": {
                      "adp.aws-e/runner-operation": integration.config["product"]["operation_id"],
                      "adp.aws-e/source-revision": integration.config["source_revision"],
                      "adp.aws-e/rollout-started-at": (now - timedelta(minutes=1)).isoformat()}}}}}
    reads = []
    def kube(*args):
        if args[0] == "rollout": return None
        if args[:2] == ("get", "deployment"): return deployment
        if args[:2] == ("get", "replicaset"):
            return {"metadata": {"uid": "rs-uid", "ownerReferences": [
                {"kind": "Deployment", "controller": True, "uid": "deployment-uid"}]}}
        assert args[:2] == ("get", "pods")
        reads.append(True)
        observed = copy.deepcopy(pod)
        if replacement and len(reads) == 2: observed["metadata"]["uid"] = "replacement"
        return {"items": [observed]}
    integration.kube = kube
    ssm = Mock()
    ssm.get_parameter.side_effect = lambda **kw: {"Parameter": {
        "LastModifiedDate": now - timedelta(seconds=10),
        "Value": "active" if kw["Name"].endswith("manager-alive") else json.dumps(registration)}}
    integration.session = SimpleNamespace(client=lambda _: ssm)
    current = {"system_id": registration["system_id"], "status": "online", "version": "19.5.2",
               "platform": "linux", "architecture": "amd64", "contacted_at": now.isoformat()}
    # The real endpoint returns every record, even when there are over 100.
    managers = [{"system_id": f"old-{i}", "status": "offline"} for i in range(150)] + [current]
    integration.admin = AsyncMock()
    integration.admin.request.return_value = managers
    if replacement:
        with pytest.raises(ValueError, match="runner_manager_changed_during_verification"):
            asyncio.run(integration.manager_uses(7))
    else:
        assert asyncio.run(integration.manager_uses(7)) is True
        assert integration.manager_history["historical_manager_records"] == 150
    integration.admin.request.assert_awaited_once_with("GET", "runners/7/managers")
    integration.admin.pages.assert_not_called()


@pytest.mark.parametrize('paused,drained', [(False, True), (True, False)])
def test_manager_recovery_never_mutates_before_paused_and_idle_proof(monkeypatch, paused, drained):
    integration = runtime.InlineIntegration.__new__(runtime.InlineIntegration)
    integration.admin = AsyncMock()
    integration.admin.request.return_value = {'paused': paused, 'runner_type': 'group_type'}
    integration.drained = AsyncMock(return_value=drained)
    mutation = Mock()
    monkeypatch.setattr(runtime, 'recover_terminating_manager', mutation)
    with pytest.raises(ValueError):
        asyncio.run(integration.switch(7))
    mutation.assert_not_called()


@pytest.mark.parametrize("case", ["same_source", "new_source", "wrong_operation", "paused", "paused_new_source", "rollout_failed"])
def test_finished_registration_maintenance_preserves_pause_drain_and_identity(case):
    integration = runtime.InlineIntegration.__new__(runtime.InlineIntegration)
    integration.config, _ = fixture()
    events = []

    async def request(method, path, **kwargs):
        assert path == "runners/7"
        if method == "GET":
            return {"paused": case in {"paused", "paused_new_source"}, "runner_type": "group_type"}
        assert method == "PUT"
        events.append(("pause", kwargs["json"]["paused"]))

    async def switch(runner_id):
        assert runner_id == 7 and (case == "paused_new_source" or events[-1] == ("pause", True))
        events.append(("switch", runner_id))
        if case == "rollout_failed":
            raise ValueError("runner_post_apply_drift")

    async def verify(runner_id):
        events.append(("verify", runner_id))

    integration.admin = SimpleNamespace(request=request)
    integration.switch = switch
    integration.manager_uses = verify
    integration.kube = Mock(return_value={"spec": {"template": {"metadata": {"annotations": {
        "adp.aws-e/runner-operation": "other" if case == "wrong_operation" else integration.config["product"]["operation_id"],
        "adp.aws-e/source-revision": integration.config["source_revision"] if case in {"same_source", "paused"} else "f" * 40,
    }}}}})
    if case in {"wrong_operation", "paused", "rollout_failed"}:
        with pytest.raises(ValueError):
            asyncio.run(integration.refresh_finished_manager(7))
        assert events == ([("pause", True), ("switch", 7)] if case == "rollout_failed" else [])
    else:
        asyncio.run(integration.refresh_finished_manager(7))
        assert events == ([("verify", 7)] if case == "same_source" else
                          ([("switch", 7), ("verify", 7), ("pause", False)] if case == "paused_new_source" else
                           [("pause", True), ("switch", 7), ("verify", 7), ("pause", False)]))


@pytest.mark.parametrize('already_terminating', [False, True])
def test_terminating_pod_recovery_waits_for_reviewed_configuration_during_apply(monkeypatch, tmp_path, already_terminating):
    integration = runtime.InlineIntegration.__new__(runtime.InlineIntegration)
    integration.config, _ = fixture()
    integration.directory = tmp_path
    integration.lock = {'manager_guard_sha256': runtime.hashlib.sha256(b'corrected guard').hexdigest()}
    applying, recovered = threading.Event(), threading.Event()
    reads = []

    def terraform(*args):
        applying.set()
        assert recovered.wait(3), 'Helm waits for recovery; recovery must run during apply'
        return SimpleNamespace(returncode=0)

    def kube(*args):
        if args[:2] == ('get', 'pods'):
            return {'items': [{'metadata': {'deletionTimestamp': '2026-01-01T00:00:00Z'} if already_terminating else {}}]}
        if args[:2] == ('get', 'deployment'):
            assert applying.wait(1)
            reads.append('deployment')
            return {'spec': {'template': {'spec': {'containers': [
                {'name': 'gitlab-runner', 'env': [{'name': 'HOME', 'value': '/' if len(reads) == 1 else '/home/gitlab-runner'}]},
                {'name': 'manager-guard', 'readinessProbe': {'exec': {'command': ['guard']}}}]}}}}
        assert args[:2] == ('get', 'configmap')
        return {'data': {'prepare_manager.py': 'old guard' if len(reads) < 3 else 'corrected guard'}}

    def recover(*args):
        assert len(reads) >= 3 and applying.is_set()
        recovered.set()

    integration.kube = kube
    monkeypatch.setattr(runtime, 'terraform', terraform)
    monkeypatch.setattr(runtime, 'recover_terminating_manager', recover)
    monkeypatch.setattr(runtime.time, 'sleep', lambda _: None)
    assert integration.apply_manager().returncode == 0
    assert recovered.is_set()


@pytest.mark.parametrize('changed',['account','source','backend','role','other_group','plan','lifecycle','unversioned','lock','public_artifact'])
def test_invalid_runtime_input_refused_before_any_provider_operation(changed):
    config,env=fixture()
    if changed=='account':config['account_id']='999999999999'
    elif changed=='source':config['source_revision']='f'*40
    elif changed=='backend':config['backend']['key']='dev/platform/terraform.tfstate'
    elif changed=='role':config['registrar_role_arn']='arn:aws:iam::999999999999:role/other'
    elif changed=='other_group':config['product']['tag_list']=['adp-group-99']
    elif changed=='plan':config['plan_sha256']='not-reviewed'
    elif changed=='lifecycle':env['LIFECYCLE_GROUP']='99'
    elif changed=='unversioned':config['artifacts']['tfplan']['version_id']='null'
    elif changed=='lock':config['release_lock_sha256']='f'*64
    else:config['artifacts']['tfplan']['bucket']='public-other'
    with pytest.raises(ValueError):runtime.validate(config,env)


def test_private_terraform_failure_never_exposes_provider_output(monkeypatch,tmp_path,capsys):
    monkeypatch.setattr(runtime.subprocess,'run',Mock(return_value=SimpleNamespace(returncode=1,stdout=b'glrt-secret-canary',stderr=b'private-state')))
    with pytest.raises(ValueError,match='runner_terraform_failed'):runtime.terraform(tmp_path,'apply','reviewed.tfplan')
    captured=capsys.readouterr()
    assert not captured.out and not captured.err
    path=tmp_path/'terraform-failure.json'
    failure=json.loads(path.read_text())
    assert failure['phase']=='apply' and failure['exit_code']==1
    assert base64.b64decode(failure['stdout_base64'])==b'glrt-secret-canary'
    assert base64.b64decode(failure['stderr_base64'])==b'private-state'
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):runtime.terraform(tmp_path,'plan')
    assert json.loads(path.read_text())==failure


def test_changed_plan_is_not_a_failure_diagnostic(monkeypatch,tmp_path):
    monkeypatch.setattr(runtime.subprocess,'run',Mock(return_value=SimpleNamespace(returncode=2,stdout=b'plan',stderr=b'')))
    assert runtime.terraform(tmp_path,'plan',allowed=(0,2)).returncode==2
    assert not (tmp_path/'terraform-failure.json').exists()


def test_failure_diagnostic_uses_private_versioned_conditional_write(tmp_path):
    config,env=fixture()
    env.update(GITHUB_REPOSITORY_ID='123',GITHUB_RUN_ID='456',GITHUB_RUN_ATTEMPT='1')
    body=b'{"stderr_base64":"cHJpdmF0ZQ=="}'
    (tmp_path/'terraform-failure.json').write_bytes(body)
    s3=Mock()
    s3.put_object.return_value={'VersionId':'v1'}
    response={'Body':io.BytesIO(body),'VersionId':'v1','ServerSideEncryption':'AES256'}
    s3.get_object.return_value=response
    runtime.retain_failure(config,tmp_path,SimpleNamespace(client=lambda _:s3),env)
    s3.put_object.assert_called_once_with(Bucket='adp-dev-deployment-evidence-123456789012',
        Key='gitlab-runner-diagnostics/repos/123/runs/456/attempts/1/terraform-failure.json',
        Body=body,ContentType='application/json',ServerSideEncryption='AES256',IfNoneMatch='*',ExpectedBucketOwner='123456789012')
    assert s3.get_object.call_args.kwargs['VersionId']=='v1' and response['Body'].closed


def test_failure_diagnostic_rejects_unversioned_write(tmp_path):
    config,env=fixture();env.update(GITHUB_REPOSITORY_ID='123',GITHUB_RUN_ID='456',GITHUB_RUN_ATTEMPT='1')
    (tmp_path/'terraform-failure.json').write_bytes(b'{}')
    s3=Mock();s3.put_object.return_value={'VersionId':'null'}
    with pytest.raises(ValueError,match='runner_diagnostic_version_missing'):
        runtime.retain_failure(config,tmp_path,SimpleNamespace(client=lambda _:s3),env)
    s3.get_object.assert_not_called()


def infrastructure():
    c,e=fixture();clients={n:Mock() for n in ('ec2','eks','autoscaling')}
    clients['eks'].describe_cluster.return_value={'cluster':{'status':'ACTIVE','resourcesVpcConfig':{'vpcId':c['vpc_id']}}}
    group={'AutoScalingGroupARN':c['asg_arn'],'LaunchTemplate':{'LaunchTemplateId':c['launch_template_id'],'Version':'1'},'VPCZoneIdentifier':'subnet-private'}
    clients['autoscaling'].describe_auto_scaling_groups.return_value={'AutoScalingGroups':[group]}
    template={'ImageId':c['ami_id'],'NetworkInterfaces':[{'AssociatePublicIpAddress':False}],'MetadataOptions':{'HttpTokens':'required'}}
    clients['ec2'].describe_launch_template_versions.return_value={'LaunchTemplateVersions':[{'LaunchTemplateData':template}]}
    subnet={'VpcId':c['vpc_id'],'MapPublicIpOnLaunch':False};clients['ec2'].describe_subnets.return_value={'Subnets':[subnet]}
    image={'State':'available','Public':False};clients['ec2'].describe_images.return_value={'Images':[image]}
    lock={'host_ami':{c['region']:{'image_id':c['ami_id']}}}
    return c,SimpleNamespace(client=lambda n:clients[n]),lock,group,template,subnet,image


def test_live_same_vpc_and_private_ami_readbacks_are_required():
    c,sdk,lock,*_=infrastructure();observed=runtime.observe_infrastructure(c,sdk,lock)
    assert observed['same-vpc-private-asg']['vpc_id']==c['vpc_id']
    assert observed['qualified-image-lock']['ami_id']==c['ami_id']


@pytest.mark.parametrize('changed',['asg','launch_template','ami','public_ip','empty_interfaces','imds','vpc','public_subnet','public_ami','lock'])
def test_runtime_infrastructure_mismatch_cannot_mint_evidence(changed):
    c,sdk,lock,g,t,s,i=infrastructure()
    if changed=='asg':g['AutoScalingGroupARN']='another'
    elif changed=='launch_template':g['LaunchTemplate']['LaunchTemplateId']='lt-another'
    elif changed=='ami':t['ImageId']='ami-other'
    elif changed=='public_ip':t['NetworkInterfaces'][0]['AssociatePublicIpAddress']=True
    elif changed=='empty_interfaces':t['NetworkInterfaces']=[]
    elif changed=='imds':t['MetadataOptions']['HttpTokens']='optional'
    elif changed=='vpc':s['VpcId']='vpc-other'
    elif changed=='public_subnet':s['MapPublicIpOnLaunch']=True
    elif changed=='public_ami':i['Public']=True
    else:lock['host_ami'][c['region']]['image_id']='ami-other'
    with pytest.raises(ValueError):runtime.observe_infrastructure(c,sdk,lock)


def pipeline_setup(config,change=None):
    admin=AsyncMock();posts=[]
    async def request(method,path,**kwargs):
        if '/repository/branches/' in path:return {'commit':{'id':config['smoke']['commit'] if change!='branch' else 'f'*40}}
        if method=='POST':posts.append(kwargs);return {'id':100,'sha':config['smoke']['commit']}
        if path.endswith('/artifacts/adp-sso-smoke.json'):
            return {'CI_COMMIT_SHA':config['smoke']['commit'],'CI_PIPELINE_ID':'100','CI_JOB_ID':'101','ADP_VERIFICATION_ID':config['correlation'] if change!='artifact' else 'stale'}
        return {'id':100,'status':'failed' if change=='failed' else 'success'}
    admin.request.side_effect=request
    async def pages(path,**kwargs):
        if path.endswith('/jobs'):return [{'id':101,'name':'adp_sso_smoke','status':'success','runner':{'id':99 if change=='runner' else 7},'commit':{'id':config['smoke']['commit']}}]
        return []
    admin.pages.side_effect=pages
    return admin,posts


def test_real_product_pipeline_requires_assigned_runner_and_bound_artifact(tmp_path):
    c,_=fixture();admin,posts=pipeline_setup(c)
    observed,details=asyncio.run(runtime.pipeline(c,admin,7,tmp_path))
    assert observed['artifact_verified'] and details=={'pipeline_id':100,'job_id':101}
    assert len(posts)==1 and posts[0]['json']['variables'][0]['value']==c['correlation']


@pytest.mark.parametrize('change',['branch','failed','runner','artifact'])
def test_wrong_or_failed_product_pipeline_cannot_pass(tmp_path,change):
    c,_=fixture();admin,posts=pipeline_setup(c,change)
    with pytest.raises(ValueError):asyncio.run(runtime.pipeline(c,admin,7,tmp_path))
    if change=='branch':assert not posts


def observations(c):
    p=c['product'];return {
        'eks-manager-ready':{'group_id':p['group_id'],'ready_replicas':1,'live_managers':1},
        'same-vpc-private-asg':{'vpc_id':c['vpc_id'],'asg_arn':c['asg_arn'],'public_ip':False},
        'qualified-image-lock':{'release_lock_sha256':c['release_lock_sha256'],'ami_id':c['ami_id']},
        'project-ci-enabled':{'project_id':p['project_id'],'enabled':True,'shared_runners':False,'developer_user_id':c['developer_user_id'],'developer_access':30},
        'adp-project-runner-assignment':{'project_id':p['project_id'],'group_id':p['group_id'],'assigned':True},
        'legal-pipeline-ran':{'project_id':p['project_id'],'commit':c['smoke']['commit'],'status':'success','artifact_verified':True},
        'idle-scale-to-zero':{'asg_arn':c['asg_arn'],'desired_capacity':0,'instances':0,'active_jobs':0}}


def test_each_live_observation_is_written_once_and_read_back_at_exact_version():
    c,e=fixture();s3=Mock();saved={}
    s3.get_bucket_versioning.return_value={'Status':'Enabled'}
    def put(**kw):
        assert kw['IfNoneMatch']=='*' and kw['ServerSideEncryption']=='AES256';saved[kw['Key']]=kw['Body'];return {'VersionId':'version1'}
    s3.put_object.side_effect=put;s3.get_object.side_effect=lambda **kw:{'VersionId':'version1','Body':io.BytesIO(saved[kw['Key']])}
    receipt=runtime.publish(c,e,s3,observations(c),{'pipeline_id':100})
    assert len(saved)==7 and set(delivery.verify(receipt,e,c['account_id'],c['source_revision']))==delivery.LIVE_CHECKS
    for check in receipt['checks']:
        bad=copy.deepcopy(receipt);bad['checks'][check]['verdict']='incomplete'
        with pytest.raises(ValueError):delivery.verify(bad,e,c['account_id'],c['source_revision'])
    bad=copy.deepcopy(receipt);bad['workflow_run_id']+=1
    with pytest.raises(ValueError):delivery.verify(bad,e,c['account_id'],c['source_revision'])


@pytest.mark.parametrize("resource,actions,accepted", [
    ("helm_release", ["create"], True), ("kubernetes_config_map_v1", ["update"], True),
    ("aws_iam_role_policy", ["create"], False), ("aws_autoscaling_group", ["update"], False),
    ("aws_instance", ["update"], False), ("helm_release", ["delete", "create"], False),
])
def test_activation_plan_cannot_apply_unreconciled_foundation_or_iam(resource, actions, accepted):
    config, _ = fixture()
    plan = {"terraform_version": "1.14.6", "resource_changes": [
        {"mode": "managed", "type": resource, "change": {"actions": actions}}],
        "variables": {k: {"value": v} for k, v in {
            "manager_activation_enabled": True, "runtime_iam_policies_enabled": True,
            "groups": {"42": {}}}.items()}}
    if accepted:
        assert runtime.activation_plan(config, plan)["manager_activation_enabled"] is True
    else:
        with pytest.raises(ValueError, match="runner_activation_requires_reconciled_foundation"):
            runtime.activation_plan(config, plan)


@pytest.mark.parametrize("kind", ["aws_launch_template", "aws_autoscaling_group"])
@pytest.mark.parametrize("changed", [None, "wrong_address", "identity", "network", "profile", "capacity", "create", "delete", "unknown_network"])
def test_reviewed_image_update_is_limited_to_existing_empty_group(kind, changed):
    config, _ = fixture()
    if kind == "aws_launch_template":
        before = {"id": config["launch_template_id"], "image_id": "ami-old", "user_data": "old",
                  "iam_instance_profile": [], "network_interfaces": [{"associate_public_ip_address": False}],
                  "latest_version": 1, "default_version": 1}
        after = {**copy.deepcopy(before), "image_id": config["ami_id"], "user_data": "new", "latest_version": None}
        unknown = {"latest_version": True, "block_device_mappings": [{"ebs": [{}]}],
                   "network_interfaces": [{"security_groups": [False]}], "metadata_options": [{}]}
    else:
        before = {"arn": config["asg_arn"], "name": config["asg_name"], "desired_capacity": 0,
                  "launch_template": [{"id": config["launch_template_id"], "name": "job", "version": "1"}]}
        after = copy.deepcopy(before)
        after["launch_template"][0]["version"] = None
        unknown = {"launch_template": [{"version": True}], "tag": [{}, {}, {}],
                   "vpc_zone_identifier": [False, False], "availability_zone_distribution": [{}]}
    resource = {"type": kind, "address": f'{kind}.job["42"]', "change": {
        "actions": ["update"], "before": before, "after": after, "after_unknown": unknown}}
    if changed == "wrong_address": resource["address"] = f'{kind}.job["99"]'
    elif changed == "identity":
        if kind == "aws_launch_template": after["image_id"] = "ami-unreviewed"
        else: after["launch_template"][0]["id"] = "lt-other"
    elif changed == "network": after["vpc_zone_identifier"] = ["subnet-other"]
    elif changed == "profile": after["iam_instance_profile"] = [{"arn": "privileged"}]
    elif changed == "capacity": after["desired_capacity"] = 1
    elif changed in {"create", "delete"}: resource["change"]["actions"] = [changed]
    elif changed == "unknown_network": unknown["network_interfaces"] = True
    assert runtime.reviewed_image_change(config, resource) is (changed is None)


@pytest.mark.parametrize("kind", ["aws_launch_template", "aws_autoscaling_group"])
def test_reviewed_image_update_rejects_another_bound_resource(kind):
    config, _ = fixture()
    before = {"id": "lt-other", "image_id": "ami-old", "user_data": "old"} if kind == "aws_launch_template" else {
        "arn": "arn:other", "name": "other", "desired_capacity": 0,
        "launch_template": [{"id": config["launch_template_id"], "version": "1"}]}
    after = {**before, "image_id": config["ami_id"], "user_data": "new"} if kind == "aws_launch_template" else copy.deepcopy(before)
    assert not runtime.reviewed_image_change(config, {"type": kind, "address": f'{kind}.job["42"]',
        "change": {"actions": ["update"], "before": before, "after": after}})


def connector_plan(cache=False):
    config, _ = fixture()
    before = {"id": "sgr-0123456789abcdef0", "security_group_id": "sg-0123456789abcdef1",
              "referenced_security_group_id": "sg-0123456789abcdef0", "ip_protocol": "tcp",
              "from_port": 22, "to_port": 22, "cidr_ipv4": None, "cidr_ipv6": None, "prefix_list_id": None}
    after = {**before, "referenced_security_group_id": "sg-0123456789abcdef2"}
    resource = {"mode": "managed", "type": "aws_vpc_security_group_ingress_rule",
                "address": 'aws_vpc_security_group_ingress_rule.manager_connector["42"]',
                "change": {"actions": ["update"], "before": before, "after": after, "after_unknown": {}}}
    plan = {"terraform_version": "1.14.6", "resource_changes": [resource],
            "variables": {k: {"value": v} for k, v in {"manager_activation_enabled": True,
                "runtime_iam_policies_enabled": True, "groups": {"42": {}}}.items()},
            "planned_values": {"root_module": {"resources": [
                {"address": "data.aws_security_group.eks_nodes", "values": {
                    "id": "sg-0123456789abcdef2", "vpc_id": config["vpc_id"]}},
                {"address": 'aws_launch_template.job["42"]', "values": {
                    "id": config["launch_template_id"], "network_interfaces": [
                        {"security_groups": [before["security_group_id"]]}]}}]}}}
    plan["planned_values"]["root_module"]["resources"].append({"address": "aws_security_group.cache_endpoint", "values": {"id": "sg-0123456789abcdef3"}})
    if cache:
        resource["address"] = "aws_vpc_security_group_ingress_rule.cache_manager"
        for rule in (before, after):
            rule.update(security_group_id="sg-0123456789abcdef3", from_port=443, to_port=443)
    return config, plan, resource


@pytest.mark.parametrize("cache", [False, True])
def test_existing_manager_rules_can_follow_the_discovered_cluster_node_group(cache):
    config, plan, resource = connector_plan(cache)
    assert runtime.reviewed_connector_change(config, plan, resource)
    assert runtime.activation_plan(config, plan)["manager_activation_enabled"]


@pytest.mark.parametrize("change", ["create", "replace", "other_rule", "other_group", "wrong_node_group",
    "other_vpc", "other_template", "wrong_destination", "open_ipv4", "open_ipv6", "prefix_list", "port", "protocol", "unknown"])
def test_connector_recovery_cannot_open_or_change_unrelated_rules(change):
    config, plan, resource = connector_plan()
    rule = resource["change"]; after = rule["after"]
    nodes, template = [item["values"] for item in plan["planned_values"]["root_module"]["resources"][:2]]
    if change == "create": rule["actions"] = ["create"]
    elif change == "replace": rule["actions"] = ["delete", "create"]
    elif change == "other_rule": after["id"] = "sgr-0123456789abcdef9"
    elif change == "other_group": resource["address"] = 'aws_vpc_security_group_ingress_rule.manager_connector["99"]'
    elif change == "wrong_node_group": after["referenced_security_group_id"] = "sg-0123456789abcdef9"
    elif change == "other_vpc": nodes["vpc_id"] = "vpc-0123456789abcdef9"
    elif change == "other_template": template["id"] = "lt-0123456789abcdef9"
    elif change == "wrong_destination": template["network_interfaces"][0]["security_groups"] = ["sg-0123456789abcdef9"]
    elif change == "open_ipv4": after["cidr_ipv4"] = "0.0.0.0/0"
    elif change == "open_ipv6": after["cidr_ipv6"] = "::/0"
    elif change == "prefix_list": after["prefix_list_id"] = "pl-0123456789abcdef0"
    elif change == "port": after["to_port"] = 65535
    elif change == "protocol": after["ip_protocol"] = "-1"
    elif change == "unknown": rule["after_unknown"] = {"referenced_security_group_id": True}
    assert not runtime.reviewed_connector_change(config, plan, resource)
    with pytest.raises(ValueError, match="runner_activation_requires_reconciled_foundation"):
        runtime.activation_plan(config, plan)


def test_connector_guard_uses_real_refreshed_data_representation():
    config, plan, resource = connector_plan()
    node = plan["planned_values"]["root_module"]["resources"].pop(0)
    node["mode"] = "data"
    plan["prior_state"] = {"values": {"root_module": {"resources": [node]}}}
    assert runtime.reviewed_connector_change(config, plan, resource)
    node["values"]["id"] = "sg-0123456789abcdef9"
    assert not runtime.reviewed_connector_change(config, plan, resource)
