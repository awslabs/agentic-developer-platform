"""Real Python TaskHost + real built SDK/MCP; scripted authenticated service receipts."""
import base64
import dataclasses
import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import boto3
from moto import mock_aws

import pytest

FACTORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(FACTORY / 'agent-worker-image' / 'tests'))
import test_task_host  # noqa: E402 - repository fixture imports need the paths above
from test_task_host import FakeClient, FakeHeartbeat  # noqa: E402 - repository fixture imports need the paths above
from lib.task_host import TaskHost, _canonical_digest  # noqa: E402 - repository fixture imports need the paths above

# Use the real gateway turn state machine. A client that always returns a
# committed turn masks SDK autonomous continuation failures after the first tool.
sys.path.insert(0, str(FACTORY.parents[1] / 'modules/gateway'))
from src.agentauth.task_turns import TaskTurnStore  # noqa: E402 - repository fixture imports need the paths above
from src.tasks.store import _serialize, _deserialize  # noqa: E402 - repository fixture imports need the paths above
from src.tasks.records import task_partition  # noqa: E402 - repository fixture imports need the paths above

assignment_and_bootstrap = test_task_host.assignment_and_bootstrap


@mock_aws
@pytest.mark.parametrize('unknown_model', [False, True])
def test_python_host_real_sdk_broker_artifact_report(tmp_path, monkeypatch, assignment_and_bootstrap, unknown_model):
    assignment, envelope, bootstrap = assignment_and_bootstrap
    assignment = dataclasses.replace(assignment, persona='agent-task-cyber')
    bootstrap['persona'] = 'agent-task-cyber'
    bootstrap['input']['inputs'] = {'url': 'https://example.com'}
    bootstrap['limits']['max_turns'] = 6
    events = []
    ddb = boto3.client('dynamodb', region_name='us-east-1')
    ddb.create_table(TableName='sdk-turns', BillingMode='PAY_PER_REQUEST',
        KeySchema=[{'AttributeName':'event_id','KeyType':'HASH'},{'AttributeName':'arrived_at','KeyType':'RANGE'}],
        AttributeDefinitions=[{'AttributeName':'event_id','AttributeType':'S'},{'AttributeName':'arrived_at','AttributeType':'S'}])
    class Repository:
        table_name = 'sdk-turns'
        _client = ddb
        def read_task(self, task_id):
            row = ddb.get_item(TableName=self.table_name, Key=_serialize({'event_id':task_partition(task_id),'arrived_at':'META'}), ConsistentRead=True)
            return _deserialize(row['Item'])
        def resolve_work(self, dispatch_id, **kwargs):
            assert dispatch_id == 'fixture-dispatch'
        def _get_authority(self, *args):
            return {'limits':bootstrap['limits']}
        def read_commands(self, **kwargs):
            return []
        def _authority_condition_checks(self, **kwargs):
            return []  # Authenticated fake client owns this test's fixed authority.
    repository = Repository()
    turns = TaskTurnStore(repository)
    artifact_id = 'art_' + str(uuid.uuid4())
    report = {'summary': 'URL analysis returned a scripted observation.',
              'findings': [{'statement': 'Scripted backend found no malicious behavior.', 'evidence_refs': [artifact_id]}],
              'uncertainties': ['This is a scripted integration test.'], 'recommendations': [],
              'evidence_refs': [{'ref': artifact_id, 'source': 'artifact', 'artifact_id': artifact_id}]}

    class Client(FakeClient):
        model_bodies = []
        artifact_bodies = []
        turn_requests = []

        def attempt(self, body):
            self.identity = SimpleNamespace(task_id=assignment.task_id, invocation_id=assignment.invocation_id,
                generation=assignment.generation, runtime_attempt_id=body['runtime_attempt_id'], tenant='test')
            task = {'event_id':task_partition(assignment.task_id),'arrived_at':'META','task_id':assignment.task_id,
                'invocation_id':assignment.invocation_id,'generation':assignment.generation,
                'runtime_attempt_id':body['runtime_attempt_id'],'scope':{'tenant':'test','canonical_principal':'test'},
                'persona':'agent-task-cyber','dispatch_id':'fixture-dispatch','state':'running','turn_count':0,
                'version':1,'event_sequence':1,'deadline_at':bootstrap['deadline_at']}
            ddb.put_item(TableName=repository.table_name,Item=_serialize(task))
            return super().attempt(body)

        def turn(self, body):
            self.turn_requests.append(body)
            return turns.commit(identity=self.identity, request_id=body['request_id'],
                expected_transcript_version=body['expected_transcript_version'],
                allow_autonomous=body.get('allow_autonomous',False))

        def model(self, body):
            self.model_bodies.append(body)
            if unknown_model:
                return {'schema_version': '1.0', 'task_id': assignment.task_id,
                        'turn_id': body['turn_id'], 'request_digest': body['request_digest'],
                        'automatic_replay_permitted': False, 'operation_status': 'unknown'}
            sdk = body['sdk_request']
            assert body['request_digest'] == _canonical_digest({**sdk, 'max_tokens': body['max_tokens']})
            assert all(tool['name'].startswith('mcp__cyber__') for tool in sdk['tools'])
            number = len(self.model_bodies)
            if number == 1:
                content = [{'type': 'tool_use', 'id': 'toolu_url', 'name': 'mcp__cyber__url_analysis', 'input': {'url': 'https://example.com'}}]
            elif number == 2:
                assert artifact_id in json.dumps(sdk['messages'])
                content = [{'type': 'tool_use', 'id': 'toolu_report', 'name': 'mcp__cyber__submit_report', 'input': report}]
            else:
                content = [{'type': 'text', 'text': 'Report submitted.'}]
            return {'schema_version': '1.0', 'task_id': assignment.task_id,
                    'turn_id': body['turn_id'], 'request_digest': body['request_digest'],
                    'automatic_replay_permitted': False, 'operation_status': 'confirmed',
                    'handoff': 'confirmed', 'content': content,
                    'stop_reason': 'tool_use' if number < 3 else 'end_turn',
                    'usage': {'input_tokens': 100, 'output_tokens': 30}}

        def tool(self, name, body):
            assert name == "cyber." + body["operation"]
            return self.cyber(body)
        def cyber(self, body):
            events.append('cyber:' + body['operation'])
            base = {'schema_version': '1.0', 'task_id': assignment.task_id,
                    'operation_id': body['operation_id'], 'operation_status': 'confirmed'}
            if body['operation'] == 'cancel_jobs':
                return {**base, 'result': {'status': 'confirmed', 'pending_jobs': []}}
            assert body['operation'] == 'url_analysis'
            return {**base, 'result': {'status': 'completed', 'findings': ['No malicious behavior observed in scripted response.']},
                    'artifact': {'artifact_id': artifact_id, 'content_type': 'application/json',
                                 'content_sha256': hashlib.sha256(b'{}').hexdigest(), 'byte_length': 2}}

        def artifact(self, body):
            self.artifact_bodies.append(body)
            return super().artifact(body)

    sys.path.insert(0, str(FACTORY.parents[1] / 'modules/tools'))
    sys.path.insert(0, str(FACTORY.parents[1] / 'modules/domain-apps/cyber/tools'))
    monkeypatch.setenv('ADP_TASK_REPORT_RENDERERS', '{"agent-task-cyber":"cyber_tools.task_report:render_report"}')
    client = Client(bootstrap, events)
    monkeypatch.setattr('lib.task_host.workload_identity', lambda: {'pod_uid': str(uuid.uuid4()), 'namespace': 'test'})
    executable = FACTORY / 'task-agents/cyber/dist/index.js'
    assert executable.exists(), 'Run npm run build in cyber and investigator packages first'
    host = TaskHost(client=client, work_root=tmp_path / 'work',
                    command_resolver=lambda _: [shutil.which('node'), str(executable), '--embedded'])
    exit_code = host.run(assignment, envelope, heartbeat=FakeHeartbeat(events), acknowledge=lambda: events.append('ack'))
    if unknown_model:
        assert exit_code != 0
        assert len(client.model_bodies) == 1
        assert 'cyber:url_analysis' not in events
        assert events.count('cyber:cancel_jobs') == 1
        assert client.finalize_body['outcome'] == 'failed'
        assert client.finalize_body['error']['code'] == 'model_outcome_unknown', client.finalize_body
        return
    assert exit_code == 0, (events, client.finalize_body, client.settlements)
    assert len(client.model_bodies) == 2
    assert len(turns.list_turns(assignment.task_id)) == 2
    assert all(request['allow_autonomous'] is True for request in client.turn_requests)
    assert all(turn['command_ids'] == [] for turn in turns.list_turns(assignment.task_id))
    assert events.count('cyber:url_analysis') == 1
    assert events.count('cyber:cancel_jobs') == 1
    assert events.index('cyber:cancel_jobs') < events.index('finalize:completed') < events.index('ack')
    assert client.finalize_body['outcome'] == 'completed'
    durable_report = json.loads(base64.b64decode(client.artifact_bodies[-2]['content_base64']))
    html_body = client.artifact_bodies[-1]
    assert html_body['content_type'] == 'text/html'
    assert b'url analysis' in base64.b64decode(html_body['content_base64'])
    assert len(client.finalize_body['result']['artifact_ids']) == 2
    assert len(client.finalize_body['committed_result_refs']) == 2
    assert durable_report == report
    assert durable_report['evidence_refs'][0]['source'] == 'artifact'
    assert durable_report['findings'][0]['evidence_refs'] == [artifact_id]
