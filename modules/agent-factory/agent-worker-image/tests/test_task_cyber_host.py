"""SDK broker admission and process cancellation, without GitHub or provider access."""
import dataclasses
import hashlib
import sys
import threading
import uuid

import pytest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_task_host import FakeClient, FakeHeartbeat, child_script
import test_task_host
from lib.task_host import TaskHost, _canonical_digest
from lib.task_protocol import TaskProtocolError, validate_child_frame

assignment_and_bootstrap = test_task_host.assignment_and_bootstrap


def frame(task_id, kind, **body):
    return dict(protocol_version=1, type=kind, request_id=str(uuid.uuid4()), task_id=task_id, **body)


def test_sdk_preserves_structured_tools_and_canonical_turn(assignment_and_bootstrap):
    assignment, _, bootstrap = assignment_and_bootstrap
    client = FakeClient(bootstrap, [])
    host = TaskHost(client=client)
    sdk = {'messages': [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'tool1', 'name': 'triage', 'input': {'sample': 'x'}}]}], 'tools': [{'name': 'triage', 'input_schema': {'type': 'object'}}]}
    request = frame(assignment.task_id, 'model.request', turn_id=str(uuid.uuid4()), sdk_request=sdk, max_tokens=32)
    validate_child_frame(request, assignment.task_id)
    result = host._model_request(assignment, host._binding(assignment, str(uuid.uuid4())), request, 64)
    assert result['sdk_request'] == sdk
    assert result['request_digest'] == _canonical_digest({**sdk, 'max_tokens': 32})
    assert host._turn_number == 1
    with pytest.raises(TaskProtocolError):
        validate_child_frame({**request, 'messages': []}, assignment.task_id)
    with pytest.raises(TaskProtocolError):
        validate_child_frame({**request, 'sdk_request': {**sdk, 'model': 'untrusted'}}, assignment.task_id)


@pytest.mark.parametrize('persona, cancel', [('agent-task-cyber', False), ('agent-task-cyber', True), ('agent-task-cyber', 'unknown'), ('agent-task-cyber', 'completion_unknown'), ('agent-task-cyber', 'failure_unknown'), ('agent-task-investigator', False)])
def test_broker_is_persona_gated_and_cancel_responsive(tmp_path, monkeypatch, assignment_and_bootstrap, persona, cancel):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    assignment = dataclasses.replace(assignment, persona=persona)
    bootstrap['persona'] = persona
    events = []
    gate = threading.Event()
    class Client(FakeClient):
        def cyber(self, body):
            if body['operation'] == 'cancel_jobs':
                return {'schema_version': '1.0', 'task_id': assignment.task_id, 'operation_id': body['operation_id'],
                        'operation_status': 'unknown' if cancel in ('unknown', 'completion_unknown', 'failure_unknown') else 'confirmed', 'result': {'status': 'confirmed', 'pending_jobs': []}}
            events.append('cyber')
            if cancel is True or cancel == 'unknown':
                self.cancel = True
                gate.wait(5)
            return {'schema_version': '1.0', 'task_id': assignment.task_id,
                    'operation_id': body['operation_id'], 'operation_status': 'confirmed',
                    'result': {'status': 'complete'}, 'artifact': {'artifact_id': 'art_fixture', 'version': 1,
                    'content_sha256': hashlib.sha256(b'{}').hexdigest(), 'content_type': 'application/json', 'byte_length': 2}}
    client = Client(bootstrap, events)
    script = child_script(tmp_path)
    source = script.read_text()
    start = source.index('try:\n    socket.socket()')
    end = source.index("task_id = start['task_id']", start)
    if persona == 'agent-task-cyber':
        source = source[:start] + "assert os.environ['ADP_TASK_NETWORK'] == 'host-mediated-sdk'\nwith socket.socket() as sock: sock.bind(('127.0.0.1', 0))\n" + source[end:]
    idx = source.index("report = {'summary'")
    source = source[:idx] + """
request = base('cyber.request'); request.update(operation='triage', payload={'sample_s3_uri': 's3://authorized/sample'}); send(request)
reply = json.loads(sys.stdin.readline())
if reply['type'] == 'cancel':
    ack = base('cancelled'); ack['command_id'] = reply['command_id']; send(ack); sys.exit(0)
assert reply['type'] == 'cyber.result' and reply['request_id'] == request['request_id']
assert reply['artifact']['artifact_id'] == 'art_fixture'
""" + source[idx:]
    if cancel == "failure_unknown":
        source = source.replace("report = {", "raise RuntimeError('fixture failure')\nreport = {")
    script.write_text(source)
    monkeypatch.setattr('lib.task_host.workload_identity', lambda: {'pod_uid': str(uuid.uuid4()), 'namespace': 'test'})
    monkeypatch.setattr('lib.task_host._CONTROL_POLL_SECONDS', 0.02)
    host = TaskHost(client=client, work_root=tmp_path/'work', command_resolver=lambda _: [sys.executable, str(script)])
    try:
        host.run(assignment, envelope, heartbeat=FakeHeartbeat(events), acknowledge=lambda: events.append('ack'))
    finally:
        gate.set()
    if persona != 'agent-task-cyber':
        assert 'cyber' not in events
        assert client.finalize_body['outcome'] == 'failed'
    else:
        assert events.count('cyber') == 1
        if cancel in ('unknown', 'completion_unknown', 'failure_unknown'):
            assert client.finalize_body is None and client.settlements
            assert 'ack' not in events
            return
        assert client.finalize_body['outcome'] == ('cancelled' if cancel else 'completed')
        assert 'heartbeat.stop' in events and 'ack' in events


