import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path
import superplane_executor.service as service
import superplane_executor.task_worker
import workspace_provisioning
import superplane_bootstrap
import superplane_contracts
import harness_jobs

def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout

assert os.getuid() == 65531
assert hashlib.sha256(Path('/usr/local/bin/kubectl').read_bytes()).hexdigest() == 'b3aa6322e5da4e85f06ceb3a7c99555b21f0f709b103891109b9360de2796ba7'
kubectl = json.loads(run('kubectl', 'version', '--client', '-o', 'json'))
assert kubectl['clientVersion']['gitVersion'] == 'v1.31.4+adp.security.1'
assert kubectl['clientVersion']['goVersion'] == 'go1.26.8'
assert hashlib.sha256(Path('/usr/local/bin/terraform').read_bytes()).hexdigest() == '0f4c860aaf922997867337b2ee97029f94ae8c2e1fee40dc442e47e706e35245'
assert json.loads(run('terraform', 'version', '-json'))['terraform_version'] == '1.9.8'
assert 'aws-cli/2.37.4 Python/3.14.7' in run('aws', '--version')
run('/opt/executor/bin/python', '-m', 'pip', 'check')
assert (Path(workspace_provisioning.__file__).parent / '_data/workspaces/.terraform.lock.hcl').is_file()
run('kubectl', 'config', 'set-cluster', 'offline', '--server=http://127.0.0.1:65534')
run('kubectl', 'config', 'set-credentials', 'fixture', '--token=synthetic-offline-only')
run('kubectl', 'config', 'set-context', 'offline', '--cluster=offline', '--user=fixture')
run('kubectl', 'config', 'use-context', 'offline')
for command, name in [('namespace', 'fixture'), ('configmap', 'fixture')]:
    obj = json.loads(run('kubectl', 'create', command, name, '--dry-run=client', '-o', 'json'))
    assert obj['metadata']['name'] == 'fixture'
paid = subprocess.run(['superplane-paid-worker'], text=True, capture_output=True)
assert paid.returncode == 1
assert paid.stderr.strip() == 'paid task stopped; durable recovery required'
async def idle():
    stop = asyncio.Event()
    entered = []
    async def refused(*args, **kwargs):
        entered.append(True)
    service.serve = refused
    task = asyncio.create_task(service.run(stop))
    await asyncio.sleep(0.1)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert not entered
asyncio.run(idle())
print(json.dumps({'uid':os.getuid(), 'kubectl':kubectl, 'terraform':'1.9.8', 'aws_cli':'2.37.4', 'pip_check':'passed', 'paid_worker_missing_authority':'refused', 'service_missing_authority':'idle_without_serving', 'offline_kubectl_creation':'passed', 'installed_imports_and_workspace_lock':'passed'}))
