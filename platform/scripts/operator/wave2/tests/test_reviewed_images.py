"""Reviewed images are confined to the disposable gateway approval boundary."""
import copy
import json

import pytest

from conftest import TARGET_ACCOUNT, base_rules, live_deployment
from render_fixture import RenderError, render_gateway

DIGEST = 'sha256:' + 'ab' * 32
PREFIX = f'{TARGET_ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/'
GATEWAY = PREFIX + 'adp-gateway@' + DIGEST
WORKER = PREFIX + 'adp-agent-runtime@' + DIGEST



def queue_rules(*, mismatch=False):
    role = f"arn:aws:iam::{TARGET_ACCOUNT}:role/gateway-service"
    policy = {"Version": "2012-10-17", "Statement": [{
        "Sid": "FixtureGatewaydeadbeefcafe0123", "Effect": "Allow",
        "Principal": {"AWS": role},
        "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:SendMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"],
        "Resource": f"arn:aws:sqs:us-east-1:{TARGET_ACCOUNT}:adp-dev-w2-fixture-fixture-test.fifo",
    }]}
    if mismatch:
        policy["Statement"][0]["Principal"] = {"AWS": "*"}
    return [
        {"tool": "kubectl", "match": ["get", "serviceaccount", "bedrockgateway-sa"],
         "stdout": json.dumps({"metadata": {"annotations": {"eks.amazonaws.com/role-arn": role}}})},
        {"tool": "aws", "match": ["get-queue-attributes"],
         "stdout": json.dumps({"Attributes": {"Policy": json.dumps(policy)}})},
    ]

def render(live, digest):
    return render_gateway(live, run_id='w2-reviewed', nonce='deadbeefcafe0123',
                          name='w2-reviewed', namespace='adp-gateway', image=GATEWAY,
                          queue_url='https://sqs.us-east-1.amazonaws.com/879318057152/fixture.fifo',
                          fixture_worker_digest=digest)[0]


def test_fixture_approval_preserves_shared_source():
    live = live_deployment()
    container = live['spec']['template']['spec']['containers'][0]
    container['env'].append({'name': 'AGENT_WORKER_IMAGE_DIGESTS', 'value': ''})
    before = copy.deepcopy(live)
    fixture = render(live, DIGEST)
    actual = fixture['spec']['template']['spec']['containers'][0]
    assert [e for e in actual['env'] if e['name'] == 'AGENT_WORKER_IMAGE_DIGESTS'] == [
        {'name': 'AGENT_WORKER_IMAGE_DIGESTS', 'value': DIGEST}]
    assert actual['envFrom'] == container['envFrom']
    assert live == before


@pytest.mark.parametrize('digest', ['', 'latest', 'sha256:abc', DIGEST + ',sha256:' + 'cd' * 32])
def test_renderer_rejects_invalid_approval(digest):
    with pytest.raises(RenderError, match='exact sha256'):
        render(live_deployment(), digest)


@pytest.mark.parametrize('args', [
    ['--gateway-image', GATEWAY], ['--worker-image', WORKER],
    ['--gateway-image', GATEWAY, '--worker-image', WORKER.replace(TARGET_ACCOUNT, '111111111111')],
    ['--gateway-image', GATEWAY, '--worker-image', PREFIX + 'adp-agent-runtime:latest'],
])
def test_invalid_image_pair_refused_before_mutation(run_create, args):
    run = run_create(base_rules(), args=args)
    assert run.rc != 0
    assert 'both be exact digest pins' in run.output
    assert not run.created()


def test_rehearsal_renders_reviewed_gateway_and_approval(run_create):
    rules = base_rules() + [{'tool': 'aws', 'match': ['get-queue-url'],
                            'rc': 1, 'stderr': 'AWS.SimpleQueueService.NonExistentQueue'}]
    rules += queue_rules()
    rules += [{'tool': 'kubectl', 'match': ['create', '-f'], 'once': True,
               'stdout': 'object/x (server dry run)\n'} for _ in range(4)]
    run = run_create(rules, args=['--check-only', '--gateway-image', GATEWAY, '--worker-image', WORKER])
    assert run.rc == 0, run.output
    objects = json.loads((run.tmp / 'evidence/manifests/10-gateway.json').read_text())
    deployment = next(item for item in objects['items'] if item['kind'] == 'Deployment')
    container = deployment['spec']['template']['spec']['containers'][0]
    attributes = json.loads((run.tmp / 'evidence/queue-attributes.json').read_text())
    statement = json.loads(attributes['Policy'])['Statement'][0]
    assert statement['Principal'] == {'AWS': f'arn:aws:iam::{TARGET_ACCOUNT}:role/gateway-service'}
    assert statement['Resource'] == f'arn:aws:sqs:us-east-1:{TARGET_ACCOUNT}:adp-dev-w2-fixture-fixture-test.fifo'
    assert all('*' not in action for action in statement['Action'])
    assert container['image'] == GATEWAY
    assert {'name': 'AGENT_WORKER_IMAGE_DIGESTS', 'value': DIGEST} in container['env']
    assert not any('create-queue' in call for call in run.calls)
    assert all('--dry-run=server' in call for call in run.calls if 'kubectl create' in call)


