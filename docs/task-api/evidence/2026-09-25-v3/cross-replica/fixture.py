"""Two exact gateway pod streams plus a public third-reader quota probe."""
import argparse
import datetime
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--image-digest', required=True)
parser.add_argument('--source-sha', required=True)
args = parser.parse_args()
ROOT = Path('/home/ubuntu/task-delivery-tmp/transport-independent/cross-replica')
ROOT.mkdir(exist_ok=True)
TASK = 'tsk_37ce236b-19a2-4d7c-a36d-b3609160b999'
KUBE = ['kubectl', '--kubeconfig', '/tmp/adp-runner-check-kubeconfig', '-n', 'adp-gateway']
TOKEN = json.loads(Path('/home/ubuntu/task-delivery-tmp/isolation/isolated-token.json').read_text())['access_token']
CURSOR = TASK + ':14'
processes, responses, logs = [], [], []

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def request(base):
    req = urllib.request.Request(base + f'/v1/tasks/{TASK}/events', headers={'Authorization': 'Bearer ' + TOKEN, 'Accept': 'text/event-stream', 'Last-Event-ID': CURSOR})
    return urllib.request.urlopen(req, timeout=35)

def read_frame(response):
    frame = []
    while True:
        line = response.readline(65537)
        if not line:
            raise RuntimeError('Stream ended before snapshot')
        text = line.decode().rstrip('\r\n')
        if not text and frame:
            break
        frame.append(text)
    return frame

def first_frame(response):
    frame = read_frame(response)
    assert 'event: snapshot' in frame, frame
    assert not any(line.startswith('id:') for line in frame)
    return frame

out = {'started_at': now(), 'task_id': TASK, 'cursor': CURSOR, 'image_digest': args.image_digest, 'source_sha': args.source_sha, 'new_tasks': 0, 'model_calls': 0, 'task_state_writes': 0, 'direct_pod_streams': [], 'public_checks': []}
try:
    pods = json.loads(subprocess.check_output(KUBE + ['get', 'pods', '-o', 'json']))['items']
    candidates = [p for p in pods if p['metadata']['name'].startswith('bedrockgateway-') and not p['metadata'].get('deletionTimestamp') and p['status']['phase'] == 'Running' and all(c.get('ready') for c in p['status'].get('containerStatuses', [])) and any(args.image_digest in c['image'] for c in p['spec']['containers'])]
    assert len(candidates) >= 2
    for i, pod in enumerate(candidates[:2]):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        log = (ROOT / f'port-forward-{i+1}.txt').open('w')
        logs.append(log)
        process = subprocess.Popen(KUBE + ['port-forward', '--address', '127.0.0.1', 'pod/' + pod['metadata']['name'], f'{port}:8080'], stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        deadline = time.monotonic() + 20
        while True:
            assert process.poll() is None, 'Port-forward exited'
            try:
                connection = socket.create_connection(('127.0.0.1', port), timeout=1)
                connection.close()
                break
            except OSError:
                assert time.monotonic() < deadline, 'Port-forward never opened'
                time.sleep(.2)
        response = request(f'http://127.0.0.1:{port}')
        responses.append(response)
        out['direct_pod_streams'].append({'pod': pod['metadata']['name'], 'pod_uid': pod['metadata']['uid'], 'image_ids': [c.get('imageID') for c in pod['status'].get('containerStatuses', [])], 'http_status': response.status, 'snapshot_wire': first_frame(response), 'opened_at': now(), 'lane': 'authenticated direct pod through owned port-forward'})
    assert out['direct_pod_streams'][0]['pod_uid'] != out['direct_pod_streams'][1]['pod_uid']
    held_start = time.monotonic()
    out['lease_hold'] = {'started_at': now(), 'minimum_seconds': 55, 'heartbeats': [[], []]}
    while time.monotonic() - held_start < 55:
        for i, response in enumerate(responses[:2]):
            frame = read_frame(response)
            assert frame[0].startswith(':'), frame
            assert not any(line.startswith('id:') for line in frame)
            out['lease_hold']['heartbeats'][i].append({'received_at': now(), 'wire': frame})
    out['lease_hold']['ended_at'] = now()
    out['lease_hold']['seconds'] = time.monotonic() - held_start
    base = 'https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev'
    try:
        third = request(base)
        responses.append(third)
        out['public_checks'].append({'at': now(), 'status': third.status, 'phase': 'two-distinct-pods-held'})
        raise AssertionError('Third public reader unexpectedly admitted')
    except urllib.error.HTTPError as error:
        body = json.loads(error.read())
        out['public_checks'].append({'at': now(), 'status': error.code, 'body': body, 'phase': 'two-distinct-pods-held'})
        assert error.code == 429 and body['code'] == 'rate_limited'
        assert body['message'] == 'This task is at its concurrent Task API stream limit.'
    responses[0].close()
    out['first_direct_closed_at'] = now()
    released = False
    for _ in range(20):
        try:
            admitted = request(base)
            responses.append(admitted)
            out['public_checks'].append({'at': now(), 'status': admitted.status, 'phase': 'one-direct-pod-held', 'snapshot_wire': first_frame(admitted)})
            assert admitted.status == 200
            released = True
            admitted.close()
            break
        except urllib.error.HTTPError as error:
            body = json.loads(error.read())
            out['public_checks'].append({'at': now(), 'status': error.code, 'body': body, 'phase': 'release-observation'})
            assert error.code == 429
            time.sleep(1)
    assert released, 'Closed stream slot did not release within bounded observation'
    out['criterion_outcome'] = 'PASS'
except Exception as error:
    out['criterion_outcome'] = 'FAIL'
    out['error_type'] = type(error).__name__
    out['error'] = str(error)
finally:
    for response in responses:
        response.close()
    for process in processes:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    for log in logs:
        log.close()
    out['cleanup'] = {'responses_closed': len(responses), 'port_forward_processes_stopped': all(p.poll() is not None for p in processes), 'processes_created': len(processes)}
    out['finished_at'] = now()
    (ROOT / 'result.json').write_text(json.dumps(out, indent=2) + '\n')
print(json.dumps(out), flush=True)
raise SystemExit(0 if out['criterion_outcome'] == 'PASS' else 1)
