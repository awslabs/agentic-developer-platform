import json
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

old = '/baseline'
new = '/candidate'
with tempfile.TemporaryDirectory(prefix='terraform-compat-', dir='/tmp') as root:
    work = Path(root)
    env = dict(os.environ, HOME=str(work), TF_DATA_DIR=str(work / 'data'),
               TF_CLI_CONFIG_FILE=str(work / 'terraformrc'),
               TF_IN_AUTOMATION='1', CHECKPOINT_DISABLE='1',
               AWS_EC2_METADATA_DISABLED='true')
    (work / 'terraformrc').write_text('disable_checkpoint = true\n')
    def run(binary, *args, expect=0):
        p = subprocess.run([binary, *args], cwd=work, env=env, capture_output=True, text=True)
        assert p.returncode == expect, (args, p.returncode, p.stdout, p.stderr)
        return p.stdout
    for binary in (old, new):
        assert json.loads(run(binary, 'version', '-json'))['terraform_version'] == '1.9.8'
    def config(value):
        (work / 'main.tf').write_text('terraform { required_version = "= 1.9.8" }\n'
          + 'resource "terraform_data" "proof" { input = '+json.dumps(value)+' }\n'
          + 'output "proof" { value = terraform_data.proof.output }\n')
    config('baseline-plan')
    run(old, 'init', '-backend=false', '-input=false', '-no-color')
    run(old, 'validate', '-no-color')
    run(old, 'plan', '-input=false', '-out=baseline.plan', '-no-color')
    assert json.loads(run(new, 'show', '-json', 'baseline.plan'))['terraform_version'] == '1.9.8'
    run(new, 'apply', '-input=false', '-auto-approve', '-no-color', 'baseline.plan')
    state1 = json.loads(run(new, 'state', 'pull'))
    assert json.loads(run(old, 'output', '-json'))['proof']['value'] == 'baseline-plan'
    config('rebuilt-plan')
    run(new, 'plan', '-input=false', '-out=rebuilt.plan', '-no-color')
    assert json.loads(run(old, 'show', '-json', 'rebuilt.plan'))['terraform_version'] == '1.9.8'
    run(old, 'apply', '-input=false', '-auto-approve', '-no-color', 'rebuilt.plan')
    state2 = json.loads(run(old, 'state', 'pull'))
    assert state1['lineage'] == state2['lineage']
    assert state2['serial'] > state1['serial']
    assert json.loads(run(new, 'output', '-json'))['proof']['value'] == 'rebuilt-plan'
    run(new, 'plan', '-input=false', '-detailed-exitcode', '-no-color')
    run(new, 'state', 'mv', 'terraform_data.proof', 'terraform_data.renamed')
    state3 = json.loads(run(old, 'state', 'pull'))
    assert state3['resources'][0]['name'] == 'renamed'
    (work / 'main.tf').write_text((work / 'main.tf').read_text().replace('"terraform_data" "proof"', '"terraform_data" "renamed"').replace('terraform_data.proof', 'terraform_data.renamed'))
    run(old, 'plan', '-input=false', '-detailed-exitcode', '-no-color')
    run(new, 'destroy', '-input=false', '-auto-approve', '-no-color')
    assert not json.loads(run(old, 'state', 'pull'))['resources']
    print(json.dumps({'terraform_version':'1.9.8','baseline_sha256':hashlib.sha256(Path(old).read_bytes()).hexdigest(),'candidate_sha256':hashlib.sha256(Path(new).read_bytes()).hexdigest(),'baseline_plan_applied_by_rebuild':'passed',
      'rebuilt_plan_applied_by_baseline':'passed','state_lineage_and_serial':'passed',
      'state_move_roundtrip':'passed','unchanged_plan_exitcode':0,'destroy_cleanup':'passed',
      'provider':'builtin terraform_data','scope':'disposable local state only'}))
