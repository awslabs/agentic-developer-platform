#!/usr/bin/env python3
"""Read an owned running fixture's private progress and authenticated state."""
import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from collect_security import runtime_counters


def observe(config, run, request):
    base = config['gateway_url'].rstrip('/')
    if urlsplit(base).scheme != 'https':
        raise ValueError('HTTPS fixture gateway required')
    path = config['runtime_progress_path']
    if not isinstance(path, str) or not path.startswith('/') or not path.endswith('.progress.json'):
        raise ValueError('explicit absolute runtime progress path required')
    kube = ['kubectl', '--kubeconfig', config['kubeconfig'], '-n', config['runtime_namespace']]
    pod_name = config['runtime_pod_name']
    def pod():
        obj = json.loads(run(kube + ['get', 'pod', pod_name, '-o', 'json']))
        meta = obj['metadata']
        if (meta['uid'] != config['runtime_pod_uid'] or meta['name'] != pod_name or
            meta['namespace'] != config['runtime_namespace'] or
            meta.get('labels', {}).get('adp.io/w2-fixture') != config['runtime_run_id']):
            raise ValueError('fixture pod ownership differs')
        statuses = obj.get('status', {}).get('containerStatuses', [])
        worker = next((x for x in statuses if x['name'] == 'agent-worker'), {})
        if 'running' not in worker.get('state', {}) or worker.get('restartCount') != 0:
            raise ValueError('fixture worker is not running without restarts')
    def progress():
        return json.loads(run(kube + ['exec', pod_name, '-c', 'agent-worker', '--', 'cat', '--', path]))
    pod()
    before = progress()
    url = base + '/activity/invocations/' + quote(config['live_run_id'], safe='') + '/agent/state'
    state = request(url)
    if (state.get('run_id') != config['live_run_id'] or state.get('available') is not True or
        state.get('state') != 'paused' or state.get('active_tool_count') != 0):
        raise ValueError('security observations require an available paused fixture')
    after = progress()
    pod()
    keys = ('sdk_queries', 'tool_starts', 'active_tools', 'counters_complete', 'dropped_events',
            'invocation_id', 'run_id', 'source_revision', 'generation')
    if any(before.get(key) != after.get(key) for key in keys):
        raise ValueError('runtime changed during state observation')
    result = dict(progress=after, state=state, pod_uid=config['runtime_pod_uid'],
        observed_at=datetime.now(timezone.utc).isoformat(),
        observed_by='UID-verified kubectl progress reads bracketing authenticated GET ' + url)
    runtime_counters(config, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    token = os.environ[config['identity_env']['owner']]
    if not token: raise ValueError('owner session required')
    import httpx
    def run(argv):
        return subprocess.run(argv, capture_output=True, text=True, timeout=15, check=True).stdout
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        def request(url):
            response = client.get(url, headers={'Authorization': 'Bearer ' + token})
            response.raise_for_status()
            return response.json()
        print(json.dumps(observe(config, run, request)))

if __name__ == '__main__':
    main()
