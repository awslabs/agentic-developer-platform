#!/usr/bin/env python3
"""SSM-side Crew measurements. Print only a fixed, secret-free JSON receipt."""

import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request


def model_environment():
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            raw = (process / 'environ').read_bytes()
            values = dict(item.decode().split('=', 1) for item in raw.split(b'\0') if b'=' in item)
            if values.get('ANTHROPIC_BASE_URL', '').startswith('http://127.0.0.1:') and values.get('ANTHROPIC_API_KEY'):
                return values
        except (OSError, UnicodeError, ValueError):
            continue
    raise ValueError('Crew adapter process unavailable')


def request(base, secret, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method='POST' if data else 'GET',
                                 headers={'x-api-key': secret, 'content-type': 'application/json'})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            payload = json.load(response)
            return {'status': response.status, 'latency_ms': round((time.monotonic() - started) * 1000),
                    'tool_count': len(payload.get('tools', [])) if path == '/tools' else None}
    except urllib.error.HTTPError as error:
        return {'status': error.code, 'latency_ms': round((time.monotonic() - started) * 1000)}


def measure():
    report = {'revision': json.loads(Path('/opt/adp/crew/current/profile.json').read_text())['revision'],
              'link': os.readlink('/opt/adp/crew/current')}
    quarantine = Path('/mnt/crew/mcp-quarantine.json')
    report['quarantine'] = json.loads(quarantine.read_text()) if quarantine.exists() else {}
    token = json.loads(Path('/run/adp/crew-host/model-token/token.json').read_text())
    report['token_ttl_before'] = token['expires_at'] - time.time()
    # Count adapter failures only during this probe: the first minutes after boot carry Kiro Crew's own
    # probe/background traffic (dev 2026-10-24, gen 44: a healthy box failed the idle check on boot noise).
    probe_started = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
    environment = model_environment()
    base, secret = environment['ANTHROPIC_BASE_URL'], environment['ANTHROPIC_API_KEY']
    report['model'] = request(base, secret, '/v1/messages', {
        'model': environment['ANTHROPIC_MODEL'], 'max_tokens': 16,
        'messages': [{'role': 'user', 'content': 'ping'}]})
    report['tools'] = request(base, secret, '/tools')
    report['token_ttl_after'] = json.loads(Path('/run/adp/crew-host/model-token/token.json').read_text())['expires_at'] - time.time()
    journal = subprocess.run(['journalctl', '-u', 'adp-crew', '--since', probe_started, '--utc', '--no-pager'],
                             capture_output=True, text=True, check=False)
    report['adapter_failures'] = journal.stdout.count('crew_model_adapter_failure')
    return report


if __name__ == '__main__':
    try:
        print(json.dumps(measure()))
    except (OSError, ValueError, KeyError, TypeError, TimeoutError, urllib.error.URLError) as error:
        print(json.dumps({'error': type(error).__name__}))
