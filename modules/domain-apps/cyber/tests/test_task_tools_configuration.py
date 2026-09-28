"""Evaluate deployment variable validation with Terraform, without providers/AWS."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
TERRAFORM = shutil.which('terraform')


@pytest.mark.skipif(TERRAFORM is None, reason='Terraform required for variable validation')
@pytest.mark.parametrize('values,valid', [
    ({}, True),
    ({'tools_endpoint':'https://api.example/dev/tools/cyber','task_persona_tools':{'agent-task-cyber':['cyber.triage','cyber.url_analysis']}}, True),
    ({'tools_endpoint':'http://api.example/tools/cyber'}, False),
    ({'tools_endpoint':'https://user:secret@api.example/tools/cyber'}, False),
    ({'tools_endpoint':'https://api.example/tools/cyber?override=x'}, False),
    ({'task_persona_tools':{'agent-task-cyber':['cyber.triage','cyber.triage']}}, False),
    ({'task_persona_tools':{'agent-task-cyber':['https://model-selected.example']}}, False),
    ({'task_persona_tools':{f'persona{i}':[] for i in range(65)}}, False),
])
def test_tools_config_validation(tmp_path, values, valid):
    worker = (ROOT/'modules/agent-factory/webhook-ingress/infra/variables.tf').read_text()
    cyber = (ROOT/'modules/domain-apps/cyber/infra/platform-integration/variables.tf').read_text()
    config = 'variable "task_persona_tools"' + worker.split('variable "task_persona_tools"',1)[1]
    config += '\nvariable "tools_endpoint"' + cyber.split('variable "tools_endpoint"',1)[1]
    (tmp_path/'main.tf').write_text(config)
    (tmp_path/'test.tfvars.json').write_text(json.dumps(values))
    result = subprocess.run([TERRAFORM,'plan','-input=false','-lock=false','-var-file=test.tfvars.json'],cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert (result.returncode == 0) == valid, result.stdout + result.stderr


def test_composition_preserves_domain_endpoint_and_generic_permission_ownership():
    source = (ROOT/'modules/domain-apps/cyber/infra/hosted-integration/main.tf').read_text()
    assert 'tools_endpoint          = lookup(var.settings, "tools_endpoint", "")' in source
    webhook = (ROOT/'modules/agent-factory/webhook-ingress/infra/domain-apps.tf').read_text()
    assert 'data "terraform_remote_state" "cyber"' in webhook
    assert 'module "cyber"' not in webhook
    output = (ROOT/'modules/domain-apps/cyber/infra/platform-integration/outputs.tf').read_text()
    assert 'ADP_CYBER_TOOLS_ENDPOINT = var.tools_endpoint' in output
    gateway = (ROOT/'modules/agent-factory/webhook-ingress/infra/worker-gateway-config.tf').read_text()
    assert 'ADP_TASK_PERSONA_TOOLS                 = jsonencode(var.task_persona_tools)' in gateway
    assert 'ADP_CYBER_TOOLS_ENDPOINT' not in gateway
