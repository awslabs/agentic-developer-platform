import argparse,collections,datetime,hashlib,json,pathlib,subprocess
ap=argparse.ArgumentParser();ap.add_argument('--inventory',required=True,type=pathlib.Path);ap.add_argument('--out',required=True,type=pathlib.Path);ap.add_argument('--desired',type=pathlib.Path);a=ap.parse_args();a.out.mkdir(parents=True,exist_ok=True)
P=pathlib.Path('/workspaces/projects/security27-continuation');ADMIN=pathlib.Path('/workspaces/projects/security27-admin-closure');OLD=pathlib.Path('/workspaces/projects/security27');B=pathlib.Path('/workspaces/projects/security25/live-audit-20260927');read=lambda p:json.loads(p.read_text())
base=read(OLD/'alas-review/reviewed-register.json');prior=read(OLD/'final-live-reconciled/conservative-open-register.json');targets=read(B/'platform-targets.json');aliases={}
for t in targets:
 for ref in [t['reference'],*t['root_references'],*t['observed_refs']]:aliases[ref]=t['reference']
scans={};receipt_paths={}
for root in [OLD,P,ADMIN]:
 for path in root.glob('*scan/receipt.json'):
  r=read(path);key=r.get('docker_root_descriptor',r.get('platform_digest'))
  if key:scans[key]=(path.parent,r);receipt_paths[key]=str(path)
# Extra registry scan transport has a verified index->platform->config binding.
r=read(P/'cloudwatch-registry-scan/receipt.json');scans['sha256:9021478d1858b0e235291992ea928cd1864faad453321337c834170cb3889fdb']=(P/'cloudwatch-registry-scan',r)
for root in [OLD,P,ADMIN]:
 for path in root.glob('*publication*.json'):
  ds=read(path);ds=ds if isinstance(ds,list) else [ds]
  for d in ds:
   ref=d.get('reference','');key=ref.split('@')[-1]
   if key in scans:
    for k in ['platform_digest','amd64_manifest_digest']:
     if d.get(k):scans[d[k]]=scans[key]
observations=read(a.inventory/'workload-images.json');templates=read(a.inventory/'templates.json');observed={r['reference'] for r in read(a.inventory/'scan-targets.json') if r['active_pod_observations']};active_observed=set(observed);desired_refs=set();desired_errors=[]
if a.desired:
 desired=read(a.desired);assert desired['inventory']==str(a.inventory)
 desired_refs={t['reference'] for t in desired['targets'] if 'reference' in t};desired_errors=[t for t in desired['targets'] if 'error' in t];observed|=desired_refs
missing=[r for r in observed if r not in aliases and r.split('@')[-1] not in scans]
(a.out/'missing-scans.json').write_text(json.dumps(sorted(missing),indent=2)+'\n')
if missing:raise SystemExit('Unmapped observed digests: '+str(len(missing)))
active={aliases.get(r,r) for r in observed};rows=[{**r,'scope':'active' if r['platform_reference'] in {aliases.get(v,v) for v in active_observed} else 'desired-registry-resolution'} for r in base if r['platform_reference'] in active and r['severity'] in ['Critical','High'] and r['status'].startswith('open-')];dispositions=[];bindings={}
def canonical(m):
 ids={m['vulnerability']['id']}|{v['id'] for v in m.get('relatedVulnerabilities',[])}
 for i in list(ids):
  f=B/'vendor'/(i+'.json')
  if f.exists():
   v=read(f);ids.update(v.get('aliases',[]));ids.update(x['value'] for x in v.get('identifiers',[]))
 cves=sorted(i for i in ids if i.startswith('CVE-'))
 return cves or [m['vulnerability']['id']]
