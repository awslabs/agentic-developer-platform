"""External lifecycle evidence must reject replacement and incomplete observations."""
import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('pod_survival', Path(__file__).resolve().parents[1] / 'lib/pod_survival.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def observation():
    worker = {'name': 'agent-worker', 'containerID': 'containerd://one', 'restartCount': 0,
              'ready': True, 'state': {'running': {'startedAt': '2026-09-24T12:00:00Z'}}}
    return {'before': {'metadata': {'uid': 'pod-one'}, 'status': {'containerStatuses': [worker]}},
            'after': {'pod_uid': 'pod-one', 'returncode': 0, 'observed_at': '2026-09-24T12:05:00Z',
                      'status': {'containerStatuses': [copy.deepcopy(worker)]}},
            'result': {'pod_uid': 'pod-one'}, 'result_collected_at': '2026-09-24T12:04:00Z',
            'events': {'items': []}}


def test_survives_running_and_collector_exit():
    data = observation()
    assert m.observe_survival(data)['pod_killed'] is False
    data['after']['status']['containerStatuses'][0]['state'] = {
        'terminated': {'finishedAt': '2026-09-24T12:04:30Z', 'exitCode': 1, 'reason': 'Error'}}
    assert m.observe_survival(data)['pod_killed'] is False


@pytest.mark.parametrize('case', ['restart', 'replacement', 'early_exit', 'oom', 'kill_event'])
def test_disruptions_never_prove_survival(case):
    data = observation()
    worker = data['after']['status']['containerStatuses'][0]
    if case == 'restart':
        worker['restartCount'] = 1
    elif case == 'replacement':
        worker['containerID'] = 'containerd://two'
    elif case in {'early_exit', 'oom'}:
        worker['state'] = {'terminated': {'finishedAt': '2026-09-24T12:03:00Z',
                                         'reason': 'OOMKilled' if case == 'oom' else 'Error'}}
    else:
        data['events']['items'] = [{'involvedObject': {'uid': 'pod-one'}, 'reason': 'Killing'}]
    assert m.observe_survival(data)['pod_killed'] is True


@pytest.mark.parametrize('case', ['foreign_pod', 'foreign_result', 'foreign_event', 'early_observation',
                                 'missing_restarts', 'missing_events', 'observation_error', 'absent'])
def test_unknown_or_foreign_observations_are_rejected(case):
    data = observation()
    if case == 'foreign_pod':
        data['after']['pod_uid'] = 'other'
    elif case == 'foreign_result':
        data['result']['pod_uid'] = 'other'
    elif case == 'foreign_event':
        data['events']['items'] = [{'involvedObject': {'uid': 'other'}, 'reason': 'Started'}]
    elif case == 'early_observation':
        data['after']['observed_at'] = '2026-09-24T12:03:00Z'
    elif case == 'missing_restarts':
        del data['after']['status']['containerStatuses'][0]['restartCount']
    elif case == 'missing_events':
        data['events'] = {}
    elif case == 'observation_error':
        data['after']['returncode'] = 1
    else:
        data['after']['absent'] = True
    with pytest.raises(ValueError):
        m.observe_survival(data)
