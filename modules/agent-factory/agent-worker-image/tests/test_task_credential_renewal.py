"""Clock-driven short credential renewal across a six-hour Task; no live calls."""
import copy
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.task_run_client import TaskRunClient, TaskRunClientError, TaskRunClientUnavailable


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setenv('ADP_AGENT_CONTROL_ENDPOINT', 'https://gateway.example/internal/v1/agent')
    clock = [datetime(2026, 9, 25, tzinfo=UTC).timestamp()]
    initial = clock[0]
    body = {'schema_version':'1.0', 'task_id':'task-1', 'invocation_id':'invocation-1', 'envelope_digest':'fixed'}
    client = TaskRunClient(clock=lambda:clock[0])
    calls = []
    settings = {'fail':False, 'cancel':False, 'mutation':None}
    from lib import task_run_client as module
    proof = 'header.' + __import__('base64').urlsafe_b64encode(json.dumps({'kubernetes.io':{'namespace':'workers','pod':{'uid':'pod-1'}}}).encode()).decode().rstrip('=') + '.signature'
    monkeypatch.setattr(module, 'read_workload_token', lambda:proof)
    def post(action, request, **kwargs):
        if action != 'bootstrap':
            if kwargs.get("run_bound"):
                client._renew_for(action, request)
            calls.append((action, copy.deepcopy(request), client._run_credential))
            return {'cancel_requested':settings['cancel'], 'attempt_valid':True}
        calls.append((action, copy.deepcopy(request), None))
        if settings['fail']:
            raise TaskRunClientUnavailable('temporary unavailable')
        value = {'schema_version':'1.0', 'task_id':body['task_id'], 'invocation_id':body['invocation_id'],
                 'generation':1, 'persona':'agent-task-cyber', 'run_credential':'opaque-'+str(len(calls)),
                 'run_credential_expires_at':datetime.fromtimestamp(min(clock[0]+900, initial+21600),UTC).isoformat(),
                 'deadline_at':datetime.fromtimestamp(initial+21600,UTC).isoformat()}
        if settings['mutation']:
            value.update(settings['mutation'])
        return value
    monkeypatch.setattr(client, '_post', post)
    client.bootstrap(body)
    return client, clock, calls, settings, initial


def test_six_hour_run_keeps_short_credentials_and_fixed_bootstrap_binding(runtime):
    client, clock, calls, settings, initial = runtime
    for elapsed in range(0, 21600, 30):
        clock[0] = initial + elapsed
        client.model({'attempt':{}})
    bootstraps = [body for action,body,_ in calls if action == 'bootstrap']
    assert 24 <= len(bootstraps) <= 28
    assert all(body == bootstraps[0] for body in bootstraps)
    assert client._credential_expiry <= initial + 21600
    assert client._credential_expiry - clock[0] <= 900
    clock[0] = initial + 21600
    count = len(bootstraps)
    with pytest.raises(TaskRunClientError, match='no longer admits'):
        client.model({'sdk_request':{}})
    client.control({})
    client.cyber({'operation':'cancel_jobs'})
    client.finalize({'outcome':'failed'})
    client.settlement({})
    assert sum(action == 'bootstrap' for action,_,_ in calls) == count


def test_concurrent_model_and_report_share_one_renewal(runtime):
    client, clock, calls, _, initial = runtime
    clock[0] = initial + 850
    gate = threading.Barrier(8)
    def request(index):
        gate.wait()
        return client.model({'request':index})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(request, range(8)))
    assert sum(action == 'bootstrap' for action,_,_ in calls) == 2
    assert len({credential for action,_,credential in calls if action == 'model'}) == 1


@pytest.mark.parametrize('mutation', [{'task_id':'other'}, {'invocation_id':'other'}, {'generation':2},
    {'persona':'other'}, {'deadline_at':'2026-09-26T00:00:00Z'},
    {'run_credential_expires_at':'2026-09-26T00:00:00Z'}])
def test_renewal_rejects_changed_binding_and_retains_stop_credential(runtime, mutation):
    client, clock, calls, settings, initial = runtime
    previous = client._run_credential
    clock[0] = initial + 850
    settings['mutation'] = mutation
    with pytest.raises(TaskRunClientError, match='binding or expiry'):
        client.model({})
    assert client._run_credential == previous
    assert not any(action == 'model' for action,_,_ in calls)
    client.cyber({'operation':'cancel_jobs'})
    assert calls[-1][2] == previous


def test_renewal_outage_does_not_block_control_cancel_or_settlement(runtime):
    client, clock, calls, settings, initial = runtime
    previous = client._run_credential
    clock[0] = initial + 901
    settings.update(fail=True, cancel=True)
    client.control({})
    assert calls[-1] == ('control', {}, previous)
    count = len(calls)
    with pytest.raises(TaskRunClientError, match='no longer admits'):
        client.model({})
    assert len(calls) == count
    client.cyber({'operation':'cancel_jobs'})
    client.finalize({'outcome':'cancelled'})
    client.settlement({})
    assert not any(action == 'bootstrap' for action,_,_ in calls[count:])
    client.clear_credential()
    assert client._run_credential is None and client._bootstrap_body is None


def test_three_hour_idle_input_does_not_renew_control_but_next_turn_does(runtime):
    client, clock, calls, _, initial = runtime
    clock[0] = initial + 10800
    for _ in range(5):
        client.control({})
    assert sum(action == 'bootstrap' for action,_,_ in calls) == 1
    client.turn({'request_id':'same-committed-turn'})
    assert sum(action == 'bootstrap' for action,_,_ in calls) == 2
    assert calls[-1][0] == 'turn' and calls[-1][2] == client._run_credential


def test_unavailable_renewal_never_replays_normal_work_or_blocks_stop(runtime):
    client, clock, calls, settings, initial = runtime
    clock[0] = initial + 10800
    settings['fail'] = True
    with pytest.raises(TaskRunClientUnavailable):
        client.model({'turn_id':'immutable-turn'})
    assert not any(action == 'model' for action,_,_ in calls)
    count = sum(action == 'bootstrap' for action,_,_ in calls)
    client.control({})
    client.cyber({'operation':'cancel_jobs'})
    client.settlement({})
    assert sum(action == 'bootstrap' for action,_,_ in calls) == count
