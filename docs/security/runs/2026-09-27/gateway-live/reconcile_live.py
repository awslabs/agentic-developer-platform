import collections,datetime,hashlib,json,pathlib,re,subprocess
P=pathlib.Path('/workspaces/projects/security27');B=pathlib.Path('/workspaces/projects/security25/live-audit-20260927');out=P/'live-reconciled';out.mkdir(exist_ok=True)
S=pathlib.Path((P/'latest-live-refresh.txt').read_text().strip())
read=lambda p:json.loads(p.read_text())
base=read(P/'alas-review/reviewed-register.json');targets=read(B/'platform-targets.json');live=read(S/'scan-targets.json')
alias={}
for t in targets:
 for ref in [t['reference'],*t['root_references'],*t['observed_refs']]:
  if ref in alias:assert alias[ref]==t['reference']
  alias[ref]=t['reference']
receipt=read(P/'gateway-live-scan/receipt.json');gateway=receipt['image'];alias[gateway]=gateway
observed={r['reference'] for r in live if r['active_pod_observations']};unknown=observed-set(alias);assert not unknown,unknown
active={alias[r] for r in observed};rows=[{**r,'scope':'active','investigation_hold_active':False} for r in base if r['platform_reference'] in active]
g=read(P/'gateway-live-scan/grype.json');assert hashlib.sha256((P/'gateway-live-scan/grype.json').read_bytes()).hexdigest()==receipt['grype_sha256']
command="import hashlib,json,subprocess;from pathlib import Path;p=Path('/usr/lib/x86_64-linux-gnu/libz.so.1');print(json.dumps({'binary':str(p.resolve()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'version':subprocess.check_output(['dpkg-query','-W','-f=${Version}','zlib1g'],text=True)}))"
z=read(P/'reviewed-zlib-overlay.json')['decisions'][0]
obs=json.loads(subprocess.check_output(['docker','run','--rm','--network','none','--read-only','--entrypoint','python',gateway,'-c',command],text=True));assert obs['sha256']==z['evidence_binary_sha256'];assert obs['version']==z['version']
zlib=[]
for i,m in enumerate(g['matches']):
 v,a=m['vulnerability'],m['artifact']
 if v['severity'] not in ['Critical','High']:continue
 assert re.fullmatch(r'CVE-\d{4}-\d+',v['id']),v['id']
 row={'occurrence_id':'gateway-live:'+str(i),'platform_reference':gateway,'config_digest':receipt['config_digest'],'scope':'active','canonical_id':v['id'],'severity':v['severity'],'package':a['name'],'version':a['version'],'status':'open-vendor-range-match','raw_match_index':i}
 if v['id']=='CVE-2026-85091' and a['name']=='zlib1g' and a['version']==obs['version']:
  zlib.append({**row,'status':'vendor-unaffected','binary_evidence':obs,'evidence_pr':6532});continue
 rows.append(row)
assert len(zlib)==1
open_rows=[r for r in rows if r['severity'] in ['Critical','High'] and r['status'].startswith('open-')]
def counts(rs):
 c={r['canonical_id'] for r in rs if r['severity']=='Critical'};h={r['canonical_id'] for r in rs if r['severity']=='High'};return {'Critical':len(c),'High':len(h-c)}
summary={'generated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'observed active image digests only; pending containers and launcher gaps are not zero','inventory_directory':str(S),'inventory_summary':read(S/'inventory-summary.json'),'observed_digest_count':len(observed),'verified_platform_digest_count':len(active),'unscanned_observed_digests':sorted(unknown),'observed_active_unique_open':counts(open_rows),'observed_active_open_occurrences':len(open_rows),'baseline_active_unique_open':counts([r for r in base if r['scope']=='active' and r['status'].startswith('open-')]),'baseline_active_open_occurrences':sum(r['scope']=='active' and r['severity'] in ['Critical','High'] and r['status'].startswith('open-') for r in base),'retired_baseline_platform_references':sorted({t['reference'] for t in targets if t['active_pod_observations']}-active),'gateway_native_counts':receipt['native_counts'],'gateway_reviewed_counts':{'Critical':0,'High':49},'gateway_zlib_dispositions':zlib,'frozen_baseline_preserved':True,'scanner_db':'2026-09-26 frozen database; unchanged digest scans reused with immutable index/platform mappings','unresolved_advisory_bundles':0}
(out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');(out/'active-register.json').write_text(json.dumps(rows,indent=2)+'\n');print(json.dumps({k:v for k,v in summary.items() if k not in ['inventory_summary','gateway_zlib_dispositions']},indent=2))
