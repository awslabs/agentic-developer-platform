"""The live runner is exercised with owner-API and host receipts, never AWS resources."""

import importlib.util
import json
import subprocess
from pathlib import Path
import urllib.error

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SPEC = importlib.util.spec_from_file_location('verify_owner_paths', SCRIPTS / 'verify-owner-paths.py')
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class Clock:
    def __init__(self):
        self.now = 0

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


class Gateway:
    def __init__(self, *, binding='active'):
        self.state = 'running'
        self.generation = 1
        self.calls = []
        self.binding = binding

    def __call__(self, method, path, body=None, headers=None):
        self.calls.append((method, path))
        if path == '/cloud-desktops/policy':
            # An owner who already has a box is refused *new* admission; the runner must not gate on it.
            return 200, {'admission_allowed': False, 'reason': 'A desktop is already reserved for this owner'}
        if path == '/cloud-desktops':
            return 200, {'items': [{'desktop_id': 'box-1', 'status': 'running'}]}
        if path == '/cloud-desktops/box-1' and method == 'GET':
            return 200, {'status': self.state, 'observed_state': self.state,
                         'machine_generation': self.generation, 'host_binding': self.binding}
        if path.rsplit('/', 1)[-1] in ('stop', 'replace', 'start') and method == 'POST':
            action = path.rsplit('/', 1)[-1]
            assert headers and headers['Idempotency-Key']
            # A Replace with a JSON body is a snapshot restore and needs snapshot_id (#8670); plain actions send none.
            assert body is None, (action, body)
            # Live behaviour (dev 2026-10-23): a Replace of a stopped box yields a *stopped* machine.
            self.state = 'running' if action == 'start' else 'stopped'
            if action == 'replace':
                self.generation += 1
            return 202, {'operation_id': action}
        if '/operations/' in path:
            return 200, {'status': 'succeeded'}
        if path == '/crew/target':
            return 200, {'health': 'ready', 'lease': {'target_kind': 'ec2-instance',
                         'target_ref': 'box-1', 'generation': self.generation}}
        if path == '/crew/handoff-ticket':
            return 200, {'handoff_url': 'https://crew.example.test/crew/handoff?ticket=secret'}
        raise AssertionError((method, path))


def receipt(**changes):
    value = {'revision': 'staged-revision', 'link': 'staged-revision', 'quarantine': {'version': 1, 'servers': {}},
             'token_ttl_before': -31, 'token_ttl_after': 29, 'adapter_failures': 0,
             'model': {'status': 200, 'latency_ms': 22},
             'tools': {'status': 200, 'tool_count': 12, 'latency_ms': 8}}
    value.update(changes)
    return value


def execute(monkeypatch, *, no_replace=False, report=None, gateway=None):
    clock = Clock()
    gateway = gateway or Gateway()
    handoffs = []
    def handoff(http):
        handoffs.append(clock.now)
        return True
    monkeypatch.setattr(RUNNER, 'handoff', handoff)
    monkeypatch.setattr(RUNNER, 'host_report', lambda *args: (report or receipt(), 'i-123'))
    monkeypatch.setattr(RUNNER, 'generation_instances', lambda aws, box_id, gen, deployment, region: [
        {'InstanceId': 'i-old', 'State': {'Name': 'running' if gateway.generation == gen else 'terminated'}}])
    status, evidence = RUNNER.run(gateway, lambda *args: None, box_id=None, deployment_id='dev-deployment',
                                  region='us-east-1', expected_revision='staged-revision', no_replace=no_replace,
                                  sleep=clock.sleep, clock=clock.monotonic)
    return status, evidence, gateway, handoffs


def test_replace_owner_paths_and_safe_evidence(monkeypatch):
    status, evidence, gateway, handoffs = execute(monkeypatch)
    assert status == 0
    assert evidence['generation'] == 2
    assert handoffs == [20]
    assert set(evidence['checks']) == {'handoff', 'target', 'model_after_idle', 'tools', 'quarantine', 'staged_bundle', 'previous_instance_retired'}
    assert [('POST', f'/cloud-desktops/box-1/{action}') for action in ('stop', 'replace', 'start')] == [
        call for call in gateway.calls if call[0] == 'POST']
    assert 'secret' not in json.dumps(evidence)


