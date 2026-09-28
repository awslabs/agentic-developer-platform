import json,pathlib,subprocess,datetime,os
os.umask(0o077)
p=pathlib.Path('/workspaces/projects/security27-continuation');inv=pathlib.Path('/workspaces/projects/security27/latest-live-refresh.txt').read_text().strip();ts=json.load(open(inv+'/templates.json'));resolved=[]
for ref in sorted({t['requested_image'] for t in ts if t['active_desired_template'] and not t['observed_digests']}):
 row={'requested_image':ref,'workloads':[t['workload'] for t in ts if t['active_desired_template'] and t['requested_image']==ref]}
 if '@sha256:' in ref:row['reference']=ref
 else:
  repo_tag=ref.split('/',1)[1];repo,tag=repo_tag.rsplit(':',1)
  r=subprocess.run(['aws','ecr','describe-images','--region','us-east-1','--repository-name',repo,'--image-ids','imageTag='+tag,'--query','imageDetails[0].imageDigest','--output','text'],capture_output=True,text=True)
  if r.returncode:row['error']=r.stderr.strip()
  else:row['reference']=ref.split('/',1)[0]+'/'+repo+'@'+r.stdout.strip()
 resolved.append(row)
(p/'desired-resolutions.json').write_text(json.dumps({'time':datetime.datetime.now(datetime.timezone.utc).isoformat(),'inventory':inv,'targets':resolved},indent=2)+'\n')
print(json.dumps(resolved,indent=2))
