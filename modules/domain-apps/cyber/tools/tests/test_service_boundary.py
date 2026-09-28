"""Domain service boundaries, with no gateway imports or live AWS requests."""
import base64
import hashlib
import io
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
import pytest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError
from fastapi import HTTPException
from moto import mock_aws

from adp_tools.authority import TaskAuthorityClient, require_worker
from adp_tools.contracts import Authorization
from adp_tools.storage import OperationRepository, serialize, task_partition
from cyber_tools import handler

ROLE = 'arn:aws:iam::123456789012:role/approved-worker'
CALLER = 'arn:aws:sts::123456789012:assumed-role/approved-worker/pod-session'
ENDPOINT = 'https://authority.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent/task'
PROOFS = {'X-Adp-Run-Credential': 'opaque-run', 'X-Adp-Workload-Token': 'projected-proof'}


def identity():
    return {'task_id': 'tsk_' + str(uuid.uuid4()), 'invocation_id': str(uuid.uuid4()),
            'generation': 1, 'runtime_attempt_id': str(uuid.uuid4()),
            'tenant': 'tenant-a', 'canonical_principal': 'principal-a'}


def authorization(ident=None, version=1):
    ident = ident or identity()
    return Authorization.model_validate({'schema_version': '1.0', 'identity': ident,
        'task': {'task_id': ident['task_id'], 'scope': {k: ident[k] for k in ('tenant', 'canonical_principal')},
                 'persona': 'agent-task-cyber', 'generation': ident['generation'],
                 'runtime_attempt_id': ident['runtime_attempt_id'], 'state': 'running', 'version': version,
                 'deadline_at': (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                 'input_payload': {'inputs': {'url': 'https://example.com'}}}})


def attempt(verified):
    ident = verified.identity.model_dump()
    return {'run': {k: ident[k] for k in ('task_id', 'invocation_id', 'generation')},
            'runtime_attempt_id': ident['runtime_attempt_id']}


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.trust_env = True
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.response
        class Response:
            status_code = 200
            raw = io.BytesIO()
            def __enter__(self):
                self.raw = SimpleNamespace(read=lambda *a, **kw: json.dumps(value).encode())
                return self
            def __exit__(self, *args):
                pass
        return Response()

    def close(self):
        self.closed = True


@pytest.mark.parametrize('arn', ['', ROLE, CALLER.replace('approved-worker', 'other-worker'),
                                  CALLER.replace('123456789012', '999999999999')])
def test_iam_context_required_and_forged_identity_headers_are_inert(arn):
    event = {'requestContext': {'identity': {'userArn': arn}},
             'headers': {'X-Adp-Worker-Role': ROLE, 'X-Amzn-User-Arn': CALLER, **PROOFS}}
    with pytest.raises(HTTPException) as error:
        require_worker(event, {ROLE})
    assert error.value.status_code == 403
    require_worker({'requestContext': {'identity': {'userArn': CALLER}}}, {ROLE})


def test_authority_forwards_only_signed_proofs_and_binds_attempt():
    verified = authorization()
    session = Session(verified.model_dump())
    client = TaskAuthorityClient(ENDPOINT, {**PROOFS, 'Authorization': 'forged', 'X-Adp-Tenant': 'other'},
                                 region='us-east-1', session=session, credentials=Credentials('test', 'secret', 'token'))
    assert client.authorize(attempt=attempt(verified), tool='cyber.url_analysis') == verified
    url, call = session.calls[0]
    assert url == ENDPOINT + '/tool-authorize'
    assert json.loads(call['data']) == {'schema_version': '1.0', 'attempt': attempt(verified),
                                       'tool': 'cyber.url_analysis', 'cleanup': False}
    lower = {k.lower(): v for k, v in call['headers'].items()}
    assert lower['x-adp-run-credential'] == 'opaque-run'
    assert lower['x-adp-workload-token'] == 'projected-proof'
    assert lower['authorization'].startswith('AWS4-HMAC-SHA256 ')
    assert 'x-adp-tenant' not in lower
    assert not call['allow_redirects'] and call['stream'] and not session.trust_env
    client.close()
    assert session.closed


@pytest.mark.parametrize('mutation', ['task_id', 'invocation_id', 'runtime_attempt_id', 'generation', 'scope'])
def test_authority_response_rejects_cross_attempt_or_scope(mutation):
    verified = authorization()
    response = verified.model_dump()
    if mutation == 'scope':
        response['task']['scope']['tenant'] = 'other'
    elif mutation == 'generation':
        response['identity'][mutation] += 1
    else:
        response['identity'][mutation] = ('tsk_' if mutation == 'task_id' else '') + str(uuid.uuid4())
    client = TaskAuthorityClient(ENDPOINT, PROOFS, region='us-east-1',
                                 session=Session(response), credentials=Credentials('test', 'secret'))
    with pytest.raises(HTTPException) as error:
        client.authorize(attempt=attempt(verified), tool='cyber.url_analysis')
    assert error.value.status_code == 503


def make_table(ddb):
    ddb.create_table(TableName='cyber-operations', BillingMode='PAY_PER_REQUEST',
                    KeySchema=[{'AttributeName': 'event_id', 'KeyType': 'HASH'}, {'AttributeName': 'arrived_at', 'KeyType': 'RANGE'}],
                    AttributeDefinitions=[{'AttributeName': 'event_id', 'AttributeType': 'S'}, {'AttributeName': 'arrived_at', 'AttributeType': 'S'}])


@mock_aws
def test_domain_repository_monotonic_authority_and_preserved_fences():
    ddb = boto3.client('dynamodb', region_name='us-east-1')
    make_table(ddb)
    current = authorization(version=2)
    repo = OperationRepository(ddb, 'cyber-operations', lambda: current)
    repo.read_task(current.identity.task_id)
    key = serialize({'event_id': task_partition(current.identity.task_id), 'arrived_at': 'META'})
    ddb.update_item(TableName='cyber-operations', Key=key,
                    UpdateExpression='SET cyber_closed_attempt=:closed, cyber_operation_count=:count',
                    ExpressionAttributeValues=serialize({':closed': current.identity.runtime_attempt_id, ':count': 9}))
    old_attempt = current.identity.runtime_attempt_id
    current = authorization({**current.identity.model_dump(), 'runtime_attempt_id': str(uuid.uuid4()), 'generation': 2}, version=3)
    row = repo.read_task(current.identity.task_id)
    assert row['cyber_closed_attempt'] == old_attempt and row['cyber_operation_count'] == 9
    metadata = repo._get(task_partition(current.identity.task_id), 'META')
    assert 'input_payload' not in metadata and metadata['authority_version'] == 3
    for version in (2, 3):
        current = authorization({**current.identity.model_dump(), 'runtime_attempt_id': old_attempt, 'generation': 1}, version=version)
        with pytest.raises((ClientError, HTTPException)):
            repo.read_task(current.identity.task_id)
        after = repo._get(task_partition(current.identity.task_id), 'META')
        assert after == metadata


@mock_aws
def test_lambda_actual_operations_artifact_dedup_and_cleanup_when_disabled(monkeypatch):
    ddb = boto3.client('dynamodb', region_name='us-east-1')
    make_table(ddb)
    verified = authorization()
    backend_calls, artifact_calls, authorizations, closed = [], [], [], []

    class Authority:
        def __init__(self, endpoint, headers, **kwargs):
            assert endpoint == ENDPOINT and headers == PROOFS
        def authorize(self, **kwargs):
            assert kwargs['attempt'] == attempt(verified)
            authorizations.append(kwargs)
            return verified
        def put_run_artifact(self, **kwargs):
            artifact_calls.append(kwargs)
            assert kwargs['attempt'] == verified.identity
            assert kwargs['digest'] == hashlib.sha256(kwargs['content']).hexdigest()
            return SimpleNamespace(artifact_id='art_' + str(uuid.uuid4()), content_type='application/json', content_sha256=kwargs['digest'])
        def close(self):
            closed.append(True)

    class Backend:
        def _client(self, name):
            assert name == 'dynamodb'
            return ddb
        def url_analysis(self, url, deadline):
            backend_calls.append(url)
            return {'status': 'completed', 'findings': ['Scripted URL evidence'], 'score': 0.875}

    monkeypatch.setattr(handler, 'TaskAuthorityClient', Authority)
    monkeypatch.setattr(handler, 'CyberBackends', Backend)
    monkeypatch.setenv('CYBER_TOOLS_WORKER_ROLES', ROLE)
    monkeypatch.setenv('CYBER_TOOLS_TABLE', 'cyber-operations')
    monkeypatch.setenv('ADP_TASK_AUTHORITY_ENDPOINT', ENDPOINT)
    monkeypatch.setenv('ADP_TASK_CYBER_ENABLED', 'true')
    body = {'schema_version': '1.0', 'attempt': attempt(verified), 'operation_id': str(uuid.uuid4()),
            'operation': 'url_analysis', 'payload': {'url': 'https://example.com'}}
    event = {'httpMethod': 'POST', 'resource': '/tools/cyber', 'requestContext': {'identity': {'userArn': CALLER}},
             'headers': PROOFS, 'body': json.dumps(body)}
    response = handler.lambda_handler(event, None)
    assert response['statusCode'] == 200, response
    receipt = json.loads(response['body'])
    assert receipt['task_id'] == verified.identity.task_id and receipt['operation_id'] == body['operation_id']
    assert receipt['operation_status'] == 'confirmed' and receipt['artifact']['content_type'] == 'application/json'
    assert json.loads(artifact_calls[0]['content'])['result']['findings'] == ['Scripted URL evidence']
    assert handler.lambda_handler(event, None)['body'] == response['body']
    assert len(backend_calls) == len(artifact_calls) == 1
    monkeypatch.setenv('ADP_TASK_CYBER_ENABLED', 'false')
    assert handler.lambda_handler(event, None)['statusCode'] == 503
    body.update(operation='cancel_jobs', operation_id=str(uuid.uuid4()), payload={})
    event['body'] = base64.b64encode(json.dumps(body).encode()).decode()
    event['isBase64Encoded'] = True
    cleanup = handler.lambda_handler(event, None)
    assert cleanup['statusCode'] == 200, cleanup
    assert json.loads(cleanup['body'])['result'] == {'status': 'confirmed', 'pending_jobs': []}
    assert authorizations[-1]['cleanup'] and authorizations[-1]['tool'] == 'cyber.cancel_jobs'
    assert len(closed) == 3
    monkeypatch.setenv('ADP_TASK_CYBER_ENABLED', 'true')
    event['body'] = json.dumps({**body, 'operation': 'url_analysis', 'payload': {'url': 'https://example.com'}})
    event['isBase64Encoded'] = False
    assert handler.lambda_handler(event, None)['statusCode'] == 409
    assert len(backend_calls) == 1
    assert not any(name.startswith(('src.agentauth', 'modules.gateway')) for name in sys.modules)


@pytest.mark.parametrize('mutation', [None, 'other_task', 'version', 'expiry', 'wrong_digest', 'input_digest'])
def test_artifact_receipt_is_bound_to_task_and_exact_content(monkeypatch, mutation):
    verified = authorization()
    content = b'{"finding":"scripted"}'
    digest = hashlib.sha256(content).hexdigest()
    task_id = 'tsk_' + str(uuid.uuid4()) if mutation == 'other_task' else verified.identity.task_id
    expected_id = 'art_' + str(uuid.UUID(bytes=hashlib.sha256(
        f'{task_id}:application/json:{digest}'.encode()).digest()[:16], version=4))
    receipt = {'schema_version': '1.0', 'artifact_id': expected_id, 'version': 1,
               'content_type': 'application/json', 'content_sha256': digest, 'expires_at': None}
    if mutation == 'version':
        receipt['version'] = True
    if mutation == 'expiry':
        receipt['expires_at'] = '2030-01-01T00:00:00Z'
    if mutation == 'wrong_digest':
        receipt['content_sha256'] = 'a' * 64
    client = TaskAuthorityClient(ENDPOINT, PROOFS, region='us-east-1', session=Session({}))
    calls = []
    def post(action, body, **kwargs):
        calls.append((action, body, kwargs))
        return receipt
    monkeypatch.setattr(client, 'post', post)
    arguments = dict(attempt=verified.identity, content=content, content_type='application/json',
                     digest='b' * 64 if mutation == 'input_digest' else digest)
    if mutation:
        with pytest.raises(HTTPException):
            client.put_run_artifact(**arguments)
        if mutation == 'input_digest':
            assert not calls
    else:
        result = client.put_run_artifact(**arguments)
        assert result.artifact_id == expected_id
        action, body, options = calls[0]
        assert action == 'artifact' and options['status'] == 201
        assert body['run'] == attempt(verified)['run'] and 'attempt' not in body
        assert base64.b64decode(body['content_base64']) == content


@pytest.mark.parametrize('proofs', [{}, {'X-Adp-Run-Credential': 'run'}, {'X-Adp-Run-Credential': 'run', 'X-Adp-Workload-Token': ''}])
def test_missing_proofs_are_rejected_before_outbound_transport(proofs):
    session = Session({})
    with pytest.raises(HTTPException) as error:
        TaskAuthorityClient(ENDPOINT, proofs, region='us-east-1', session=session)
    assert error.value.status_code == 403 and not session.calls


def test_lambda_rejects_forged_transport_headers_before_authority(monkeypatch):
    monkeypatch.setenv('CYBER_TOOLS_WORKER_ROLES', ROLE)
    def forbidden(*args, **kwargs):
        pytest.fail('Untrusted caller reached Task authority')
    monkeypatch.setattr(handler, 'TaskAuthorityClient', forbidden)
    response = handler.lambda_handler({'httpMethod': 'POST', 'resource': '/tools/cyber',
        'headers': {'X-Amzn-User-Arn': CALLER, 'X-Adp-Worker-Role': ROLE, **PROOFS},
        'requestContext': {'identity': {'userArn': 'arn:aws:iam::123456789012:user/forged'}},
        'body': '{}'}, None)
    assert response['statusCode'] == 403
    assert 'opaque-run' not in response['body'] and 'projected-proof' not in response['body']
