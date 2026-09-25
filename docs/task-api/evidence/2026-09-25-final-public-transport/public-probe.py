"""Read/replay/artifact and natural-window qualification on an existing task only."""
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import time

ROOT = Path('/home/ubuntu/task-delivery-tmp/transport-independent/public')
TASK = 'tsk_37ce236b-19a2-4d7c-a36d-b3609160b999'
ROOT.mkdir(exist_ok=True)
spec = importlib.util.spec_from_file_location('client', '/home/ubuntu/task-delivery/release/examples/task-api/client.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
c = m.Client('https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev', json.loads(Path('/home/ubuntu/task-delivery-tmp/isolation/isolated-token.json').read_text())['access_token'])

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def raw_frames(response, deadline):
    frame = []
    while time.monotonic() < deadline:
        line = response.readline(65537)
        if not line:
            return
        text = line.decode().rstrip('\r\n')
        if text:
            frame.append(text)
        elif frame:
            yield {'event': 'heartbeat' if frame[0].startswith(':') else 'frame', 'wire': frame}
            frame = []

out = {'task_id': TASK, 'started_at': now(), 'writes_performed': 0, 'new_tasks': 0, 'model_calls': 0, 'connections': []}
try:
    snapshot = c.snapshot(TASK)
    assert snapshot['status'] == 'completed'
    assert snapshot['queue_ack_status'] == 'confirmed'
    (ROOT / 'snapshot.json').write_text(json.dumps(snapshot, indent=2) + '\n')
    out['artifact_proofs'] = []
    for artifact in snapshot['result']['artifact_ids']:
        body = c.artifact(TASK, artifact)
        assert json.loads(body) == snapshot['result']['report']
        (ROOT / (artifact + '.json')).write_bytes(body)
        out['artifact_proofs'].append({'artifact_id': artifact, 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(), 'matches_report': True})
    held_at = time.monotonic()
    disconnect = {'started_at': now(), 'last_event_id': snapshot['latest_event_cursor'], 'body_read': False, 'limitation': 'Twenty-second unread-body pause and disconnect; small fixture does not prove public TCP saturation.'}
    with c.open('GET', f'/v1/tasks/{TASK}/events', headers={'Accept': 'text/event-stream', 'Last-Event-ID': snapshot['latest_event_cursor']}, timeout=40) as response:
        disconnect['http_status'] = response.status
        time.sleep(20)
    disconnect['ended_at'] = now()
    disconnect['seconds_open'] = time.monotonic() - held_at
    disconnect['closed_by_observer'] = True
    out['public_unread_body_disconnect'] = disconnect
    replay = []
    with c.open('GET', f'/v1/tasks/{TASK}/events', headers={'Accept': 'text/event-stream', 'Last-Event-ID': TASK + ':7'}, timeout=40) as response:
        out['replay_http_status'] = response.status
        for event in m.parse_sse(response, time.monotonic() + 60):
            replay.append({'received_at': now(), 'event': event})
    ids = [item['event']['id'] for item in replay if 'id' in item['event']]
    assert ids == [TASK + ':' + str(i) for i in range(8, 15)], ids
    assert replay[-1]['event']['data']['type'] == 'task.completed'
    (ROOT / 'replay.ndjson').write_text(''.join(json.dumps(item) + '\n' for item in replay))
    out['replay_exact_cursors_8_through_14'] = True
    cursor = snapshot['latest_event_cursor']
    for i in range(2):
        start = time.monotonic()
        rec = {'started_at': now(), 'last_event_id': cursor, 'frames': 0, 'heartbeats': 0}
        out['connections'].append(rec)
        with c.open('GET', f'/v1/tasks/{TASK}/events', headers={'Accept': 'text/event-stream', 'Last-Event-ID': cursor}, timeout=40) as response:
            rec['http_status'] = response.status
            rec['request_headers_returned'] = {key: response.headers.get(key) for key in ['x-request-id', 'x-amzn-requestid', 'x-amz-apigw-id'] if response.headers.get(key)}
            with (ROOT / f'connection-{i+1}.ndjson').open('w') as f:
                for event in raw_frames(response, time.monotonic() + 660):
                    f.write(json.dumps({'received_at': now(), 'elapsed_seconds': time.monotonic() - start, 'event': event}) + '\n')
                    f.flush()
                    rec['frames'] += 1
                    if event['event'] == 'heartbeat':
                        rec['heartbeats'] += 1
                    if i == 1 and rec['frames'] >= 2:
                        rec['close'] = 'observer-after-snapshot-and-heartbeat'
                        break
                else:
                    rec['close'] = 'natural-eof'
        rec['ended_at'] = now()
        rec['elapsed_seconds'] = time.monotonic() - start
        (ROOT / 'result.json').write_text(json.dumps(out, indent=2) + '\n')
    first, second = out['connections']
    assert first['close'] == 'natural-eof' and 595 <= first['elapsed_seconds'] <= 625
    assert first['heartbeats'] >= 35 and second['heartbeats'] >= 1
    out['criterion_outcome'] = 'PASS'
except Exception as error:
    out['criterion_outcome'] = 'FAIL'
    out['error_type'] = type(error).__name__
    out['error'] = str(error)
out['finished_at'] = now()
(ROOT / 'result.json').write_text(json.dumps(out, indent=2) + '\n')
print(json.dumps(out), flush=True)
raise SystemExit(0 if out['criterion_outcome'] == 'PASS' else 1)
