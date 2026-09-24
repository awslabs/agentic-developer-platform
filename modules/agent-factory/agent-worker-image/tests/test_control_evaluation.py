"""Run-bound SDK fixture execution; no model or cloud calls in this suite."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.control_evaluation import evaluation_request, run_evaluation, verify_handoff


@pytest.fixture
def fixture(tmp_path):
    identity = tmp_path / 'identity'
    identity.mkdir()
    for name, value in {'pod-labels': 'adp.io/w2-nonce="' + 'a' * 16 + '"\nadp.io/w2-fixture="w2-test"', 'pod-uid': 'pod-one'}.items():
        (identity / name).write_text(value)
    request = {'run_nonce': 'a' * 16, 'run_id': 'w2-test', 'source_revision': 'b' * 40, 'bundle_sha256': 'c' * 64}
    env = {'ADP_AGENT_AUTHORITY_ENABLED': 'true', 'W2_FIXTURE_RUN_ID': 'w2-test',
           'ADP_AGENT_CONTROL_ENDPOINT': 'https://fixture.test/dev/internal/v1/agent',
           'SIGV4_PROXY_TARGET': 'https://fixture.test/dev/agent'}
    return identity, request, env


def test_ordinary_task_does_not_enter_fixture_mode():
    assert evaluation_request({}, authenticated=False, env={}) is None


@pytest.mark.parametrize('change', ['unauthenticated', 'ordinary-role', 'nonce', 'run', 'revision', 'bundle', 'model-target'])
def test_fixture_dispatch_refuses_mismatched_identity(fixture, change):
    identity, request, env = fixture
    authenticated = change != 'unauthenticated'
    if change == 'ordinary-role': env['ADP_AGENT_AUTHORITY_ENABLED'] = 'false'
    if change == 'nonce': request['run_nonce'] = 'd' * 16
    if change == 'run': request['run_id'] = 'w2-other'
    if change == 'revision': request['source_revision'] = 'main'
    if change == 'bundle': request['bundle_sha256'] = ''
    if change == 'model-target': env['SIGV4_PROXY_TARGET'] = 'https://ordinary.test/dev/agent'
    with pytest.raises(ValueError):
        evaluation_request({'payload': {'control_evaluation': request}}, authenticated=authenticated, env=env, identity_dir=identity)


def test_missing_projected_nonce_is_not_an_environment_override(fixture):
    identity, request, env = fixture
    (identity / 'pod-labels').unlink()
    env['W2_RUN_NONCE'] = request['run_nonce']
    with pytest.raises(FileNotFoundError):
        evaluation_request({'payload': {'control_evaluation': request}}, authenticated=True, env=env, identity_dir=identity)


def prepare_handoff(path, request, bundle):
    path.mkdir()
    (path / 'source.bundle').write_bytes(bundle)
    request['bundle_sha256'] = hashlib.sha256(bundle).hexdigest()
    (path / 'expected-identity.json').write_text(json.dumps({'expected_identity': {'pod_uid': 'pod-one'}}))
    (path / 'cleanup-ledger.json').write_text(json.dumps({'run_id': request['run_id'], 'run_nonce': request['run_nonce']}))
    (path / 'ready').touch()
    (path / 'collected').touch()


@pytest.mark.parametrize('change', ['bundle', 'pod', 'ledger'])
def test_handoff_is_bound_to_dispatch_and_observed_pod(tmp_path, fixture, change):
    _, request, _ = fixture
    handoff = tmp_path / 'handoff'
    prepare_handoff(handoff, request, b'original')
    if change == 'bundle': (handoff / 'source.bundle').write_bytes(b'changed')
    if change == 'pod': (handoff / 'expected-identity.json').write_text(json.dumps({'pod_uid': 'another-pod'}))
    if change == 'ledger': (handoff / 'cleanup-ledger.json').write_text(json.dumps({'run_id': 'w2-another', 'run_nonce': request['run_nonce']}))
    with pytest.raises(ValueError):
        verify_handoff(request, handoff=handoff, pod_uid='pod-one')


def test_runs_exact_bundle_with_fixture_proxy_and_retains_result(tmp_path, fixture, monkeypatch):
    identity, request, env = fixture
    repo = tmp_path / 'repo'
    repo.mkdir()
    script = repo / 'platform/scripts/operator/wave2/20-collect-pause-evidence.sh'
    script.parent.mkdir(parents=True)
    script.write_text('test "$CLAUDE_CODE_USE_BEDROCK" = 1\ntest "$ANTHROPIC_BEDROCK_BASE_URL" = http://127.0.0.1:9090\nexit 7\n')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    request['source_revision'] = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    bundle = tmp_path / 'fixture.bundle'
    subprocess.run(['git', '-C', str(repo), 'bundle', 'create', str(bundle), 'HEAD'], check=True)
    handoff = tmp_path / 'handoff'
    prepare_handoff(handoff, request, bundle.read_bytes())
    for name, value in env.items(): monkeypatch.setenv(name, value)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'must-not-propagate')
    seen = {}
    proxy = object()
    def start(env, tenant):
        seen.update(env=env, tenant=tenant)
        return proxy
    def stop(actual):
        assert actual is proxy
        seen['stopped'] = True
    rc = run_evaluation(request, {'message_id': 'invocation-one', 'tenant_id': 'tenant-one'},
                        start_proxy=start, stop_proxy=stop, handoff=handoff, identity_dir=identity)
    assert rc == 7
    assert seen['stopped']
    assert 'AWS_ACCESS_KEY_ID' not in seen['env']
    assert seen['tenant'] == 'tenant-one'
    assert json.loads((handoff / 'result.json').read_text()) == {'exit_code': 7, 'pod_uid': 'pod-one', 'run_nonce': request['run_nonce']}
    assert json.loads((handoff / 'bootstrap-ready.json').read_text())['invocation_id'] == 'invocation-one'


@pytest.mark.parametrize('bootstrap_ok,recorded', [(False, False), (True, False), (True, True)])
def test_entrypoint_authenticates_before_eval_and_records_before_ack(fixture, monkeypatch, bootstrap_ok, recorded):
    from unittest.mock import MagicMock
    import entrypoint
    import lib.control_evaluation as evaluation
    import lib.run_identity as identity_module
    identity, request, env = fixture
    envelope = {'message_id': 'invocation-one', 'tenant_id': 'tenant-one', 'persona': 'developer',
                'arrived_at': '2026-09-24T12:00:00Z',
                'source_ref': {'installation_id': 0, 'repo': 'fixture/repo', 'issue': 0},
                'payload': {'control_evaluation': request}}
    for name, value in env.items(): monkeypatch.setenv(name, value)
    monkeypatch.setenv('QUEUE_URL', 'fixture-queue')
    monkeypatch.setattr(entrypoint, '_receive_one_message', lambda *args: (json.dumps(envelope), 'receipt'))
    monkeypatch.setattr(entrypoint, 'BootstrapLogger', MagicMock())
    monkeypatch.setattr(entrypoint.run_report, 'configure', lambda *args: None)
    monkeypatch.setattr(entrypoint.run_report, 'enabled', lambda: False)
    order = []
    def bootstrap(actual):
        order.append('bootstrap')
        if not bootstrap_ok: raise RuntimeError('bootstrap refused')
        return object()
    monkeypatch.setattr(identity_module, 'bootstrap_run_identity', bootstrap)
    real_request = evaluation.evaluation_request
    monkeypatch.setattr(evaluation, 'evaluation_request', lambda actual, **kwargs: real_request(actual, **kwargs, identity_dir=identity))
    monkeypatch.setattr(evaluation, 'run_evaluation', lambda *args, **kwargs: order.append('evaluation') or 0)
    def status(*args, **kwargs):
        order.append('status')
        return recorded
    monkeypatch.setattr(entrypoint, 'update_invocation_status', status)
    monkeypatch.setattr(entrypoint, '_delete_message', lambda *args: order.append('ack'))
    if not bootstrap_ok:
        with pytest.raises(RuntimeError, match='bootstrap refused'):
            entrypoint._main(task_heartbeat=MagicMock())
        assert order == ['bootstrap']
    else:
        assert entrypoint._main(task_heartbeat=MagicMock()) == (0 if recorded else 1)
        assert order == ['bootstrap', 'evaluation', 'status'] + (['ack'] if recorded else [])


def test_entrypoint_registers_and_tears_down_the_production_control_channel(fixture, monkeypatch):
    """Issue #5891 (LF-01): the fixture path must register the SAME control
    channel an ordinary run registers, before the evaluation subprocess starts,
    and tear it down afterward — not skip registration entirely, which is what
    the branch did before this fix (it returned above `_setup_agent_control`).
    """
    from unittest.mock import MagicMock
    import entrypoint
    import lib.control_evaluation as evaluation
    import lib.run_identity as identity_module
    identity, request, env = fixture
    envelope = {'message_id': 'invocation-one', 'tenant_id': 'tenant-one', 'persona': 'developer',
                'arrived_at': '2026-09-24T12:00:00Z',
                'source_ref': {'installation_id': 0, 'repo': 'fixture/repo', 'issue': 0},
                'payload': {'control_evaluation': request}}
    for name, value in env.items(): monkeypatch.setenv(name, value)
    monkeypatch.setenv('QUEUE_URL', 'fixture-queue')
    monkeypatch.setattr(entrypoint, '_receive_one_message', lambda *args: (json.dumps(envelope), 'receipt'))
    monkeypatch.setattr(entrypoint, 'BootstrapLogger', MagicMock())
    monkeypatch.setattr(entrypoint.run_report, 'configure', lambda *args: None)
    monkeypatch.setattr(entrypoint.run_report, 'enabled', lambda: False)
    monkeypatch.setattr(identity_module, 'bootstrap_run_identity', lambda actual: object())
    real_request = evaluation.evaluation_request
    monkeypatch.setattr(evaluation, 'evaluation_request', lambda actual, **kwargs: real_request(actual, **kwargs, identity_dir=identity))
    monkeypatch.setattr(entrypoint, 'update_invocation_status', lambda *args, **kwargs: True)
    monkeypatch.setattr(entrypoint, '_delete_message', lambda *args: None)

    setup_calls = []
    teardown_calls = []
    evaluation_calls = []

    def fake_setup(agent_env, message_id, arrived_at):
        setup_calls.append((dict(agent_env), message_id, arrived_at))
        agent_env['ADP_CONTROL_TOKEN'] = 'fixture-token'
        agent_env['ADP_CONTROL_PORT'] = '8770'
        return True

    def fake_teardown(message_id, arrived_at, was_registered):
        teardown_calls.append((message_id, arrived_at, was_registered))

    def fake_run_evaluation(actual_request, actual_envelope, *, start_proxy, stop_proxy, control_env=None):
        evaluation_calls.append(dict(control_env or {}))
        return 0

    monkeypatch.setattr(entrypoint, '_setup_agent_control', fake_setup)
    monkeypatch.setattr(entrypoint, '_teardown_agent_control', fake_teardown)
    monkeypatch.setattr(evaluation, 'run_evaluation', fake_run_evaluation)

    assert entrypoint._main(task_heartbeat=MagicMock()) == 0

    # Registration happened for THIS run's id/arrived_at, before the evaluation ran.
    assert len(setup_calls) == 1
    assert setup_calls[0][1:] == ('invocation-one', '2026-09-24T12:00:00Z')
    # The token minted by registration reached the evaluation subprocess's env —
    # this is what makes the listener the fixture starts reachable, rather than
    # a registration that happened and was then discarded.
    assert evaluation_calls == [{'ADP_CONTROL_TOKEN': 'fixture-token', 'ADP_CONTROL_PORT': '8770'}]
    # Teardown ran with the registration's own outcome, so a real credential is
    # always revoked and a registration that never happened is never "torn down".
    assert teardown_calls == [('invocation-one', '2026-09-24T12:00:00Z', True)]


def test_entrypoint_tears_down_control_even_when_evaluation_raises(fixture, monkeypatch):
    """Teardown must run on the same path an ordinary run's does: unconditionally,
    even when the evaluation itself raises — a credential must not outlive the
    process it was minted for.
    """
    from unittest.mock import MagicMock
    import entrypoint
    import lib.control_evaluation as evaluation
    import lib.run_identity as identity_module
    identity, request, env = fixture
    envelope = {'message_id': 'invocation-one', 'tenant_id': 'tenant-one', 'persona': 'developer',
                'arrived_at': '2026-09-24T12:00:00Z',
                'source_ref': {'installation_id': 0, 'repo': 'fixture/repo', 'issue': 0},
                'payload': {'control_evaluation': request}}
    for name, value in env.items(): monkeypatch.setenv(name, value)
    monkeypatch.setenv('QUEUE_URL', 'fixture-queue')
    monkeypatch.setattr(entrypoint, '_receive_one_message', lambda *args: (json.dumps(envelope), 'receipt'))
    monkeypatch.setattr(entrypoint, 'BootstrapLogger', MagicMock())
    monkeypatch.setattr(entrypoint.run_report, 'configure', lambda *args: None)
    monkeypatch.setattr(entrypoint.run_report, 'enabled', lambda: False)
    monkeypatch.setattr(identity_module, 'bootstrap_run_identity', lambda actual: object())
    real_request = evaluation.evaluation_request
    monkeypatch.setattr(evaluation, 'evaluation_request', lambda actual, **kwargs: real_request(actual, **kwargs, identity_dir=identity))
    monkeypatch.setattr(entrypoint, 'update_invocation_status', lambda *args, **kwargs: True)
    monkeypatch.setattr(entrypoint, '_delete_message', lambda *args: None)

    teardown_calls = []
    monkeypatch.setattr(entrypoint, '_setup_agent_control', lambda agent_env, message_id, arrived_at: True)
    monkeypatch.setattr(entrypoint, '_teardown_agent_control',
                        lambda message_id, arrived_at, was_registered: teardown_calls.append(was_registered))

    def raising_run_evaluation(*args, **kwargs):
        raise RuntimeError('evaluation subprocess crashed')
    monkeypatch.setattr(evaluation, 'run_evaluation', raising_run_evaluation)

    # entrypoint._main catches the exception (logger.exception) and reports rc=1
    # rather than propagating — asserting only that teardown still ran.
    entrypoint._main(task_heartbeat=MagicMock())
    assert teardown_calls == [True]


def test_unprotected_evaluation_marker_cannot_bypass_installation_guard(monkeypatch):
    from unittest.mock import MagicMock
    import entrypoint
    import lib.run_identity as identity_module
    monkeypatch.setenv('ADP_AGENT_AUTHORITY_ENABLED', 'false')
    monkeypatch.setenv('QUEUE_URL', 'fixture-queue')
    envelope = {'tenant_id': 'tenant', 'persona': 'developer',
                'source_ref': {'installation_id': 0, 'repo': 'fixture/repo', 'issue': 0},
                'payload': {'control_evaluation': {}}}
    monkeypatch.setattr(entrypoint, '_receive_one_message', lambda *args: (json.dumps(envelope), 'receipt'))
    monkeypatch.setattr(entrypoint, 'BootstrapLogger', MagicMock())
    monkeypatch.setattr(entrypoint, '_fail_bootstrap_status', MagicMock())
    ack = MagicMock()
    bootstrap = MagicMock()
    monkeypatch.setattr(entrypoint, '_delete_message', ack)
    monkeypatch.setattr(identity_module, 'bootstrap_run_identity', bootstrap)
    assert entrypoint._main() == 1
    bootstrap.assert_not_called()
    ack.assert_called_once()
