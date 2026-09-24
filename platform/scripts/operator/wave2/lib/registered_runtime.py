#!/usr/bin/env python3
"""Read the SAME fixture invocation's terminal row after handoff collection.

No task dispatch or mutation. Session credentials remain in a private file.
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def verify_runtime(runtime, envelope):
    request = envelope['payload']['control_evaluation']
    if request.get('mode') not in ('registered-control', 'native-interrupt'):
        raise ValueError('registered fixture mode is required')
    if type(runtime.get('generation')) is not int or runtime['generation'] < 1:
        raise ValueError('registered control generation is missing')
    if type(runtime.get('exit_code')) is not int or runtime['exit_code'] not in (0, 1):
        raise ValueError('runtime exit code is invalid')
    for key, expected in {'mode': request.get('mode'), 'invocation_id': envelope['message_id'],
                          'run_id': request['run_id'], 'source_revision': request['source_revision']}.items():
        if runtime.get(key) != expected:
            raise ValueError('runtime identity mismatch: ' + key)
    if runtime.get('timed_out') is not False or type(runtime.get('dropped_events')) is not int or runtime.get('dropped_events') != 0:
        raise ValueError('runtime timed out or lost observations')
    if runtime.get('cleanup_errors') != []:
        raise ValueError('runtime cleanup is incomplete')
    events = runtime.get('events')
    if not isinstance(events, list) or not events:
        raise ValueError('runtime events are missing')
    if any(not isinstance(event, dict) for event in events):
        raise ValueError('runtime event is malformed')
    try:
        timestamps = [datetime.fromisoformat(event['at']) for event in events]
        if any(stamp.tzinfo is None for stamp in timestamps) or timestamps != sorted(timestamps):
            raise ValueError('runtime events are not chronologically ordered')
    except (KeyError, TypeError) as error:
        raise ValueError('runtime event timestamp is missing or malformed') from error
    types = [event.get('type') for event in events]
    if types[-1] != 'runtime_disposed' or 'listener_started' not in types or 'sdk_attempt_attached' not in types:
        raise ValueError('production runtime lifecycle is incomplete')
    if types.count('listener_started') != 1 or types.index('listener_started') > types.index('sdk_attempt_attached'):
        raise ValueError('listener did not precede the SDK attempt')
    if events[types.index('listener_started')].get('generation') != runtime['generation']:
        raise ValueError('listener generation mismatch')
    if request['mode'] == 'native-interrupt':
        required = ['sdk_attempt_attached', 'native_interrupt_requested', 'native_interrupt_acknowledged', 'runtime_disposed']
        positions = [types.index(key) if types.count(key) == 1 else -1 for key in required]
        if -1 in positions or positions != sorted(positions):
            raise ValueError('native interruption causal trace is incomplete')
        before = events[:positions[1]]
        if not any(event.get('type') == 'sdk_message' and event.get('message_type') == 'assistant' for event in before):
            raise ValueError('native interruption has no observed active turn')
        if runtime.get('native_requested') is not True or runtime.get('native_acknowledged') is not True:
            raise ValueError('SDK native interruption was not acknowledged')
    return request


def terminal_observation(runtime, row):
    if row.get('invocation_id') != runtime['invocation_id']:
        raise ValueError('terminal row belongs to a different invocation')
    status = row.get('status')
    if status not in ('complete', 'completed', 'failed', 'aborted'):
        return None
    if runtime['mode'] == 'native-interrupt' and status == 'aborted':
        raise ValueError('native SDK interruption was misclassified as ADP abort')
    if status in ('complete', 'completed') and runtime['exit_code'] != 0:
        raise ValueError('failed runtime was recorded as complete')
    if status == 'failed' and runtime['exit_code'] == 0:
        raise ValueError('successful runtime was recorded as failed')
    return {'run_id': row['invocation_id'], 'status': status,
            'observed_by': 'authenticated GET /me/agent-invocations/{invocation_id}',
            'observed_at': datetime.now(timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('runtime', 'envelope', 'session-file', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--gateway-url', required=True)
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    if urlsplit(args.gateway_url).scheme != 'https' or not 1 <= args.timeout <= 600:
        raise ValueError('HTTPS gateway and bounded timeout are required')
    runtime = json.loads(args.runtime.read_text())
    verify_runtime(runtime, json.loads(args.envelope.read_text()))
    session = json.loads(args.session_file.read_text())
    token = session.get('id_token') or session.get('access_token')
    if not isinstance(token, str) or not token:
        raise ValueError('authenticated owner session is required')
    url = args.gateway_url.rstrip('/') + '/me/agent-invocations/' + quote(runtime['invocation_id'], safe='')
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(Request(url, headers={'Authorization': 'Bearer ' + token}), timeout=min(20, max(1, deadline - time.monotonic()))) as response:
                row = json.load(response)
        except HTTPError as error:
            if error.code != 404:
                raise
            time.sleep(2)
            continue
        observation = terminal_observation(runtime, row)
        if observation:
            args.out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            args.out.touch(mode=0o600, exist_ok=True)
            args.out.chmod(0o600)
            args.out.write_text(json.dumps({'runtime': runtime, 'terminal_row': row,
                                           'native_interrupt_status': observation if runtime['mode'] == 'native-interrupt' else None}, indent=2))
            args.out.chmod(0o600)
            return 0
        time.sleep(2)
    raise TimeoutError('same invocation has no observed terminal row; preserve the fixture ledger')


if __name__ == '__main__':
    raise SystemExit(main())
