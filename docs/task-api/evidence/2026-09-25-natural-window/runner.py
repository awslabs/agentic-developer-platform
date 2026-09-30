import importlib.util,json,time,datetime,pathlib,traceback
root=pathlib.Path('/home/ubuntu/task-delivery-tmp/live-run/clarify3/window')
root.mkdir(exist_ok=True)
def raw_frames(response,deadline):
 frame=[]
 while time.monotonic()<deadline:
  line=response.readline(65537)
  if not line:return
  text=line.decode().rstrip('\r\n')
  if text:frame.append(text)
  elif frame:
   yield {'event':'heartbeat' if frame[0].startswith(':') else 'frame','wire':frame}
   frame=[]
spec=importlib.util.spec_from_file_location('client','/home/ubuntu/task-delivery/release/examples/task-api/client.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
c=m.Client('https://gateway-1.example.com/dev',json.loads(pathlib.Path('/home/ubuntu/task-delivery-tmp/isolation/isolated-token.json').read_text())['access_token'])
task='tsk_37ce236b-19a2-4d7c-a36d-b3609160b999';cursor=task+':14'
out={'task_id':task,'cursor':cursor,'lane':'completed-task-natural-connection-window','writes_performed':0,'connections':[]}
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
try:
 for i in range(2):
  start=time.monotonic(); rec={'started_at':now(),'last_event_id':cursor,'frames':0,'heartbeats':0};out['connections'].append(rec)
  with c.open('GET',f'/v1/tasks/{task}/events',headers={'Accept':'text/event-stream','Last-Event-ID':cursor},timeout=40) as response:
   rec['http_status']=response.status
   with (root/f'connection-{i+1}.ndjson').open('w') as f:
    for event in raw_frames(response,time.monotonic()+660):
     f.write(json.dumps({'received_at':now(),'elapsed_seconds':time.monotonic()-start,'event':event})+'\n');f.flush();rec['frames']+=1
     if event.get('event')=='heartbeat':rec['heartbeats']+=1
     if i==1 and rec['frames']>=2:rec['close']='observer-after-snapshot-and-heartbeat';break
    else:rec['close']='natural-eof'
  rec['ended_at']=now();rec['elapsed_seconds']=time.monotonic()-start
  (root/'result.json').write_text(json.dumps(out,indent=2)+'\n')
 out['criterion_outcome']='PASS' if out['connections'][0]['close']=='natural-eof' and 595<=out['connections'][0]['elapsed_seconds']<=625 and out['connections'][0]['heartbeats']>=35 and out['connections'][1]['frames']>=2 else 'FAIL'
except Exception as e:
 out['error_type']=type(e).__name__;out['error']=str(e);out['criterion_outcome']='FAIL'
(root/'result.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out),flush=True)
