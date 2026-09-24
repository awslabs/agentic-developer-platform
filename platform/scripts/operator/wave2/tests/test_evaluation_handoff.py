"""Refuse mismatched source and ownership before touching a worker."""
import hashlib
from copy import deepcopy
from unittest.mock import patch

import pytest

from evaluation_handoff import validate_inputs


@pytest.fixture
def inputs(tmp_path):
    bundle = tmp_path / 'source.bundle'
    bundle.write_bytes(b'captured bundle')
    request = {'run_id': 'w2-test', 'run_nonce': 'abcd1234abcd1234', 'source_revision': 'a' * 40,
               'bundle_sha256': hashlib.sha256(bundle.read_bytes()).hexdigest()}
    envelope = {'payload': {'control_evaluation': request}}
    row = {'kind': 'Pod', 'name': 'worker', 'namespace': 'adp-agents', 'uid': 'owned-uid', 'created_by_this_run': True}
    ledger = {'run_id': request['run_id'], 'run_nonce': request['run_nonce'], 'k8s': [row]}
    identity = {'expected_identity': {'run_id': request['run_id'], 'nonce': request['run_nonce'],
                                    'pod_name': 'worker', 'namespace': 'adp-agents', 'pod_uid': 'owned-uid'}}
    return envelope, ledger, identity, bundle


def test_matching_bundle_and_owned_pod(inputs):
    with patch('evaluation_handoff.subprocess.check_output', return_value='a' * 40 + ' HEAD\n'):
        expected, request = validate_inputs(*inputs)
    assert expected['pod_uid'] == 'owned-uid'
    assert request['source_revision'] == 'a' * 40


@pytest.mark.parametrize('defect', ['nonce', 'pod_uid', 'adopted', 'missing_pod', 'duplicate_pod', 'bundle'])
def test_refuses_before_any_external_command(inputs, defect):
    envelope, ledger, identity, bundle = inputs
    if defect == 'nonce':
        envelope['payload']['control_evaluation']['run_nonce'] = 'different'
    elif defect == 'pod_uid':
        identity['expected_identity']['pod_uid'] = 'replacement'
    elif defect == 'adopted':
        ledger['k8s'][0]['created_by_this_run'] = False
    elif defect == 'missing_pod':
        ledger['k8s'] = []
    elif defect == 'duplicate_pod':
        ledger['k8s'].append(deepcopy(ledger['k8s'][0]))
    else:
        bundle.write_bytes(b'tampered')
    with patch('evaluation_handoff.subprocess.check_output') as command:
        with pytest.raises(ValueError):
            validate_inputs(*inputs)
        command.assert_not_called()


def test_refuses_source_not_advertised_by_bundle(inputs):
    with patch('evaluation_handoff.subprocess.check_output', return_value='b' * 40 + ' HEAD\n'):
        with pytest.raises(ValueError, match='bundle head'):
            validate_inputs(*inputs)


def test_observer_preserves_actual_exit_before_ttl(tmp_path):
    import json
    from evaluation_handoff import observe_termination
    expected = {'pod_name': 'worker', 'pod_uid': 'owned', 'namespace': 'agents'}
    def pod(phase, exit_code=None):
        return json.dumps({'metadata': {'name': 'worker', 'uid': 'owned', 'namespace': 'agents'},
                          'status': {'phase': phase, 'containerStatuses': [] if exit_code is None else
                                     [{'name': 'agent-worker', 'state': {'terminated': {'exitCode': exit_code}}}]}}).encode()
    from unittest.mock import Mock
    command = Mock(side_effect=[pod('Running'), pod('Succeeded', 0)])
    assert observe_termination(command, expected, tmp_path, sleep=lambda _: None) == 0
    records = [json.loads(line) for line in (tmp_path/'pod-termination-observations.jsonl').read_text().splitlines()]
    assert [r['status']['phase'] for r in records] == ['Running', 'Succeeded']
    assert json.loads((tmp_path/'pod-terminal.json').read_text())['status']['containerStatuses'][0]['state']['terminated']['exitCode'] == 0


@pytest.mark.parametrize('raw, error', [
    (b'', RuntimeError),
    (b'{"metadata":{"uid":"replacement","name":"worker","namespace":"agents"}}', ValueError),
    (b'{"metadata":{"uid":"owned","name":"worker","namespace":"agents"},"status":{"phase":"Failed"}}', ValueError),
])
def test_observer_does_not_infer_exit_from_absence_or_phase(tmp_path, raw, error):
    from evaluation_handoff import observe_termination
    with pytest.raises(error):
        observe_termination(lambda *args: raw, {'pod_name':'worker','pod_uid':'owned','namespace':'agents'}, tmp_path)
    assert not (tmp_path/'pod-terminal.json').exists()
