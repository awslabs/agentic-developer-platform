import http.server,json,os,subprocess,threading,time,urllib.request,urllib.error
class Backend(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"ok"}')
 def log_message(self,*args):pass
server=http.server.HTTPServer(('127.0.0.1',46580),Backend)
threading.Thread(target=server.serve_forever,daemon=True).start()
env=dict(os.environ,SKYPILOT_SERVICE_TOKEN='synthetic-security-fixture-token-123456789')
p=subprocess.Popen(['python','-m','app.skypilot_proxy'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
def status(path,auth=False):
 req=urllib.request.Request('http://127.0.0.1:46581/'+path,headers={'Authorization':'Bearer '+env['SKYPILOT_SERVICE_TOKEN']} if auth else {})
 try:
  with urllib.request.urlopen(req,timeout=5) as r:return r.status
 except urllib.error.HTTPError as e:return e.code
try:
 for _ in range(100):
  try:
   if status('api/health')==401:break
  except OSError:pass
  time.sleep(.1)
 assert status('api/health')==401
 assert status('api/health',True)==200
 assert status('users/create',True)==404
 assert subprocess.run(['python','-m','app.skypilot_proxy','--check'],env=env).returncode==0
 server.shutdown();server.server_close()
 assert status('api/health',True)==503
 print(json.dumps({'unauthenticated':401,'authenticated_health':200,'unapproved_route':404,'health_command':0,'backend_outage':503}))
finally:
 p.terminate();p.wait(timeout=10)