def test_no_replace_restarts_same_generation(monkeypatch):
    status, evidence, gateway, _ = execute(monkeypatch, no_replace=True)
    assert status == 0
    assert evidence['generation'] == 1
    assert 'staged_bundle' not in evidence['checks']
    assert ('POST', '/cloud-desktops/box-1/start') in gateway.calls
    assert ('POST', '/cloud-desktops/box-1/replace') not in gateway.calls


@pytest.mark.parametrize('failed,report', [
    ('model_after_idle', receipt(model={'status': 503})),
    ('tools', receipt(tools={'status': 503, 'tool_count': 0})),
    ('quarantine', receipt(quarantine={'version': 1, 'servers': {'adp-tools': {'fails': 3, 'last_status': 'error', 'crossed_at': 1792796404.9}}})),
    ('staged_bundle', receipt(revision='old-revision')),
])
def test_failed_path_names_check_without_secret(monkeypatch, failed, report):
    status, evidence, _, _ = execute(monkeypatch, report=report)
    assert status == 1
    assert evidence['checks'][failed]['status'] == 'failed'
    assert 'secret' not in json.dumps(evidence)
    if failed == 'quarantine':  # #8835: the evidence names the server and Kiro's verdict
        assert evidence['quarantine']['adp-tools']['crossed_at'] == 1792796404.9


def test_missing_handoff_fails_and_names_check(monkeypatch):
    monkeypatch.setattr(RUNNER, 'handoff', lambda http: False)
    gateway = Gateway()
    clock = Clock()
    monkeypatch.setattr(RUNNER, 'generation_instances', lambda *args: [
        {'InstanceId': 'i-old', 'State': {'Name': 'terminated'}}])
    status, evidence = RUNNER.run(gateway, lambda *args: None, box_id=None, deployment_id='dev',
                                  region='us-east-1', expected_revision='staged-revision',
                                  sleep=clock.sleep, clock=clock.monotonic)
    assert status == 1
    assert evidence['checks']['handoff']['status'] == 'failed'


def test_handoff_uses_ticket_origin_without_following_redirect(monkeypatch):
    urls = []
    class Opener:
        def open(self, request, timeout):
            urls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 302, 'Found',
                                         {'Set-Cookie': 'adp_crew=hidden; Secure; HttpOnly'}, None)
    monkeypatch.setattr(RUNNER.urllib.request, 'build_opener', lambda handler: Opener())
    assert RUNNER.handoff(Gateway()) is True
    assert urls == ['https://crew.example.test/crew/handoff?ticket=secret']
    with pytest.raises(ValueError, match='invalid handoff origin'):
        RUNNER.handoff(lambda *args: (200, {'handoff_url': 'http://crew.example.test/crew/handoff'}))


def test_stage_wires_owner_acceptance_without_releasing_box():
    stage = (SCRIPTS / 'deploy-dev-box.sh').read_text()
    assert 'verify-owner-paths.py' in stage
    assert '"$VERIFY_LIVE" = true' in stage
    assert '--evidence "$EVIDENCE"' in stage


