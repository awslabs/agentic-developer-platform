#!/usr/bin/env python3
"""Owner-path acceptance on an existing dev box; stop and Replace unless --no-replace.

Requires an owner bearer token and operator AWS access. Never logs API bodies,
handoff URLs, host command output, challenges, or credentials. Evidence is JSON.
"""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

_spec = importlib.util.spec_from_file_location('dev_box_verify', Path(__file__).with_name('verify-dev-box.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)


def check(condition, name):
    if not condition:
        raise ValueError(name)


def failure_reason(error):
    """The check name for a refused check, else the exception class; never an API body or a secret."""
    if isinstance(error, ValueError) and str(error) and len(str(error)) <= 80 and '\n' not in str(error):
        return str(error)
    if isinstance(error, _base.ApiError):
        return f'ApiError {error.status}'
    return type(error).__name__


def operation(http, box_id, action, wait):
    # Replace right after a stop is refused (409) until the final checkpoint has been verified
    # (dev 2026-10-23: refused at +0 s, accepted on the owner's retry); poll it like the owner would.
    deadline = time.monotonic() + 300
    while True:
        try:
            # No JSON body: since #8670 a Replace *with* a body is a restore and requires `snapshot_id`
            # (dev 2026-10-23 22:15: `{}` was refused with 422); the plain owner actions send none.
            _, accepted = http('POST', f'/cloud-desktops/{box_id}/{action}', None, {'Idempotency-Key': str(uuid.uuid4())})
            break
        except _base.ApiError as error:
            if action != 'replace' or error.status != 409 or time.monotonic() >= deadline:
                raise
            time.sleep(10)
    def result():
        status = http('GET', f'/cloud-desktops/{box_id}/operations/{accepted["operation_id"]}')[1]['status']
        check(status != 'failed', f'{action} operation failed')
        return status == 'succeeded'
    check(wait(result), f'{action} operation timed out')


def enroll(http, box_id, generation):
    endpoint = f'/cloud-desktops/{box_id}/host-enrollment'
    # The new machine publishes its host key only once its bootstrap has run; until then the key
    # endpoint answers 409 "Current host key is not available" (dev 2026-10-23: ~1-2 min after
    # "running"). Poll it like the owner's page does, bounded.
    deadline = time.monotonic() + 420
    while True:
        try:
            _, key = http('GET', endpoint + '-key')
            if key.get('generation') == generation:
                break
            check(time.monotonic() < deadline, 'host key for the new generation not published')
        except _base.ApiError as error:
            if error.status != 409 or time.monotonic() >= deadline:
                raise
        time.sleep(10)
    pem = base64.b64decode(key['public_key_b64'], validate=True)
    check(key['generation'] == generation and hashlib.sha256(pem).hexdigest() == key['public_key_digest'], 'host key changed')
    check(pem.startswith(b'-----BEGIN PUBLIC KEY-----\n') and pem.endswith(b'-----END PUBLIC KEY-----\n'), 'invalid host key')
    _, approval = http('POST', endpoint, {'public_key_digest': key['public_key_digest']})
    check(bool(re.fullmatch(r'[A-Za-z0-9_-]{32,128}', approval['challenge'])), 'invalid host challenge')
    check(bool(re.fullmatch(r'[A-Za-z0-9-]{32,64}', approval['device_id'])), 'invalid device id')
    key_fd = os.memfd_create('crew-host-public-key', 0)
    try:
        os.write(key_fd, pem)
        result = subprocess.run(['openssl', 'pkeyutl', '-encrypt', '-pubin', '-inkey', f'/proc/self/fd/{key_fd}',
                                 '-pkeyopt', 'rsa_padding_mode:oaep', '-pkeyopt', 'rsa_oaep_md:sha256',
                                 '-pkeyopt', 'rsa_mgf1_md:sha256'], input=approval['challenge'].encode(),
                                capture_output=True, pass_fds=(key_fd,), check=False)
    finally:
        os.close(key_fd)
    check(result.returncode == 0 and len(result.stdout) == 384, 'host seal failed')
    ciphertext = result.stdout
    http('POST', f'{endpoint}/{approval["device_id"]}/sealed', {'ciphertext_b64': base64.b64encode(ciphertext).decode()})


def handoff(http, timeout=30):
    try:
        _, ticket = http('POST', '/crew/handoff-ticket')
    except _base.ApiError as error:
        # 409 lease_fenced: the host binding handshake has not completed yet; "not yet", not a failure.
        if error.status == 409:
            return False
        raise
    url = ticket.get('handoff_url', '')
    parsed = urllib.parse.urlsplit(url)
    check(parsed.scheme == 'https' and parsed.hostname and parsed.path == '/crew/handoff' and
          parsed.port is None and not parsed.username and not parsed.password, 'invalid handoff origin')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None

    request = urllib.request.Request(url, headers={'Accept': 'text/html'})
    try:
        urllib.request.build_opener(NoRedirect).open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        return error.code == 302 and bool(error.headers.get('Set-Cookie', '').startswith('adp_crew='))
    return False


def generation_instances(aws, box_id, generation, deployment_id, region):
    inventory = aws(['ec2', 'describe-instances', '--region', region, '--filters',
                     f'Name=tag:adp:deployment-id,Values={deployment_id}', f'Name=tag:adp:box-id,Values={box_id}',
                     f'Name=tag:adp:generation,Values={generation}'])
    return [instance for group in inventory['Reservations'] for instance in group['Instances']]


def host_report(aws, box_id, generation, deployment_id, region, wait):
    instances = [instance['InstanceId'] for instance in generation_instances(aws, box_id, generation, deployment_id, region)
                 if instance['State']['Name'] == 'running']
    check(len(instances) == 1, 'current instance not uniquely found')
    program = Path(__file__).with_name('owner_paths_host.py').read_bytes()
    encoded = base64.b64encode(program).decode()
    command = "python3 -c 'import base64; exec(compile(base64.b64decode(\"" + encoded + "\"), \"host-probe\", \"exec\"))'"
    sent = aws(['ssm', 'send-command', '--region', region, '--instance-ids', instances[0],
                '--document-name', 'AWS-RunShellScript', '--parameters', json.dumps({'commands': [command]})])
    command_id = sent['Command']['CommandId']
    def completed():
        try:
            reply = aws(['ssm', 'get-command-invocation', '--region', region, '--command-id', command_id,
                         '--instance-id', instances[0]])
        except RuntimeError:
            return None
        if reply['Status'] in ('Pending', 'InProgress', 'Delayed'):
            return None
        check(reply['Status'] == 'Success', 'host probe failed')
        return json.loads(reply['StandardOutputContent'].strip())
    result = wait(completed)
    check(result is not None and 'error' not in result, 'host probe unavailable')
    return result, instances[0]


def run(http, aws, *, box_id, deployment_id, region, expected_revision, no_replace=False,
        sleep=time.sleep, clock=time.monotonic):
    evidence = {'checks': {}, 'started_at': time.time()}
    current_check = 'setup'
    def wait(predicate, timeout=600, interval=5):
        end = clock() + timeout
        while True:
            outcome = predicate()
            if outcome:
                return outcome
            if clock() >= end:
                return None
            sleep(interval)

    def record(name, action):
        nonlocal current_check
        current_check = name
        try:
            value = action()
            check(value is not False, name + ' failed')
            evidence['checks'][name] = {'status': 'passed', 'value': value}
        except (ValueError, KeyError, _base.ApiError, OSError, RuntimeError) as error:
            evidence['checks'][name] = {'status': 'failed', 'reason': failure_reason(error)}
            raise

    try:
        # The policy's admission flag answers "may this owner reserve a NEW box"; for an owner who
        # already has one it is false with reason "A desktop is already reserved for this owner"
        # (dev 2026-10-23), which is exactly the state this runner needs. Record it, never gate on it.
        _, policy = http('GET', '/cloud-desktops/policy')
        evidence['policy'] = {'admission_allowed': policy.get('admission_allowed'), 'reason': policy.get('reason')}
        _, listing = http('GET', '/cloud-desktops')
        boxes = [box for box in listing['items'] if box['status'] != 'released']
        check(len(boxes) == 1 and (box_id is None or boxes[0]['desktop_id'] == box_id), 'expected one owned box')
        box_id = boxes[0]['desktop_id']
        evidence['box_id'] = box_id
        _, box = http('GET', f'/cloud-desktops/{box_id}')
        if not no_replace:
            current_check = 'replace'
            previous = generation_instances(aws, box_id, box['machine_generation'], deployment_id, region)
            check(len(previous) == 1, 'previous instance not uniquely found')
            if box['observed_state'] != 'stopped':
                operation(http, box_id, 'stop', wait)
                check(wait(lambda: http('GET', f'/cloud-desktops/{box_id}')[1]['observed_state'] == 'stopped'), 'stop not observed')
            operation(http, box_id, 'replace', wait)
            # A Replace of a stopped box produces a stopped machine (dev 2026-10-23, gen 37): the owner
            # starts it afterwards, so the runner does the same instead of waiting for "running" in vain.
            if http('GET', f'/cloud-desktops/{box_id}')[1]['observed_state'] == 'stopped':
                operation(http, box_id, 'start', wait)
        else:
            current_check = 'start'
            if box['observed_state'] != 'stopped':
                operation(http, box_id, 'stop', wait)
                check(wait(lambda: http('GET', f'/cloud-desktops/{box_id}')[1]['observed_state'] == 'stopped'), 'stop not observed')
            operation(http, box_id, 'start', wait)
        running = wait(lambda: (lambda state: state if state['observed_state'] == 'running' and state['status'] == 'running' else None)(
            http('GET', f'/cloud-desktops/{box_id}')[1]))
        check(running is not None, 'running not observed')
        evidence['generation'] = running['machine_generation']
        observed_at = clock()
        def target_ready():
            try:
                target = http('GET', '/crew/target')[1]
            except _base.ApiError as error:
                if error.status in (404, 409):
                    return False
                raise
            lease = target.get('lease') or {}
            return target.get('health') == 'ready' and lease.get('target_kind') == 'ec2-instance' and \
                lease.get('target_ref') == box_id and lease.get('generation') == running['machine_generation']
        if running.get('host_binding') == 'none':
            # A replaced machine must be enrolled before the owner can open it; the binding handshake then
            # takes a few minutes (dev 2026-10-23: ~3 min from seal to ready). AC-01's 20 s / 60 s window is
            # the owner's *start* path, so after a Replace the clock starts once the target is bound.
            current_check = 'enrollment'
            enroll(http, box_id, running['machine_generation'])
            record('target', lambda: wait(target_ready, timeout=600, interval=10) is True)
            observed_at = clock()
        sleep(max(0, 20 - (clock() - observed_at)))
        record('handoff', lambda: wait(lambda: handoff(http), timeout=max(0, 60 - (clock() - observed_at)), interval=2) is True)
        if 'target' not in evidence['checks']:
            record('target', lambda: wait(target_ready, timeout=60) is True)
        sleep(max(0, 90 - (clock() - observed_at)))
        current_check = 'host_probe'
        report, instance = host_report(aws, box_id, running['machine_generation'], deployment_id, region, wait)
        # AC-02 needs an *expired* pushed token. Kiro Crew's own boot-time traffic can keep it fresh past the
        # 90 s mark (dev 2026-10-24, gen 44), so wait out up to three more token lifetimes before judging.
        for _ in range(3):
            if report['token_ttl_before'] < 0:
                break
            sleep(65)
            report, instance = host_report(aws, box_id, running['machine_generation'], deployment_id, region, wait)
        evidence['instance_id'] = instance
        record('model_after_idle', lambda: report['token_ttl_before'] < 0 and report['model']['status'] == 200 and
               report['token_ttl_after'] > 0 and report['adapter_failures'] == 0)
        record('tools', lambda: report['tools']['status'] == 200 and report['tools']['tool_count'] >= 10)
        # Kiro Crew's quarantine file is {"version": 1, "servers": {name: {fails, crossed_at, ...}}}; a server
        # is quarantined only once `crossed_at` is set (three failed probes). Early probe failures before the
        # first listing answered are recorded, not failed (dev 2026-10-23, gen 42: two boot-time timeouts,
        # then healthy; the first full run failed here on the non-empty envelope).
        servers = (report['quarantine'] or {}).get('servers') or {}
        evidence['probe_failures'] = {name: int(state.get('fails') or 0) for name, state in servers.items()}
        # #8835: a quarantine (or a near miss) names the server and Kiro's last probe error in the evidence.
        evidence['quarantine'] = {name: {key: state.get(key) for key in ('fails', 'last_status', 'last_error', 'crossed_at')}
                                  for name, state in servers.items()}
        record('quarantine', lambda: not any(state.get('crossed_at') for state in servers.values()))
        if not no_replace:
            record('staged_bundle', lambda: report['revision'] == report['link'] == expected_revision)
            record('previous_instance_retired', lambda: wait(lambda: all(
                instance['State']['Name'] == 'terminated' for instance in generation_instances(
                    aws, box_id, box['machine_generation'], deployment_id, region)), timeout=60) is True)
        evidence['measurements'] = {key: report[key] for key in ('token_ttl_before', 'token_ttl_after', 'model', 'tools', 'adapter_failures')}
    except (ValueError, KeyError, _base.ApiError, OSError, RuntimeError) as error:
        evidence['failure'] = failure_reason(error)
        evidence['checks'].setdefault(current_check, {'status': 'failed', 'reason': failure_reason(error)})
        return 1, evidence
    return 0, evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', default='dev')
    parser.add_argument('--region', default=os.getenv('AWS_REGION', 'us-east-1'))
    parser.add_argument('--gateway-url')
    parser.add_argument('--token')
    parser.add_argument('--box-id')
    parser.add_argument('--no-replace', action='store_true')
    parser.add_argument('--expected-revision', help='profile revision the staged release carries (deploy-dev-box.sh passes it); '
                        'default: SSM crew-bundle-revision, the user-data bundle the stage published')
    parser.add_argument('--evidence', required=True)
    args = parser.parse_args(argv)
    if args.env != 'dev':
        parser.error('live acceptance is limited to dev')
    try:
        token = _base.resolve_token(args.token)
        gateway = _base.Gateway(_base.resolve_gateway_url(args.gateway_url, args.env, args.region), token)
        deployment = _base.resolve_deployment_id(None, args.env, args.region)
        # The staged Crew bundle is the user-data release (SSM crew-bundle-revision); profile-revision is the
        # AMI-baked bundle and lags it (dev 2026-10-23: 17 Oct vs the 22 Oct release), so it is never the reference.
        staged = None if args.no_replace else args.expected_revision or _base.run_aws(['ssm', 'get-parameter', '--region', args.region,
                                '--name', f'/adp/{args.env}/dev-box/crew-bundle-revision'])['Parameter']['Value']
        result, evidence = run(gateway, _base.run_aws, box_id=args.box_id, region=args.region,
                               deployment_id=deployment, expected_revision=staged, no_replace=args.no_replace)
    except (ValueError, KeyError, OSError, RuntimeError, _base.ApiError) as error:
        result, evidence = 1, {'checks': {}, 'failure': failure_reason(error)}
    path = Path(args.evidence)
    if path.exists():
        parser.error('evidence path already exists')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        json.dump(evidence, output, sort_keys=True)
    print(f'owner-paths: {"passed" if result == 0 else "failed"}; evidence: {path}')
    return result


if __name__ == '__main__':
    sys.exit(main())
