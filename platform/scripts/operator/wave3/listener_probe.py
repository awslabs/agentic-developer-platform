#!/usr/bin/env python3
"""Run inside the owned worker: real listener auth probes, no credentials emitted.

A prepare call retains a credential only in a private local receipt after its
ping succeeds. Expired probes refuse to run until that actual credential expires
or its observed renewal-overlap window closes. No lease file is modified.
"""
import argparse
import hashlib
import json
import os
import secrets
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

VERBS = {'pause', 'resume', 'steer', 'abort'}

def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None: raise ValueError('token expiry requires timezone')
    return parsed.timestamp()

def private_read(path):
    with Path(path).open() as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError('credential observation must be a private regular file')
        return json.load(stream)

def lease(config):
    doc = private_read(config['lease_path'])
    if (doc.get('version') != 1 or doc.get('run_id') != config['run_id'] or
        doc.get('generation') != config['generation']):
        raise ValueError('listener lease identity differs')
    return doc

def send(config, method, path, token=None, generation=None, body=None):
    # The operator binds this address to the UID-observed fixture pod. Reject
    # redirects and proxy environment variables; this must hit that exact socket.
    import ipaddress
    host = str(ipaddress.IPv4Address(config['pod_ip']))
    if not host.startswith('10.') or host != os.environ.get('POD_IP') or config['port'] != 8770:
        raise ValueError('fixture listener must be the explicit private pod on 8770')
    headers = {'Content-Type': 'application/json'}
    if token is not None: headers['Authorization'] = 'Bearer ' + token
    if generation is not None: headers['X-ADP-Control-Generation'] = str(generation)
    from urllib.request import HTTPRedirectHandler
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    req = Request('http://' + host + ':8770' + path, method=method, headers=headers,
                  data=json.dumps(body).encode() if body is not None else None)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)

def prepare(config, request=send):
    doc = lease(config);current = doc['current']
    if timestamp(current['expires_at']) <= time.time(): raise ValueError('initial credential already expired')
    status, _ = request(config, 'GET', '/agent/ping', current['token'], config['generation'])
    if status != 200: raise ValueError('cannot establish a previously accepted credential')
    receipt = dict(run_id=config['run_id'], generation=config['generation'], token=current['token'],
        expires_at=current['expires_at'], accepted_at=datetime.now(timezone.utc).isoformat(),
        token_sha256=hashlib.sha256(current['token'].encode()).hexdigest())
    # Exclusive creation prevents relabeling a prior acceptance observation.
    fd = os.open(config['receipt_path'], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream: json.dump(receipt, stream)
    return {key:value for key,value in receipt.items() if key != 'token'}

def probe(config, case, verb, command_id, request=send):
    if case not in {'missing','wrong','expired','stale'} or verb not in VERBS or not command_id:
        raise ValueError('explicit supported rejection case, verb and command ID required')
    doc = lease(config);current = doc['current'];generation = config['generation']
    if timestamp(current['expires_at']) <= time.time(): raise ValueError('current lease expired')
    status, _ = request(config, 'GET', '/agent/ping', current['token'], generation)
    if status != 200: raise ValueError('positive listener control failed')
    proof = {}
    if case == 'missing': token = None
    elif case == 'wrong': token = secrets.token_urlsafe(32)
    elif case == 'stale': token = current['token']; generation = config['generation'] - 1
    else:
        saved = private_read(config['receipt_path'])
        if saved['run_id'] != config['run_id'] or saved['generation'] != config['generation']:
            raise ValueError('expired credential receipt belongs to another invocation')
        token = saved['token'];deadline = timestamp(saved['expires_at'])
        digest = hashlib.sha256(token.encode()).hexdigest()
        if saved.get('token_sha256') != digest: raise ValueError('accepted token receipt digest differs')
        rotation_path = config['receipt_path'] + '.rotation.json'
        previous = doc.get('previous', {})
        if previous.get('token') == token:
            staged = timestamp(doc['staged_at']);until = timestamp(previous['valid_until'])
            if until > staged + 30 or staged > time.time(): raise ValueError('invalid renewal overlap')
            if not Path(rotation_path).exists():
                # Preserve the actual observed overlap before the next rotation
                # replaces it. Never rewrite the original acceptance receipt.
                observation = dict(run_id=config['run_id'], generation=config['generation'],
                    token_sha256=digest, staged_at=doc['staged_at'], valid_until=previous['valid_until'],
                    observed_at=datetime.fromtimestamp(time.time(),timezone.utc).isoformat())
                fd = os.open(rotation_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as stream: json.dump(observation, stream)
        if Path(rotation_path).exists():
            rotation = private_read(rotation_path)
            if (rotation.get('run_id') != config['run_id'] or rotation.get('generation') != config['generation'] or
                rotation.get('token_sha256') != digest): raise ValueError('rotation observation identity differs')
            staged = timestamp(rotation['staged_at']);until = timestamp(rotation['valid_until'])
            observed = timestamp(rotation['observed_at'])
            if not staged <= until <= staged + 30 or not staged <= observed <= time.time():
                raise ValueError('invalid observed renewal overlap')
            deadline = min(deadline, until)
            proof['rotation_observation'] = rotation
        if deadline >= time.time(): raise ValueError('previously accepted credential has not expired yet')
        proof.update(accepted_at=saved['accepted_at'], expired_at=datetime.fromtimestamp(deadline,timezone.utc).isoformat(),
                     token_sha256=digest)
    body = {'command_id':command_id}
    if verb == 'steer': body['instruction'] = 'Disposable rejection probe'
    status, response = request(config,'POST','/agent/'+verb,token,generation,body)
    return dict(case=case,verb=verb,command_id=command_id,run_id=config['run_id'],status=status,
                response_body=response,observed_at=datetime.now(timezone.utc).isoformat(),
                observed_by='direct production listener on owned fixture pod',credential_proof=proof)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--case',choices=['prepare','missing','wrong','expired','stale'],required=True)
    parser.add_argument('--verb',choices=sorted(VERBS));parser.add_argument('--command-id')
    args=parser.parse_args();config=json.loads(args.config.read_text())
    result=prepare(config) if args.case=='prepare' else probe(config,args.case,args.verb,args.command_id)
    print(json.dumps(result))
if __name__=='__main__': main()
