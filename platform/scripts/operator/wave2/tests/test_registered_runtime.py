"""Recorded native SDK interruption cannot stand in for another run's outcome."""
from copy import deepcopy

import pytest

from registered_runtime import verify_runtime, terminal_observation


@pytest.fixture
def capture():
    request = {'mode': 'native-interrupt', 'run_id': 'w2-test', 'source_revision': 'a' * 40}
    envelope = {'message_id': 'invocation-one', 'payload': {'control_evaluation': request}}
    events = [dict(type=kind, at=f'2026-09-24T12:00:0{i}+00:00') for i, kind in enumerate([
        'listener_started', 'sdk_attempt_attached', 'sdk_message', 'native_interrupt_requested',
        'native_interrupt_acknowledged', 'runtime_disposed'])]
    events[0]['generation'] = 1
    events[2]['message_type'] = 'assistant'
    runtime = dict(request, invocation_id='invocation-one', generation=1, timed_out=False,
                   exit_code=1, dropped_events=0, cleanup_errors=[], events=events,
                   native_requested=True, native_acknowledged=True)
    return runtime, envelope


def test_native_failure_is_retained_without_becoming_operator_abort(capture):
    runtime, envelope = capture
    verify_runtime(runtime, envelope)
    assert terminal_observation(runtime, {'invocation_id': 'invocation-one', 'status': 'in_progress'}) is None
    assert terminal_observation(runtime, {'invocation_id': 'invocation-one', 'status': 'failed'})['status'] == 'failed'


@pytest.mark.parametrize('key,value', [('mode', 'sdk'), ('invocation_id', 'other'), ('source_revision', 'b'*40),
    ('run_id', 'another'), ('generation', 0), ('exit_code', True), ('dropped_events', False),
    ('timed_out', True), ('dropped_events', 1), ('cleanup_errors', ['Error']),
    ('native_requested', False), ('native_acknowledged', False)])
def test_refuses_identity_and_incomplete_runtime(capture, key, value):
    runtime, envelope = capture
    runtime[key] = value
    with pytest.raises(ValueError): verify_runtime(runtime, envelope)


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'reordered', 'no-active-turn', 'wrong-generation', 'malformed', 'naive-time'])
def test_refuses_incomplete_causal_trace(capture, defect):
    runtime, envelope = capture
    events = runtime['events']
    if defect == 'missing': events.pop(3)
    elif defect == 'duplicate': events.insert(3, deepcopy(events[3]))
    elif defect == 'reordered': events[3], events[4] = events[4], events[3]
    elif defect == 'no-active-turn': events[2]['message_type'] = 'system'
    elif defect == 'wrong-generation': events[0]['generation'] = 2
    elif defect == 'malformed': events[1] = None
    else: events[1]['at'] = '2026-09-24T12:00:01'
    with pytest.raises(ValueError): verify_runtime(runtime, envelope)


@pytest.mark.parametrize('row', [dict(invocation_id='another', status='failed'),
    dict(invocation_id='invocation-one', status='aborted'), dict(invocation_id='invocation-one', status='complete')])
def test_terminal_readback_rejects_foreign_abort_and_inconsistent_success(capture, row):
    with pytest.raises(ValueError): terminal_observation(capture[0], row)


def test_registered_operator_abort_is_allowed(capture):
    runtime, envelope = capture
    runtime['mode'] = envelope['payload']['control_evaluation']['mode'] = 'registered-control'
    verify_runtime(runtime, envelope)
    assert terminal_observation(runtime, {'invocation_id': 'invocation-one', 'status': 'aborted'})['status'] == 'aborted'


def test_cli_waits_for_same_invocation_and_keeps_evidence_private(capture, tmp_path, monkeypatch):
    import io
    import json
    import stat
    import sys
    from urllib.error import HTTPError
    import registered_runtime as collector
    runtime, envelope = capture
    runtime_path, envelope_path, session_path, output = [tmp_path / name for name in ('runtime', 'envelope', 'session', 'out')]
    for path, value in ((runtime_path, runtime), (envelope_path, envelope), (session_path, {'id_token': 'private-test-token'})):
        path.write_text(json.dumps(value))
    calls = []
    def get(request, timeout):
        calls.append(request)
        assert request.full_url == 'https://fixture.invalid/dev/me/agent-invocations/invocation-one'
        assert 0 < timeout <= 20
        if len(calls) == 1:
            raise HTTPError(request.full_url, 404, 'not visible yet', {}, None)
        status = 'in_progress' if len(calls) == 2 else 'failed'
        return io.StringIO(json.dumps({'invocation_id': 'invocation-one', 'status': status}))
    monkeypatch.setattr(collector, 'urlopen', get)
    monkeypatch.setattr(collector.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(sys, 'argv', ['collector', '--runtime', str(runtime_path), '--envelope', str(envelope_path),
        '--session-file', str(session_path), '--out', str(output), '--gateway-url', 'https://fixture.invalid/dev'])
    assert collector.main() == 0
    assert len(calls) == 3
    assert json.loads(output.read_text())['native_interrupt_status']['status'] == 'failed'
    assert 'private-test-token' not in output.read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
