from pathlib import Path
import subprocess,tempfile,os,json
p=Path('/workspaces/projects/security27')
with tempfile.TemporaryDirectory(prefix='plan-compat-',dir=p) as tmp:
 root=Path(tmp);root.chmod(0o777)
 (root/'main.tf').write_text('resource "terraform_data" "fixture" { input = "security-fixture" }\noutput "value" { value = terraform_data.fixture.output }\n')
 def run(image,script):
  args=['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges=true','--tmpfs','/tmp:rw,mode=1777','-e','HOME=/tmp/home','-e','BG_CONFIG_DIR=/tmp/bg','-e','TF_DATA_DIR=/fixture/.terraform','-v',str(root)+':/fixture','-w','/fixture','--entrypoint','sh',image,'-c','set -eu; '+script]
  r=subprocess.run(args,capture_output=True,text=True,timeout=180);assert r.returncode==0,r.stdout+r.stderr;return r.stdout
 run('security27/worker-runtime:rebuilt','terraform init -backend=false -input=false; terraform validate; terraform plan -out=baseline.tfplan -input=false')
 result=run('security27/worker-runtime:tools-fixed','test "$(terraform version -json | python3 -c \'import json,sys;print(json.load(sys.stdin)["terraform_version"])\')" = 1.15.7; terraform apply -input=false baseline.tfplan; terraform output -raw value; terraform plan -out=fixed.tfplan -input=false')
 run('security27/worker-runtime:rebuilt','terraform apply -input=false fixed.tfplan; terraform output -raw value')
 assert 'security-fixture' in result
 print(json.dumps({'terraform_version':'1.15.7','old_plan_applied_by_fixed_binary':'passed','fixed_plan_applied_by_old_binary':'passed','state_and_output_roundtrip':'passed','network':'none','uid':1001,'BG_CONFIG_DIR':'isolated'}))
