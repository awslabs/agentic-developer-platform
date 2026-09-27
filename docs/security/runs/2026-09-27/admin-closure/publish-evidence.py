import json,pathlib,shutil,hashlib,datetime
p=pathlib.Path(__file__).parent;repo=pathlib.Path('/workspaces/projects/security27-closure');out=repo/'docs/security/runs/2026-09-27/admin-closure';out.mkdir(exist_ok=True);read=lambda f:json.loads(f.read_text());write=lambda n,d:(out/n).write_text(json.dumps(d,indent=2)+'\n')
s=read(p/'reconciled-final/summary.json');assert s['conservative']['Critical']==0 and not s['unmapped_observed'] and not s['desired_resolution_errors'];inv=pathlib.Path(s['inventory'])
for f in (p/'reconciled-final').glob('*.json'):shutil.copy2(f,out/f.name)
for n in ['desired-resolutions.json','dependabot-summary.json','probe-acceptance.json','superplane-api-acceptance.json','api-application-preservation.json','api-compat-curl-review.json','final-worker-curl-review.json','keda-cleanup-receipt.json','s3-cleanup-receipt.json','zoekt-http-acceptance.json','access-restored.json','access-restoration-response.json','arc-ci-lifecycle-receipt.json','jwt-reference-repair.json']:
 shutil.copy2(p/n,out/n)
for n in ['workload-images.json','templates.json','coverage-gaps.json','inventory-summary.json','collection-receipt.json','scan-targets.json']:shutil.copy2(inv/n,out/n)
for n in ['keda-actual-webhook-rejection.txt','arc-version-gate-live.log','arc-version-gate-baseline-rejection.txt','api-compat-runtime.jsonl']:shutil.copy2(p/n,out/n)
def pod(x):
 return {'namespace':x['metadata']['namespace'],'name':x['metadata']['name'],'uid':x['metadata']['uid'],'node':x['spec'].get('nodeName'),'phase':x['status'].get('phase'),'containers':[{'name':v['name'],'image':v.get('image'),'imageID':v.get('imageID'),'ready':v.get('ready'),'restartCount':v.get('restartCount'),'state':v.get('state')} for v in x['status'].get('initContainerStatuses',[])+x['status'].get('containerStatuses',[])]}
final=read(p/'final-pods.private.json')['items'];selected=[x for x in final if x['metadata']['namespace'] in ['superplane','keda','arc-systems','mount-s3'] or x['metadata']['name'].startswith(('s3-csi-','zoekt-webserver','authority-probe-gateway'))];write('component-pods.json',[pod(x) for x in selected]);receipt=read(p/'keda-positive-job-receipt.json');write('keda-queue-acceptance.json',{'pod':pod(receipt['pod']),'logs':receipt['logs']})
for stage in ['rollback','final']:
 write('s3-'+stage+'-acceptance.json',[{'job':r['job'],'logs':r['logs'],'pods':[pod(x) for x in r['pods']['items']]} for r in read(p/f's3-{stage}-acceptance.private.json')])
r=read(p/'s3-production-acceptance.private.json');write('s3-production-acceptance.json',{'logs':r['logs'],'pods':[pod(x) for x in r['pods']['items']]});r=read(p/'s3-production-resumed.json');write('s3-production-resumed.json',{k:v for k,v in r.items() if k!='zoekt'});shutil.copy2(p/'s3-final-addon.json',out/'s3-final-addon.json')
reviews=read(p/'reconciled-final/conservative-open-register.json');old=read(repo/'docs/security/runs/2026-09-27/continuation-final/critical-coverage-matrix.json');matrix=[]
for r in old:
 current=[v for v in reviews if v['canonical_id']==r['advisory']];matrix.append({'advisory':r['advisory'],'status':'remaining High' if current else 'absent from current Critical/High register','remaining_critical':sum(v['severity']=='Critical' for v in current),'remaining_high':sum(v['severity']=='High' for v in current)})
write('critical-coverage-matrix.json',matrix);write('critical-closure-summary.json',{'baseline_critical':len(matrix),'no_remaining_critical':sum(r['remaining_critical']==0 for r in matrix),'absent_from_current_ch_register':sum(r['remaining_high']==0 and r['remaining_critical']==0 for r in matrix),'remain_high':sum(r['remaining_high']>0 for r in matrix),'continuation_handoff_critical':32,'final_critical':0})
for n in ['reconcile.py','refresh.py','resolve-desired.py','publish-evidence.py']:shutil.copy2(p/n,out/n)
write('file-hashes.json',{str(f.relative_to(out)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(out.rglob('*')) if f.is_file() and f.name!='file-hashes.json'})
print(json.dumps(read(out/'critical-closure-summary.json')))
