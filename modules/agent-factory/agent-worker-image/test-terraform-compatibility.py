"""Verify Terraform saved-plan and gRPC provider compatibility.

Usage: python3 test-terraform-compatibility.py BASELINE_IMAGE FIXED_BINARY EVIDENCE_DIR
Only initialization has network access; plan/apply use a local null provider.
"""
from pathlib import Path
import subprocess,tempfile,os,json,sys
image=sys.argv[1]
binary=Path(sys.argv[2]).resolve()
p=Path(sys.argv[3]);p.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='grpc-plan-compat-',dir=p,ignore_cleanup_errors=True) as tmp:
 root=Path(tmp);root.chmod(0o777)
 (root/'main.tf').write_text('''terraform {
  required_providers {
    null = { source = "hashicorp/null", version = "3.2.4" }
  }
}
resource "null_resource" "grpc_fixture" { triggers = { value = "grpc-client-security-fixture" } }
resource "terraform_data" "fixture" { input = null_resource.grpc_fixture.id }
output "value" { value = null_resource.grpc_fixture.triggers.value }
''')
 def run(fixed,script,network='none'):
  args=['docker','run','--rm','--network',network,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges=true','--tmpfs','/tmp:rw,mode=1777','-e','HOME=/tmp/home','-e','BG_CONFIG_DIR=/tmp/bg','-e','TF_DATA_DIR=/fixture/.terraform','-v',str(root)+':/fixture','-w','/fixture']
  if fixed:args+=['-v',str(binary)+':/usr/local/bin/terraform:ro']
  args+=['--entrypoint','sh',image,'-c','set -eu; '+script]
  r=subprocess.run(args,capture_output=True,text=True,timeout=240)
  with (p/'worker-terraform-plan-details.log').open('a') as f:f.write(r.stdout+r.stderr)
  assert r.returncode==0,r.stdout+r.stderr
  return r.stdout
 run(False,'terraform init -backend=false -input=false; terraform validate',network='bridge')
 run(False,'terraform plan -out=baseline.tfplan -input=false')
 result=run(True,'terraform apply -input=false baseline.tfplan; terraform output -raw value; terraform plan -out=fixed.tfplan -input=false')
 run(False,'terraform apply -input=false fixed.tfplan; terraform output -raw value')
 assert 'grpc-client-security-fixture' in result
 run(False,'chmod -R a+rwX /fixture/.terraform')
 print(json.dumps({'image':image,'terraform_version':'1.15.7','provider':'hashicorp/null@3.2.4','old_plan_applied_by_fixed_binary':'passed','fixed_plan_applied_by_old_binary':'passed','grpc_provider_roundtrip':'passed','network':'none after provider download','uid':1001}))