for ref in sorted(observed):
 if ref in aliases:continue
 directory,r=scans[ref.split('@')[-1]];g=read(directory/'grype.json');assert hashlib.sha256((directory/'grype.json').read_bytes()).hexdigest()==r['grype_sha256'];assert hashlib.sha256((directory/'syft.json').read_bytes()).hexdigest()==r['sbom_sha256'];bindings[ref]={'receipt':r,'directory':str(directory)}
 approved={}
 for reviewpath in [*P.glob('*review.json'),*ADMIN.glob('*review.json')]:
  review=read(reviewpath)
  if review.get('config_digest')!=r['config_digest'] or review.get('raw_receipt',{}).get('grype_sha256')!=r['grype_sha256']:continue
  for d in review.get('dispositions',[]):approved[d['native_match_index']]={'review':str(reviewpath),'disposition':d}
 # Exact zlib vendor-unaffected package and bytes; never version-only clearance.
 zlib=[i for i,m in enumerate(g['matches']) if m['artifact']['name']=='zlib1g' and m['artifact']['version']=='1:1.3.dfsg+really1.3.1-1+b1' and m['vulnerability']['id']=='CVE-2026-85091']
 if zlib:
  script="import hashlib;from pathlib import Path;print(hashlib.sha256(Path('/usr/lib/x86_64-linux-gnu/libz.so.1.3.1').read_bytes()).hexdigest())"
  proc=subprocess.run(['docker','run','--rm','--network','none','--read-only','--entrypoint','python',r['image'],'-c',script],text=True,capture_output=True)
  if proc.returncode==0 and proc.stdout.strip()=='85590dd58edf5445e18bc7193e5ebc01ac5841f1ae187e97705a662e90c6421e':
   for i in zlib:approved[i]={'evidence_pr':6532,'binary_sha256':proc.stdout.strip(),'status':'vendor-unaffected'}
 for i,m in enumerate(g['matches']):
  v,pkg=m['vulnerability'],m['artifact']
  if v['severity'] not in ['Critical','High']:continue
  for cid in canonical(m):
   row={'occurrence_id':ref.split('@')[-1]+':'+str(i)+':'+cid,'platform_reference':ref,'config_digest':r['config_digest'],'scope':'active' if ref in active_observed else 'desired-registry-resolution','canonical_id':cid,'native_id':v['id'],'severity':v['severity'],'package':pkg['name'],'version':pkg['version'],'raw_scan_sha256':r['grype_sha256'],'raw_match_index':i,'status':'open-scanner-match-retained'}
   if i in approved:dispositions.append({**row,'review':approved[i]});continue
   rows.append(row)
# Carry prior desired-workload findings when no current runtime observation can
# establish its replacement. Do not equate a missing imageID with retirement.
old_workloads=read(B/'workload-images.json');prior_workloads=read(OLD/'live-refresh-20260927T105050Z/workload-images.json');unresolved=[];carry=[]
def key(w):return w['namespace'],w['kind'],w['name']
for row in prior:
 if row['platform_reference'] in active:continue
 owners={key(old_workloads[i]['workload']) for i in row.get('workload_observation_indices',[]) if i<len(old_workloads)}
 owners|={key(w['workload']) for w in prior_workloads if aliases.get(w.get('digest_reference'),w.get('digest_reference'))==row['platform_reference']}
 gaps=[t for t in templates if t['active_desired_template'] and key(t['workload']) in owners and not t['observed_digests']]
 if gaps:
  carry.append({**row,'scope':'carried-desired-unverified'});unresolved.extend('/'.join(key(t['workload'])) for t in gaps)
seen=set();combined=[]
for r in rows+carry:
 k=(r['platform_reference'],r['canonical_id'],r['package'],r['version'],r.get('raw_match_index',r['occurrence_id']))
 if k not in seen:seen.add(k);combined.append(r)
def counts(rs):
 c={r['canonical_id'] for r in rs if r['severity']=='Critical'};h={r['canonical_id'] for r in rs if r['severity']=='High'};return {'Critical':len(c),'High':len(h-c),'occurrences':len(rs)}
summary={'time':datetime.datetime.now(datetime.timezone.utc).isoformat(),'inventory':str(a.inventory),'observed':counts([r for r in rows if r['scope']=='active']),'conservative':counts(combined),'desired_resolution_errors':desired_errors,'desired_resolutions':str(a.desired) if a.desired else None,'carried_occurrences':len(carry),'carried_workloads':sorted(set(unresolved)),'inventory_coverage':read(a.inventory/'inventory-summary.json'),'unmapped_observed':missing,'closure_limit':'Coverage gaps remain explicit. This is image/package scope; source findings are separately reconciled.','candidate_fixes_not_automatically_deducted':True}
for name,data in [('summary',summary),('conservative-open-register',combined),('dispositions',dispositions),('scan-bindings',bindings)]: (a.out/(name+'.json')).write_text(json.dumps(data,indent=2)+'\n')
print(json.dumps({k:summary[k] for k in ['observed','conservative','carried_occurrences','carried_workloads']}))
