import json, os, subprocess, hashlib
from pathlib import Path
assert os.getuid() == 65531
commands = {
 ('eks','describe-cluster'): ['name'],
 ('eks','describe-access-entry'): ['clusterName','principalArn'],
 ('eks','list-associated-access-policies'): ['clusterName','principalArn'],
 ('eks','list-access-entries'): ['clusterName'],
 ('ec2','describe-security-group-rules'): ['SecurityGroupRuleIds','Filters'],
 ('ec2','describe-security-groups'): ['GroupIds','Filters'],
 ('ec2','describe-vpc-endpoints'): ['VpcEndpointIds','Filters'],
 ('iam','simulate-principal-policy'): ['PolicySourceArn','ActionNames'],
 ('sts','get-caller-identity'): [],
 ('sts','assume-role'): ['RoleArn','RoleSessionName'],
 ('s3api','head-object'): ['Bucket','Key'],
 ('ssm','get-parameter'): ['Name','WithDecryption'],
}
results=[]
for binary,version in [('/baseline/aws','2.31.22'),('/candidate/aws','2.37.4')]:
 v=subprocess.run([binary,'--version'],check=True,text=True,capture_output=True,timeout=30).stdout.strip()
 assert 'aws-cli/'+version in v
 passed=[]
 for (service,operation),keys in commands.items():
  r=subprocess.run([binary,'--region','us-east-1','--no-cli-pager',service,operation,'--generate-cli-skeleton','input'],check=True,text=True,capture_output=True,timeout=30)
  data=json.loads(r.stdout)
  assert set(keys)<=set(data), (service,operation,keys)
  passed.append(service+' '+operation)
 bad=subprocess.run([binary,'eks','not-an-operation'],capture_output=True,text=True,timeout=30)
 assert bad.returncode==252 and 'invalid choice' in bad.stderr.lower()
 results.append({'version':v,'launcher_sha256':hashlib.sha256(Path(binary).read_bytes()).hexdigest(),'service_model_input_skeletons':passed,'invalid_command_refused':True})
print(json.dumps({'scope':'offline parser/service-model only; no cloud or credential calls','results':results},indent=2))
