import argparse,collections,hashlib,json,pathlib,shutil,datetime
ap=argparse.ArgumentParser();ap.add_argument('--reconciled',type=pathlib.Path,required=True);ap.add_argument('--out',type=pathlib.Path,required=True);a=ap.parse_args();a.out.mkdir(parents=True,exist_ok=True)
p=pathlib.Path(__file__).parent;b=pathlib.Path('/workspaces/projects/security25/live-audit-20260927');read=lambda f:json.loads(f.read_text());summary=read(a.reconciled/'summary.json');inv=pathlib.Path(summary['inventory']);rows=read(a.reconciled/'conservative-open-register.json');workloads=read(inv/'workload-images.json');aliases={}
for t in read(b/'platform-targets.json'):
 for ref in [t['reference'],*t['root_references'],*t['observed_refs']]:aliases[ref]=t['reference']
by_ref=collections.defaultdict(set)
for w in workloads:
 if w['active_pod'] and w.get('digest_reference'):
  k=w['workload'];by_ref[aliases.get(w['digest_reference'],w['digest_reference'])].add('/'.join([k['namespace'],k['kind'],k['name']]))
for t in read(p/'desired-resolutions.json')['targets']:
 if 'reference' in t:
  for w in t['workloads']:by_ref[aliases.get(t['reference'],t['reference'])].add('/'.join([w['namespace'],w['kind'],w['name']]))
for row in rows:row['current_workloads']=sorted(by_ref[row['platform_reference']])
critical=[r for r in rows if r['severity']=='Critical'];matrix=read(p/'critical-coverage-matrix.json');all_ids={r['canonical_id'] for r in rows}
for entry in matrix:
 current=[r for r in rows if r['canonical_id']==entry['advisory']];entry['current_occurrences']=current;entry['status']=('open Critical' if any(r['severity']=='Critical' for r in current) else 'open High; Critical occurrences removed') if current else 'closed in reconciled image/package scope';entry['remaining_workloads']=sorted({w for r in current for w in r['current_workloads']})
 for old in entry['occurrences']:
  old['previous_digest_observed']=bool(by_ref[old['affected_image']]);old['deployment_status']='See current_occurrences and rollout receipts; candidate evidence is not a live deduction';old['live_closure']=not bool(current)
source=read(p/'dependabot-final.json');source_ch=[{'alert_number':r['number'],'severity':r['security_advisory']['severity'],'cve_id':r['security_advisory']['cve_id'],'ghsa_id':r['security_advisory']['ghsa_id'],'package':r['dependency']['package'],'manifest':r['dependency']['manifest_path'],'overlaps_image_register':r['security_advisory']['cve_id'] in all_ids or r['security_advisory']['ghsa_id'] in all_ids} for r in source if r['security_advisory']['severity'] in ['critical','high']]
summary['dependabot_open_severity_counts']=dict(collections.Counter(r['security_advisory']['severity'] for r in source));summary['dependabot_critical_high']=source_ch;summary['baseline_critical_advisories_closed']=sorted(e['advisory'] for e in matrix if not e['current_occurrences']);summary['baseline_critical_advisories_open']=sorted(e['advisory'] for e in matrix if e['current_occurrences']);summary['additional_ingestion_criticals']={'advisories':['CVE-2026-27820','CVE-2026-42257','CVE-2026-6653'],'still_open':[i for i in ['CVE-2026-27820','CVE-2026-42257','CVE-2026-6653'] if i in all_ids]};summary['objective_complete']=False;summary['baseline_advisories_with_critical_occurrences_removed_but_high_remaining']=sorted(e['advisory'] for e in matrix if e['current_occurrences'] and not any(r['severity']=='Critical' for r in e['current_occurrences']))
for name,data in [('summary',summary),('critical-coverage-matrix',matrix),('conservative-open-register',rows)]: (a.out/(name+'.json')).write_text(json.dumps(data,indent=2)+'\n')
lines=['# Critical remediation coverage','',f"{len(matrix)} baseline advisories. {len(summary['baseline_critical_advisories_closed'])} absent from the current conservative image/package register; {len(summary['baseline_critical_advisories_open'])} remain open. Candidate scans are not automatically deducted.",'','| Advisory | State | Remaining workloads |','|---|---|---|']
for e in matrix:lines.append('| '+e['advisory']+' | '+e['status']+' | '+('<br>'.join(e['remaining_workloads']) or 'No remaining matched occurrence')+' |')
(a.out/'critical-coverage-matrix.md').write_text('\n'.join(lines)+'\n')
for name in ['scan-bindings.json','dispositions.json','missing-scans.json']:shutil.copyfile(a.reconciled/name,a.out/name)
for name in ['inventory-summary.json','workload-images.json','templates.json','scan-targets.json','coverage-gaps.json']:
 if (inv/name).exists():shutil.copyfile(inv/name,a.out/name)
for name in ['desired-resolutions.json','dependabot-final.json','cloudwatch-final.json','cloudwatch-recovery-receipt.json','cloudwatch-recovery-errors.json','cloudwatch-role-restoration.json','cloudwatch-credential-rollout.json','cloudwatch-credential-rollout-update.json','ingestion-publication.json','ingestion-combined-review.json','legacy-gateway-publication.json','legacy-kubernetes-receipt.json','legacy-live-receipt.json','worker-normal-publication.json','worker-normal-curl-review.json']:
 if (p/name).exists():shutil.copyfile(p/name,a.out/name)
shutil.copyfile(b/'db/6/import.json',a.out/'frozen-db-import.json');shutil.copyfile(p/'reconciliation-baseline-check/summary.json',a.out/'baseline-reproduction.json')
files={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in a.out.iterdir() if f.is_file() and f.name!='file-hashes.json'};(a.out/'file-hashes.json').write_text(json.dumps(files,indent=2)+'\n');print(json.dumps({'counts':summary['conservative'],'closed_baseline_critical':len(summary['baseline_critical_advisories_closed']),'source_critical':summary['dependabot_open_severity_counts'].get('critical',0)}))