def test_protected_composition_is_shared_and_preserves_live_template():
    from conftest import live_worker_template, WORKER_CONTROL_ENDPOINT
    from render_fixture import render_worker_job
    live = live_worker_template()
    spec = live["spec"]
    spec["serviceAccountName"] = "agent-scaledjob-sa"
    spec["volumes"] = [{"name": "unrelated", "emptyDir": {}}]
    container = spec["containers"][0]
    container["volumeMounts"] = [{"name": "unrelated", "mountPath": "/existing"}]
    container["env"] = [e for e in container["env"] if not any(
        word in e["name"] for word in ("AUTHORITY", "WORKLOAD", "ENVELOPE"))]
    before = copy.deepcopy(live)
    job, report = render_worker_job(live, run_id="w2-reviewed", nonce="deadbeefcafe0123",
        name="w2-reviewed-worker", namespace="adp-agents", image=WORKER,
        control_endpoint=WORKER_CONTROL_ENDPOINT, queue_url="https://sqs.us-east-1.amazonaws.com/879318057152/fixture.fifo",
        approved_digests=[DIGEST], compose_protected=True)
    actual = job["spec"]["template"]["spec"]
    assert actual["serviceAccountName"] == "agent-authority-worker-sa"
    assert {"name": "unrelated", "emptyDir": {}} in actual["volumes"]
    assert {"name": "unrelated", "mountPath": "/existing"} in actual["containers"][0]["volumeMounts"]
    assert len(report["protected_composition_sha256"]) == 64
    assert live == before


@pytest.mark.parametrize('mismatch', [None, 'image', 'approval', 'queue'])
def test_reviewed_worker_checks_live_gateway_before_creation(run_create, tmp_path, mismatch):
    from test_create_fixture import _worker_args, _worker_rules, GW_NAME
    live = live_deployment()
    container = live['spec']['template']['spec']['containers'][0]
    container['image'] = GATEWAY if mismatch != 'image' else GATEWAY.replace('ab', 'cd')
    container['env'].append({'name': 'AGENT_WORKER_IMAGE_DIGESTS',
                             'value': DIGEST if mismatch != 'approval' else ''})
    rules = _worker_rules(digest=DIGEST) + queue_rules(mismatch=mismatch == 'queue')
    rules.append({'tool': 'kubectl', 'match': ['get', 'deployment', GW_NAME, '-o', 'json'],
                  'stdout': json.dumps(live)})
    run = run_create(rules, args=_worker_args(tmp_path) + [
        '--gateway-image', GATEWAY, '--worker-image', WORKER])
    if mismatch:
        assert run.rc != 0
        assert ('fixture queue policy does not' if mismatch == 'queue' else 'fixture gateway does not') in run.output
        assert not run.created('20-worker')
    else:
        assert run.rc == 0, run.output
        assert run.created('20-worker')
        objects = json.loads((run.tmp / 'evidence/manifests/20-worker.json').read_text())
        worker = objects['items'][0]['spec']['template']['spec']['containers'][0]
        assert worker['image'] == WORKER


def test_task_api_configuration_stays_inside_fixture():
    live = live_deployment()
    container = live['spec']['template']['spec']['containers'][0]
    container['env'].extend([
        {'name': 'ADP_TASK_API_QUEUE_URL', 'value': 'https://example/ordinary.fifo'},
        {'name': 'ADP_TASK_WORKER_IMAGE_DIGESTS', 'value': 'sha256:' + 'cd' * 32},
        {'name': 'ADP_TASK_WORKER_SERVICE_ACCOUNT', 'value': 'ordinary-worker'},
    ])
    before = copy.deepcopy(live)
    fixture = render(live, DIGEST)
    env = {e['name']: e.get('value') for e in fixture['spec']['template']['spec']['containers'][0]['env']}
    assert env['ADP_TASK_API_QUEUE_URL'] == env['ADP_RUN_TASK_QUEUE_URL']
    assert env['ADP_TASK_API_WORKER_ENABLED'] == 'true'
    assert env['ADP_TASK_WORKER_IMAGE_DIGESTS'] == env['AGENT_WORKER_IMAGE_DIGESTS'] == DIGEST
    assert env['ADP_TASK_WORKER_SERVICE_ACCOUNT'] == 'agent-authority-worker-sa'
    assert env['ADP_TASK_WORKER_NAMESPACE'] == 'adp-agents'
    assert live == before
