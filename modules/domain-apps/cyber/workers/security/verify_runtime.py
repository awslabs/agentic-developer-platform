"""Offline worker compatibility and CVE-2026-19445 regression.

The TLS MemoryBIO case follows CPython's regression added by commit
 d8717ed01717a9641686e6e6f83f0ab8af235e2c (PSF-2.0).
"""
import datetime
import gc
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import weakref

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

assert sys.version_info[:3] == (3, 13, 16)
assert not Path('/usr/lib/python3.13').exists()
manifest = json.loads(Path('/opt/worker-manifest.json').read_text())
for command in ('strings', 'yara', 'binwalk', 'file', 'osslsigncode', 'upx'):
    assert shutil.which(command) == manifest['system_binaries'][command]['path']
receipt = json.loads(Path('/opt/adp-security/binwalk.json').read_text())
for row in receipt['files']:
    assert hashlib.sha256(Path(row['installed']).read_bytes()).hexdigest() == row['sha256']

with tempfile.TemporaryDirectory(prefix='cyber-runtime-') as directory:
    root = Path(directory)
    firmware = root / 'firmware.bin'
    firmware.write_bytes(b'\x00' * 512 + gzip.compress(b'local firmware fixture\n' * 100, mtime=0))
    result = subprocess.run(['binwalk', str(firmware)], capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'HOME': str(root)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert '512' in result.stdout and 'gzip compressed data' in result.stdout, (result.stdout, result.stderr)
    rules = root / 'fixture.yar'
    rules.write_text('rule fixture { strings: $s = "fixture" condition: $s }')
    sample = root / 'sample.txt'
    sample.write_text('local analysis fixture')
    result = subprocess.run(['yara', str(rules), str(sample)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and result.stdout.startswith('fixture '), result.stdout + result.stderr

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    pem = root / 'server.pem'
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    leaf = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    leaf.load_cert_chain(pem)
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE
    calls = []
    def make_server():
        dispatch = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        dispatch.load_cert_chain(pem)
        dispatch.set_ecdh_curve('secp384r1')
        def callback(connection, server_name, original):
            calls.append(server_name)
            connection.context = leaf
        dispatch.sni_callback = callback
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        return dispatch.wrap_bio(incoming, outgoing, server_side=True), incoming, outgoing, weakref.ref(dispatch)
    server, server_in, server_out, original_ref = make_server()
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname='localhost')
    for _ in range(10):
        for connection, outgoing, peer in ((client, client_out, server_in), (server, server_out, client_in)):
            try:
                connection.do_handshake()
            except ssl.SSLWantReadError:
                pass
            if outgoing.pending:
                peer.write(outgoing.read())
    client.do_handshake()
    server.do_handshake()
    gc.collect()
    assert original_ref() is None
    assert calls and calls[0] == 'localhost'
    assert server.context is leaf and client.cipher()
print(json.dumps({'binwalk_payload': 'unchanged', 'firmware_signature': 'passed',
                  'yara': 'passed', 'sni_context_release': 'passed'}))
