#!/usr/bin/env python3
"""Transfer an immutable experiment bundle to an already authenticated owned worker.

This never seeds authority, publishes a task, or creates a pod. Those prerequisites
must finish first. Run with the operator kubeconfig, never the worker credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

from worker_observation import observe_worker_pod

REMOTE = '/work/control-evaluation'


def validate_inputs(envelope, ledger, identity, bundle):
    expected = identity['expected_identity']
    request = envelope['payload']['control_evaluation']
    if (request['run_id'] != ledger['run_id'] or request['run_nonce'] != ledger['run_nonce']
            or expected['run_id'] != ledger['run_id'] or expected['nonce'] != ledger['run_nonce']):
        raise ValueError('handoff documents belong to different fixtures')
    owned = [row for row in ledger['k8s'] if row.get('kind') == 'Pod'
             and row.get('name') == expected['pod_name'] and row.get('namespace') == expected['namespace']
             and row.get('uid') == expected['pod_uid'] and row.get('created_by_this_run') is True]
    if len(owned) != 1:
        raise ValueError('worker must be recorded as owned in the cleanup ledger')
    with bundle.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != request['bundle_sha256']:
        raise ValueError('bundle differs from protected dispatch')
    heads = subprocess.check_output(['git', 'bundle', 'list-heads', str(bundle)], text=True)
    if request['source_revision'] not in [line.split()[0] for line in heads.splitlines()]:
        raise ValueError('requested source revision is not a bundle head')
    return expected, request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('envelope', 'ledger', 'identity', 'bundle', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=1900)
    args = parser.parse_args()
    envelope, ledger, identity = [json.loads(p.read_text()) for p in (args.envelope, args.ledger, args.identity)]
    expected, request = validate_inputs(envelope, ledger, identity, args.bundle)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.output.chmod(0o700)
    base = ['kubectl', '-n', expected['namespace']]
    target = expected['pod_name']

    def command(*parts, **kwargs):
        return subprocess.check_output([*base, *parts], timeout=60, **kwargs)

    def check_pod():
        pod = json.loads(command('get', 'pod', target, '-o', 'json'))
        observed = observe_worker_pod(pod, run_id=request['run_id'], nonce=request['run_nonce'],
                                     job_uid=expected['job_uid'], service_account=expected['service_account'],
                                     approved_digests=[expected['runtime_image_digest']])
        if observed['pod_uid'] != expected['pod_uid']:
            raise ValueError('worker pod was replaced')
        return pod

    (args.output / 'pod-before-handoff.json').write_text(json.dumps(check_pod()))
    ready = json.loads(command('exec', target, '-c', 'agent-worker', '--', 'cat', REMOTE + '/bootstrap-ready.json'))
    if ready != {'pod_uid': expected['pod_uid'], 'run_id': request['run_id'],
                 'run_nonce': request['run_nonce'], 'invocation_id': envelope['message_id']}:
        raise ValueError('authenticated worker acquired a different dispatch')
    started = command('exec', target, '-c', 'agent-worker', '--', 'sh', '-c',
                      'if test -e /work/control-evaluation/ready; then echo started; fi')
    if started.strip():
        raise ValueError('handoff already started; collect existing evidence without retransferring source')
    # Transfer data first. No paid experiment may start before every transfer succeeds.
    for path, name in ((args.bundle, 'source.bundle'), (args.identity, 'expected-identity.json'),
                       (args.ledger, 'cleanup-ledger.json')):
        check_pod()
        command('cp', str(path.resolve()), target + ':' + REMOTE + '/' + name, '-c', 'agent-worker')
    check_pod()
    command('exec', target, '-c', 'agent-worker', '--', 'touch', REMOTE + '/ready')
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        check_pod()
        exists = command('exec', target, '-c', 'agent-worker', '--', 'sh', '-c',
                         'if test -f /work/control-evaluation/result.json; then echo ready; fi')
        if exists.strip() == b'ready':
            break
        time.sleep(10)
    else:
        raise TimeoutError('worker did not finish; preserve the ledger and drain it before cleanup')
    for name in ('result.json', 'experiment.log', 'bootstrap-ready.json'):
        data = command('exec', target, '-c', 'agent-worker', '--', 'cat', REMOTE + '/' + name)
        (args.output / name).write_bytes(data)
    result = json.loads((args.output / 'result.json').read_text())
    if result.get('pod_uid') != expected['pod_uid'] or result.get('run_nonce') != request['run_nonce']:
        raise ValueError('collected result belongs to another worker')
    # Preserve logs even if the experiment failed before creating its evidence directory.
    evidence = command('exec', target, '-c', 'agent-worker', '--', 'sh', '-c',
                       'if test -d /work/control-evaluation/evidence; then echo exists; fi')
    if evidence.strip():
        command('cp', target + ':' + REMOTE + '/evidence', str(args.output / 'evidence'), '-c', 'agent-worker')
    elif result['exit_code'] == 0:
        raise ValueError('successful experiment has no evidence directory')
    check_pod()
    command('exec', target, '-c', 'agent-worker', '--', 'touch', REMOTE + '/collected')
    return int(result['exit_code'])


if __name__ == '__main__':
    raise SystemExit(main())
