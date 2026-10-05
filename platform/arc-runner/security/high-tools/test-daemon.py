"""Run locally: python3 test-daemon.py IMAGE EVIDENCE_DIRECTORY. Requires privileged Docker."""
import subprocess,time,json,pathlib,sys,io,tarfile,os
image=sys.argv[1];name='adp-runner-security-test-'+str(os.getpid());p=pathlib.Path(sys.argv[2]);p.mkdir(parents=True,exist_ok=True)
def call(args,**kw):return subprocess.run(args,check=True,text=True,capture_output=True,**kw)
started=False
try:
 call(['docker','run','-d','--name',name,'--privileged','--user','root','--entrypoint','dockerd',image,'--host=unix:///tmp/docker.sock','--data-root=/tmp/adp-docker','--exec-root=/tmp/adp-exec','--storage-driver=vfs','--iptables=false','--ip6tables=false','--bridge=none'])
 started=True
 docker=['docker','exec',name,'docker','-H','unix:///tmp/docker.sock']
 for _ in range(45):
  r=subprocess.run([*docker,'info','--format','{{json .}}'],text=True,capture_output=True)
  if r.returncode==0:break
  time.sleep(1)
 else:raise RuntimeError('nested daemon did not become ready: '+r.stderr)
 info=json.loads(r.stdout)
 assert info['ServerVersion']=='29.8.1',info['ServerVersion']
 # Import a local pre-pulled Alpine image; no daemon registry credentials/network.
 archive=p/'alpine-runtime.tar'
 call(['docker','save','-o',str(archive),'public.ecr.aws/docker/library/alpine:3.23'])
 with archive.open('rb') as f:subprocess.run(['docker','exec','-i',name,'docker','-H','unix:///tmp/docker.sock','load'],stdin=f,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
 result=call([*docker,'run','--rm','--network=none','public.ecr.aws/docker/library/alpine:3.23','sh','-c','test "$(id -u)" = 0; echo container-runtime-ok'])
 assert result.stdout.strip()=='container-runtime-ok'
 # Run a Dockerfile RUN instruction, exercising Buildx, BuildKit and runc.
 payload=io.BytesIO()
 with tarfile.open(fileobj=payload,mode='w') as tf:
  data=b'FROM public.ecr.aws/docker/library/alpine:3.23\nRUN printf "build-runtime-ok\\n" > /marker\nCMD ["cat", "/marker"]\n';item=tarfile.TarInfo('Dockerfile');item.size=len(data);tf.addfile(item,io.BytesIO(data))
 result=subprocess.run(['docker','exec','-i',name,'docker','-H','unix:///tmp/docker.sock','buildx','build','--network=none','--load','-t','adp-runtime:test','-'],input=payload.getvalue(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
 (p/'docker-buildx-integration.log').write_bytes(result.stdout+result.stderr)
 result=call([*docker,'run','--rm','--network=none','adp-runtime:test'])
 assert result.stdout.strip()=='build-runtime-ok'
 print(json.dumps({'image':image,'daemon':info['ServerVersion'],'containerd':info.get('ContainerdCommit'), 'runc':info.get('RuncCommit'),'container_run':'passed','buildx_run_instruction':'passed'}))
finally:
 if started:
  logs=subprocess.run(['docker','logs',name],text=True,capture_output=True);(p/'nested-daemon.log').write_text(logs.stdout+logs.stderr)
  subprocess.run(['docker','stop','-t','5',name],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  subprocess.run(['docker','rm',name],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
