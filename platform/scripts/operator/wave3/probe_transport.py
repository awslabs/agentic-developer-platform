#!/usr/bin/env python3
"""Exercise deployed shared transport with real HTTP and a contained destination.

Run inside the owned gateway pod. No database records or process-global gateway
configuration are changed. Invalid destinations must be rejected before httpx;
a containment transport counts and refuses any unexpected outgoing request.
"""
import argparse
import asyncio
import hashlib
import inspect
import json
import os
import threading
import uuid
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import httpx
from src.activity.control_service import ControlService,ControlTarget,ControlError,CLUSTER_CIDRS_ENV

async def capture(config):
    host=config['pod_ip']
    if host != os.environ.get('POD_IP'):raise ValueError('probe must run on the named gateway pod')
    source=Path(inspect.getfile(ControlService));source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    if source_hash != config['control_service_sha256']:raise ValueError('deployed transport source differs')
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            received.append(self.path)
            self.send_response(307)
            self.send_header('Location','http://169.254.169.254/latest/meta-data/')
            self.send_header('Content-Length','0');self.end_headers()
        def log_message(self,*args):pass
    server=ThreadingHTTPServer((host,8770),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    attempts=[]
    class ContainedTransport(httpx.AsyncBaseTransport):
        def __init__(self):self.inner=httpx.AsyncHTTPTransport()
        async def handle_async_request(self,request):
            attempts.append(str(request.url))
            if request.url.host != host or request.url.port != 8770:
                raise RuntimeError('forbidden destination reached transport boundary')
            return await self.inner.handle_async_request(request)
        async def aclose(self):await self.inner.aclose()
    rows=[]
    try:
        async with httpx.AsyncClient(transport=ContainedTransport(),trust_env=False) as client:
            service=ControlService(table=object(),http_client=client,env={CLUSTER_CIDRS_ENV:host+'/32'})
            for verb in ('pause','resume','steer','abort'):
                targets={'unregistered_ip':('10.255.255.254' if host!='10.255.255.254' else '10.255.255.253',8770),
                    'wrong_port':(host,8771),'metadata':('169.254.169.254',8770),
                    'link_local':('169.254.10.10',8770),'loopback':('127.0.0.1',8770),
                    'public':('8.8.8.8',8770),'redirect':(host,8770)}
                for family,(address,port) in targets.items():
                    target=ControlTarget(run_id=config['run_id'],arrived_at='fixture-transport-probe',status='running',address=address,port=port,
                        token='disposable-probe-no-authority',generation=1,token_expires_at=None)
                    body={'command_id':str(uuid.uuid4())}
                    if verb=='steer':body['instruction']='Disposable transport probe'
                    start=len(attempts);received_before=len(received);status=None
                    try:
                        await service.forward_command(target,verb,request_body=json.dumps(body).encode(),envelope='disposable-not-authorized')
                    except ControlError as error:status=error.status_code
                    except RuntimeError:status='containment_refused'
                    observed=attempts[start:]
                    forbidden=observed[1:] if family=='redirect' else observed
                    blocked=(status==502 and len(observed)==1 and len(received)==received_before+1) if family=='redirect' else (status==409 and not observed)
                    rows.append(dict(verb=verb,family=family,blocked=blocked,status=status,
                        transport_attempts=len(forbidden),observed_requests=observed,
                        run_id=config['run_id'],observed_at=datetime.now(timezone.utc).isoformat()))
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
    return dict(shared_transport_probes=rows,passed=all(r['blocked'] for r in rows),
        control_service_sha256=source_hash,received_requests=received,
        scope='Shared deployed service transport with synthetic targets and real local HTTP; not gateway API authorization or full wave acceptance')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,required=True);args=p.parse_args()
    result=asyncio.run(capture(json.loads(args.config.read_text())));print(json.dumps(result))
    raise SystemExit(0 if result['passed'] else 1)
if __name__=='__main__':main()
