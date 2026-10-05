"""Exercise the portable AWS CLI's XML parser with small local HTTP fixtures."""
import http.server
import json
import os
from pathlib import Path
import subprocess
import threading

adapter = Path('/opt/awscli-adp/lib/aws-cli/python3.14/lib-dynload/pyexpat.cpython-314-x86_64-linux-gnu.so')
assert b'expat_2.8.5' in adapter.read_bytes()
body = b'''<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Buckets><Bucket><Name>local-fixture</Name><CreationDate>2026-01-01T00:00:00.000Z</CreationDate></Bucket></Buckets></ListAllMyBucketsResult>'''
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/xml')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    env = dict(os.environ, AWS_EC2_METADATA_DISABLED='true', AWS_MAX_ATTEMPTS='1', AWS_PAGER='')
    command = ['aws', 's3api', 'list-buckets', '--endpoint-url', f'http://127.0.0.1:{server.server_port}',
               '--no-sign-request', '--region', 'us-east-1', '--output', 'json',
               '--cli-connect-timeout', '2', '--cli-read-timeout', '2']
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(result.stdout)['Buckets'][0]['Name'] == 'local-fixture'
    body = b'<root></other>'
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode in (252, 254, 255), result
    print(json.dumps({'expat': '2.8.5', 'awscli_valid_xml': 'passed', 'awscli_malformed_xml_rejected': 'passed',
                      'network': 'local HTTP fixture only', 'maximum_fixture_bytes': 256}))
finally:
    server.shutdown()
