import json, pathlib, subprocess, time, urllib.request
root = pathlib.Path(__file__).resolve().parent
old = '000000000101.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-zoekt@sha256:713fa47caf137bc06798dbc019a46be1af0f7ea5b2240a39bce65d2d97bbba66'
new = 'security27/zoekt:high-fixed'
binary = root.parent / 'build/bin/zoekt-git-index'
with (root/'new-index.log').open('w') as log:
 subprocess.run([str(binary), '-index', str(root/'new-index'), '-require_ctags=false', str(root/'repo')], check=True, stdout=log, stderr=log)
results=[]
for label, image, shard_dir, port in [('old_shard_new_server',new,'old-index',16071),('new_shard_old_server',old,'new-index',16072),('new_shard_new_server',new,'new-index',16073)]:
 name='security27-zoekt-'+label
 subprocess.run(['docker','run','-d','--name',name,'-p',f'127.0.0.1:{port}:6070','-v',f'{root/shard_dir}:/data/index:ro','--entrypoint','zoekt-webserver',image,'-index','/data/index','-listen',':6070','-rpc'],check=True,capture_output=True)
 try:
  for attempt in range(30):
   try:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/search?q=zoektHighClosure20260927&format=json',timeout=3) as r: body=r.read().decode()
    if 'fixture.go' in body and 'zoektHighClosure20260927' in body: break
   except Exception: pass
   time.sleep(1)
  else: raise AssertionError(label+' search failed')
  (root/(label+'.json')).write_text(body)
  results.append({'case':label,'passed':True,'image':image})
 finally:
  log=subprocess.run(['docker','logs',name],capture_output=True,text=True)
  (root/(label+'.log')).write_text(log.stdout+log.stderr)
  subprocess.run(['docker','rm','-f',name],check=True,capture_output=True)
(root/'receipt.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps(results))
