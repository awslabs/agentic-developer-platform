#!/usr/bin/env python3
"""Exercise both deployed HTTP adapters with isolated outbound fault injection.

Run in a separate process inside the owned fixture gateway. JWT authentication,
SQL identity resolution, protected authority, request validation and envelope
signing remain real. Only the outbound target/credential is faulted, after
authorization, for one explicitly named fixture invocation. No registration or
running gateway process is modified. The HTTP client refuses unexpected network
destinations even if destination validation regresses.
"""
import argparse
import asyncio
import dataclasses
import hashlib
import inspect
import json
import os
import secrets
import socket
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from fastapi import FastAPI

from src.activity import routes as activity
from src.activity.control_service import ControlService, ControlError
from src.orchestration import controls as orchestration


async def capture(config):
    run_id = config['run_id']
    gateway_ip = config['gateway_pod_ip']
    worker_ip = config['worker_pod_ip']
    if gateway_ip != socket.gethostbyname(socket.gethostname()):
        raise ValueError('probe must run inside the owned gateway')
    source_hashes = {
        name: hashlib.sha256(Path(inspect.getfile(obj)).read_bytes()).hexdigest()
        for name, obj in [('service', ControlService), ('activity', activity), ('orchestration', orchestration)]
    }
    if source_hashes != config['source_sha256']:
        raise ValueError('deployed route/service source differs')
    session = json.loads(Path(config['session_file']).read_text())
    expired = json.loads(Path(config['expired_token_file']).read_text())
    if expired['run_id'] != run_id or expired['expiry_observed'] is not True:
        raise ValueError('expired credential lacks actual expiry observation')
    if datetime.fromisoformat(expired['valid_until'].replace('Z', '+00:00')) >= datetime.now(timezone.utc):
        raise ValueError('previous credential is not yet expired')
    redirect_requests = []

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            redirect_requests.append(self.path)
            self.send_response(307)
            self.send_header('Location', 'http://169.254.169.254/latest/meta-data/')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((gateway_ip, 8770), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fault = {'case': None, 'group': None}
    outgoing = []
    forward_calls = []

    class ContainedTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            observation = {'host': request.url.host, 'port': request.url.port, 'path': request.url.path}
            outgoing.append(observation)
            if request.url.host not in {worker_ip, gateway_ip} or request.url.port != 8770:
                raise RuntimeError('forbidden destination reached transport')
            if fault['group'] == 'token_expiry' and fault['case'] == 'missing':
                del request.headers['Authorization']
            response = await self.inner.handle_async_request(request)
            observation['status'] = response.status_code
            return response

        async def aclose(self):
            await self.inner.aclose()

    class FaultedService(ControlService):
        async def forward_command(self, target, action, *, request_body, envelope):
            if target.run_id != run_id or target.address != worker_ip:
                raise ValueError('fault injection refuses any other execution')
            forward_calls.append(json.loads(request_body)['command_id'])
            group, case = fault['group'], fault['case']
            changes = {}
            if group == 'token_expiry':
                if case == 'wrong':
                    changes['token'] = secrets.token_urlsafe(32)
                elif case == 'expired':
                    changes['token'] = expired['token']
            elif group == 'stale_generation':
                changes['generation'] = target.generation - 1
            elif group == 'unsafe_targets':
                address, port = {
                    'unregistered_ip': ('10.255.255.254', 8770),
                    'wrong_port': (worker_ip, 8771),
                    'metadata': ('169.254.169.254', 8770),
                    'link_local': ('169.254.10.10', 8770),
                    'loopback': ('127.0.0.1', 8770),
                    'public': ('8.8.8.8', 8770),
                    'redirect': (gateway_ip, 8770),
                }[case]
                changes.update(address=address, port=port)
            else:
                raise ValueError('a rejecting fault is required')
            return await super().forward_command(dataclasses.replace(target, **changes), action,
                                                  request_body=request_body, envelope=envelope)

    rows = []
    try:
        async with httpx.AsyncClient(transport=ContainedTransport(), trust_env=False) as pod_client:
            service = FaultedService(http_client=pod_client)
            app = FastAPI()
            app.include_router(activity.router)
            app.include_router(orchestration.router)
            app.dependency_overrides[activity.get_control_service] = lambda: service
            app.dependency_overrides[orchestration.get_run_control_service] = lambda: service
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture-route-probe',
                                         headers={'Authorization': 'Bearer ' + session['access_token']}) as client:
                for adapter, path in [('activity', f'/activity/invocations/{run_id}/agent'),
                                      ('orchestration', f'/orchestration/runs/{run_id}')]:
                    for verb in ('pause', 'resume', 'steer', 'abort'):
                        for group, cases in [('token_expiry', ('missing', 'wrong', 'expired')),
                                             ('stale_generation', ('stale',)),
                                             ('unsafe_targets', ('unregistered_ip', 'wrong_port', 'metadata',
                                                                 'link_local', 'loopback', 'public', 'redirect'))]:
                            for case in cases:
                                fault.update(group=group, case=case)
                                command_id = str(uuid.uuid4())
                                body = {'command_id': command_id}
                                if verb == 'steer':
                                    body['instruction'] = 'Rejected security fixture command'
                                # The operator brackets this exact request with
                                # UID-verified worker progress/journal snapshots.
                                # No command is sent until its before-read exists.
                                if config.get('interactive'):
                                    print(json.dumps({'ready': command_id, 'adapter': adapter,
                                                      'verb': verb, 'group': group, 'case': case}), flush=True)
                                    permit = await asyncio.to_thread(sys.stdin.readline)
                                    if permit.strip() != command_id:
                                        raise ValueError('operator observation handshake missing')
                                before = len(outgoing)
                                before_redirect = len(redirect_requests)
                                start_forward = len(forward_calls)
                                response = await client.post(path + '/' + verb, json=body)
                                observed = outgoing[before:]
                                expected_status = 409 if group == 'unsafe_targets' and case != 'redirect' else 502
                                reached = forward_calls[start_forward:] == [command_id]
                                forbidden = [x for x in observed if x['host'] not in {worker_ip, gateway_ip} or x['port'] != 8770]
                                listener_status = observed[0].get('status') if len(observed) == 1 else None
                                listener_refused = group == 'unsafe_targets' or listener_status == 401
                                transport_shape = True
                                if group == 'unsafe_targets':
                                    transport_shape = (listener_status == 307 and len(redirect_requests) == before_redirect + 1
                                                       if case == 'redirect' else len(observed) == 0)
                                row = dict(adapter=adapter, verb=verb, group=group, case=case,
                                           command_id=command_id, run_id=run_id, status=response.status_code,
                                           authenticated_forward_reached=reached,
                                           listener_status=listener_status,
                                           transport_attempts=len(forbidden), outgoing_count=len(observed),
                                           passed=reached and response.status_code == expected_status and not forbidden and listener_refused and transport_shape,
                                           observed_at=datetime.now(timezone.utc).isoformat(),
                                           observed_by='deployed ASGI route, real JWT/SQL/authority/signing; isolated post-authorization outbound fault')
                                rows.append(row)
                                print(json.dumps(row), flush=True)
                                if not row['passed']:
                                    raise AssertionError('route fault did not produce the required rejection')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return {'requests': rows, 'passed': all(x['passed'] for x in rows), 'source_sha256': source_hashes,
            'scope': 'Both deployed HTTP adapters in an isolated ASGI process; real downstream listener, no ordinary registration mutation'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    result = asyncio.run(capture(json.loads(args.config.read_text())))
    args.out.write_text(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
