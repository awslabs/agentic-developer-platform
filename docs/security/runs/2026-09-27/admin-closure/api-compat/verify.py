import hashlib,pathlib,json,subprocess,os,ssl,http.server,threading,tempfile
root=pathlib.Path('/app');h=hashlib.sha256()
for f in sorted(root.rglob('*')):
 if f.is_file():h.update(str(f.relative_to(root)).encode()+b'\0'+f.read_bytes())
print(json.dumps({'application_tree_sha256':h.hexdigest(),'uid':os.getuid()}),flush=True)
with tempfile.TemporaryDirectory() as d:
 p=pathlib.Path(d);subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(p/'key'),' -out'.strip(),str(p/'cert'),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 class Handler(http.server.BaseHTTPRequestHandler):
  def do_GET(self):self.send_response(200);self.send_header('Content-Length',str(len(b'critical-closure-tls')));self.end_headers();self.wfile.write(b'critical-closure-tls')
  def log_message(self,*args):pass
 server=http.server.HTTPServer(('127.0.0.1',0),Handler);ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(p/'cert',p/'key');server.socket=ctx.wrap_socket(server.socket,server_side=True);threading.Thread(target=server.serve_forever,daemon=True).start();port=server.server_port
 cases=[(['--cacert',str(p/'cert'),f'https://localhost:{port}'],0),([f'https://localhost:{port}'],60),(['--cacert',str(p/'cert'),f'https://127.0.0.1:{port}'],60)]
 for args,expected in cases:
  r=subprocess.run(['curl','-sS','--max-time','5',*args],capture_output=True);assert r.returncode==expected,(r.returncode,expected)
 server.shutdown();print(json.dumps({'trusted_tls':'pass','untrusted_tls_refused':True,'wrong_hostname_refused':True}))