def test_enrollment_seals_challenge_without_plaintext_on_disk(tmp_path):
    import base64
    import hashlib
    private = tmp_path / 'synthetic-key.pem'
    subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072',
                    '-out', str(private)], check=True, capture_output=True)
    public = subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout'],
                            check=True, capture_output=True).stdout
    ciphertext = []
    def http(method, path, body=None):
        if path.endswith('host-enrollment-key'):
            return 200, {'public_key_b64': base64.b64encode(public).decode(),
                         'public_key_digest': hashlib.sha256(public).hexdigest(), 'generation': 2}
        if path.endswith('host-enrollment'):
            assert body == {'public_key_digest': hashlib.sha256(public).hexdigest()}
            return 201, {'challenge': 'c' * 32, 'device_id': 'd' * 36}
        assert path.endswith('/sealed')
        ciphertext.append(base64.b64decode(body['ciphertext_b64']))
        return 202, {}
    RUNNER.enroll(http, 'box-1', 2)
    decrypted = subprocess.run(['openssl', 'pkeyutl', '-decrypt', '-inkey', str(private),
                                '-pkeyopt', 'rsa_padding_mode:oaep', '-pkeyopt', 'rsa_oaep_md:sha256',
                                '-pkeyopt', 'rsa_mgf1_md:sha256'], input=ciphertext[0],
                               capture_output=True, check=True)
    assert decrypted.stdout == b'c' * 32


def test_failed_check_names_the_refused_check_not_only_the_exception_class(monkeypatch):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location('owner_paths', pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'verify-owner-paths.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert module.failure_reason(ValueError('expected one owned box')) == 'expected one owned box'
    assert module.failure_reason(KeyError('operation_id')) == 'KeyError'
    assert module.failure_reason(ValueError('x' * 81)) == 'ValueError'


def test_replace_retries_while_the_stopped_box_is_still_verifying(monkeypatch):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location('owner_paths', pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'verify-owner-paths.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, 'sleep', lambda s: None)
    attempts = []
    def http(method, path, body=None, headers=None):
        if path.endswith('/replace') and method == 'POST':
            attempts.append(path)
            if len(attempts) < 3:
                raise module._base.ApiError(409, {'detail': 'Stop and verify the owned workspace before replacement'})
            return 202, {'operation_id': 'op'}
        if '/operations/' in path:
            return 200, {'status': 'succeeded'}
        raise AssertionError((method, path))
    module.operation(http, 'box-1', 'replace', lambda predicate, timeout=600, interval=5: predicate())
    assert len(attempts) == 3
    assert module.failure_reason(module._base.ApiError(409, {})) == 'ApiError 409'


def test_enrolment_waits_for_the_new_generation_host_key(monkeypatch):
    import importlib.util, pathlib, base64
    spec = importlib.util.spec_from_file_location('owner_paths', pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'verify-owner-paths.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, 'sleep', lambda s: None)
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: type('R', (), {'returncode': 0, 'stdout': b'x' * 384})())
    monkeypatch.setattr(module.os, 'memfd_create', lambda *a, **k: 0)
    monkeypatch.setattr(module.os, 'write', lambda *a, **k: None)
    monkeypatch.setattr(module.os, 'close', lambda *a, **k: None)
    pem = b'-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----\n'
    import hashlib
    key = {'generation': 2, 'public_key_b64': base64.b64encode(pem).decode(), 'public_key_digest': hashlib.sha256(pem).hexdigest()}
    reads = []
    def http(method, path, body=None, headers=None):
        if path.endswith('/host-enrollment-key'):
            reads.append(path)
            if len(reads) < 3:
                raise module._base.ApiError(409, {'detail': 'Current host key is not available'})
            return 200, key
        if path.endswith('/host-enrollment') and method == 'POST':
            return 201, {'challenge': 'c' * 40, 'device_id': 'd' * 36}
        if path.endswith('/sealed'):
            return 202, {'status': 'pending'}
        raise AssertionError((method, path))
    module.enroll(http, 'box-1', 2)
    assert len(reads) == 3


def test_early_probe_failures_are_recorded_but_not_a_quarantine(monkeypatch):
    status, evidence, _, _ = execute(monkeypatch, report=receipt(quarantine={'version': 1, 'servers': {'adp-tools': {'fails': 2, 'last_status': 'error', 'crossed_at': 0.0}}}))
    assert status == 0
    assert evidence['checks']['quarantine']['status'] == 'passed'
    assert evidence['probe_failures'] == {'adp-tools': 2}
    assert evidence['quarantine'] == {'adp-tools': {'fails': 2, 'last_status': 'error', 'last_error': None, 'crossed_at': 0.0}}