@pytest.mark.parametrize('operation', ['shell', 'github', 'pause', 'resume'])
def test_broker_unknown_operations_rejected(operation):
    with pytest.raises(TaskProtocolError):
        validate_child_frame(frame('task', 'cyber.request', operation=operation, payload={}), 'task')


@pytest.mark.parametrize('status', ['pending', 'unknown', 'rejected'])
def test_unconfirmed_broker_result_is_not_replayed(assignment_and_bootstrap, status):
    assignment, _, bootstrap = assignment_and_bootstrap
    client = FakeClient(bootstrap, [])
    requests = []
    def cyber(body):
        requests.append(body)
        return {'schema_version': '1.0', 'task_id': assignment.task_id,
                'operation_id': body['operation_id'], 'operation_status': status, 'error_code': 'broker_unavailable'}
    client.cyber = cyber
    host = TaskHost(client=client)
    request = frame(assignment.task_id, 'cyber.request', operation='enrich', payload={'sha256': 'a'*64})
    response = host._cyber(assignment, host._binding(assignment, str(uuid.uuid4())), request)
    assert response['operation'] == 'enrich'
    assert response['operation_status'] == status
    assert response['result'] == {} and 'artifact' not in response
    assert len(requests) == 1


@pytest.mark.parametrize('mutation', [ {'operation_id': str(uuid.uuid4())}, {'artifact': {}}, {'task_id': 'other'}])
def test_broker_confirmation_requires_bound_durable_receipt(assignment_and_bootstrap, mutation):
    from lib.task_run_client import TaskRunClientError
    assignment, _, bootstrap = assignment_and_bootstrap
    client = FakeClient(bootstrap, [])
    def cyber(body):
        return {'schema_version': '1.0', 'task_id': assignment.task_id,
                'operation_id': body['operation_id'], 'operation_status': 'confirmed',
                'result': {}, 'artifact': {'artifact_id': 'art_fixture', 'byte_length': 2,
                'content_type': 'application/json', 'content_sha256': 'a'*64}, **mutation}
    client.cyber = cyber
    host = TaskHost(client=client)
    with pytest.raises(TaskRunClientError):
        host._cyber(assignment, host._binding(assignment, str(uuid.uuid4())), frame(assignment.task_id, 'cyber.request', operation='enrich', payload={'sha256': 'a'*64}))


def test_only_sdk_model_admission_requests_autonomous_turns(assignment_and_bootstrap):
    assignment, _, bootstrap = assignment_and_bootstrap
    client = FakeClient(bootstrap, [])
    requests = []
    original = client.turn
    def turn(body):
        requests.append(body)
        return original(body)
    client.turn = turn
    host = TaskHost(client=client)
    bound = host._binding(assignment, str(uuid.uuid4()))
    # Input delivery retains the explicit caller-command contract.
    host._turn(assignment, bound, str(uuid.uuid4()))
    assert 'allow_autonomous' not in requests[-1]
    sdk = frame(assignment.task_id, 'model.request', turn_id=str(uuid.uuid4()), max_tokens=32,
                sdk_request={'messages':[{'role':'user','content':'continue tool analysis'}]})
    host._model_request(assignment, bound, sdk, 32)
    assert requests[-1]['allow_autonomous'] is True
    legacy = frame(assignment.task_id, 'model.request', turn_id=str(uuid.uuid4()), max_tokens=32,
                   messages=[{'role':'user','content':'investigator input'}])
    host._model_request(assignment, bound, legacy, 32)
    assert 'allow_autonomous' not in requests[-1]
