import collections,datetime,hashlib,json,pathlib,re,subprocess
P=pathlib.Path('/workspaces/projects/security27');B=pathlib.Path('/workspaces/projects/security25/live-audit-20260927');S=pathlib.Path((P/'latest-live-refresh.txt').read_text().strip());O=P/'final-live-reconciled';O.mkdir(exist_ok=True)
read=lambda p:json.loads(p.read_text())
base=read(P/'alas-review/reviewed-register.json');targets=read(B/'platform-targets.json');alias={}
for t in targets:
 for ref in [t['reference'],*t['root_references'],*t['observed_refs']]:alias[ref]=t['reference']
new={'arc-runner':('live-final-arc-runner-scan','adp-arc-runner'),'chat':('live-final-chat-scan','adp-chat-agent'),'gateway':('live-final-gateway-scan','adp-gateway'),'context-mcp':('context-mcp-fixed-scan','adp-dev-agent-context-context-mcp'),'litellm':('litellm-scan','adp-dev-agent-context-litellm-proxy')}
registry='000000000101.dkr.ecr.us-east-1.amazonaws.com';added={};receipts={}
for name,(directory,repo) in new.items():
 r=read(P/directory/'receipt.json');ref=registry+'/'+repo+'@'+r['docker_root_descriptor'];alias[ref]=ref;added[ref]=(name,directory,r);receipts[name]=r
observed={x['reference'] for x in read(S/'scan-targets.json') if x['active_pod_observations']};missing=observed-set(alias);assert not missing,missing
active={alias[r] for r in observed};rows=[{**r,'scope':'active','investigation_hold_active':False} for r in base if r['platform_reference'] in active];dispositions=[]
def canonical(m):
 ids={m['vulnerability']['id']}|{v['id'] for v in m.get('relatedVulnerabilities',[])}
 for i in list(ids):
  f=B/'vendor'/(i+'.json')
  if f.exists():
   v=read(f);ids.update(v.get('aliases',[]));ids.update(x['value'] for x in v.get('identifiers',[]))
 cves=sorted(i for i in ids if i.startswith('CVE-'));assert len(cves)<=1,(m['vulnerability']['id'],cves)
 return cves[0] if cves else m['vulnerability']['id']
for ref,(name,directory,r) in added.items():
 if ref not in active:continue
 f=P/directory/'grype.json';assert hashlib.sha256(f.read_bytes()).hexdigest()==r['grype_sha256'];assert hashlib.sha256((P/directory/'syft.json').read_bytes()).hexdigest()==r['sbom_sha256'];g=read(f)
 z=[m for m in g['matches'] if m['artifact']['name']=='zlib1g' and m['artifact']['version']=='1:1.3.dfsg+really1.3.1-1+b1' and m['vulnerability']['id']=='CVE-2026-85091'];obs=None
 if z:
  code="import hashlib,json;from pathlib import Path;p=Path('/usr/lib/x86_64-linux-gnu/libz.so.1');print(json.dumps({'binary':str(p.resolve()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))"
  obs=json.loads(subprocess.check_output(['docker','run','--rm','--network','none','--read-only','--entrypoint','python',r['image'],'-c',code],text=True));assert obs['sha256']=='85590dd58edf5445e18bc7193e5ebc01ac5841f1ae187e97705a662e90c6421e'
 for i,m in enumerate(g['matches']):
  a,v=m['artifact'],m['vulnerability']
  if v['severity'] not in ['Critical','High']:continue
  row={'occurrence_id':name+':'+str(i),'platform_reference':ref,'config_digest':r['config_digest'],'scope':'active','canonical_id':canonical(m),'native_id':v['id'],'severity':v['severity'],'package':a['name'],'version':a['version'],'status':'open-scanner-match-retained','raw_match_index':i,'raw_scan_sha256':r['grype_sha256']}
  if obs and m in z:dispositions.append({**row,'status':'vendor-unaffected','binary_evidence':obs,'evidence_pr':6532});continue
  rows.append(row)
opened=[r for r in rows if r['severity'] in ['Critical','High'] and r['status'].startswith('open-')]
def counts(rs):
 c={r['canonical_id'] for r in rs if r['severity']=='Critical'};h={r['canonical_id'] for r in rs if r['severity']=='High'};return {'Critical':len(c),'High':len(h-c)}
old=[r for r in base if r['scope']=='active' and r['severity'] in ['Critical','High'] and r['status'].startswith('open-')]
summary={'generated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'observed active EKS image digests; pending/runtime/launcher gaps remain unknown','inventory_summary':read(S/'inventory-summary.json'),'observed_digests':len(observed),'unscanned_observed_digests':sorted(missing),'active_unique_open':counts(opened),'baseline_active_unique_open':counts(old),'active_open_occurrences':len(opened),'baseline_active_open_occurrences':len(old),'retired_baseline_platform_references':sorted({t['reference'] for t in targets if t['active_pod_observations']}-active),'current_new_images':{name:{'reference':ref,'native_counts':r['native_counts'],'config_digest':r['config_digest']} for ref,(name,_,r) in added.items() if ref in active},'rebound_zlib_unaffected_occurrences':dispositions,'frozen_baseline_preserved':True,'unresolved_advisory_bundles':0,'original_all_scope_baseline':{'Critical':64,'High':425,'open_occurrences':4123}}
# Keep prior findings open for a desired workload that currently lacks a
# verified runtime digest. Disappearance during a failed/pending rollout is
# not remediation. The only such retired baseline image in this snapshot is
# DeepWiki; all other retired references have independently scanned replacements.
carried_ref='ghcr.io/asyncfuncai/deepwiki-open@sha256:1f24e9aba56305f3104bf9e7f19d12ed93817db77a025d7f38d7829a9ecfaf4c'
carried=[]
if carried_ref in summary['retired_baseline_platform_references']:
 templates=read(S/'templates.json')
 desired=[t for t in templates if t['workload']['namespace']=='agent-context' and t['workload']['name']=='deepwiki' and t['active_desired_template']]
 assert desired and not any(t['observed_digests'] for t in desired)
 carried=[{**r,'current_exposure_verification':'retained pending current DeepWiki imageID; not claimed currently running'} for r in base if r['platform_reference']==carried_ref and r['severity'] in ['Critical','High'] and r['status'].startswith('open-')]
summary['conservative_open_unique']=counts(opened+carried)
summary['conservative_open_occurrences']=len(opened)+len(carried)
summary['carried_forward_unverified_occurrences']=len(carried)
summary['carried_forward_reason']='DeepWiki remains desired but has no verified current runtime imageID; previous findings remain open'
(O/'conservative-open-register.json').write_text(json.dumps(rows+carried,indent=2)+'\n')
(O/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');(O/'active-register.json').write_text(json.dumps(rows,indent=2)+'\n');(O/'scan-receipts.json').write_text(json.dumps(receipts,indent=2)+'\n');print(json.dumps({k:v for k,v in summary.items() if k not in ['inventory_summary','rebound_zlib_unaffected_occurrences']},indent=2))
