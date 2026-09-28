"""Render real Terraform policy locals with fixture resources, without providers or state."""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
root = Path(__file__).resolve().parents[1] / 'infra'
blocks = '\n'.join(block for file in ('scaledjob-iam.tf', 'agent-authority-boundary.tf') for block in re.findall(r'^locals \{.*?^\}', (root / file).read_text(), re.S | re.M))
values = {
 'var.task_tool_invoke_resources': json.dumps(json.loads(sys.argv[1]) if len(sys.argv) > 1 else []),
 # App outputs are provider-independent inputs, like the fixture resource ARNs below.
 'try(module.cyber[0].worker_browser_permissions, [])': '[]',
 'local.domain_worker_artifact_resources': '["arn:aws:s3:::fixture-domain-artifacts/*"]',
 'var.agent_authority_enabled': 'true', 'var.aws_region': '"us-east-1"', 'var.environment': '"dev"',
 'local.account_id': '"879318057152"', 'local.name_prefix': '"adp-dev"',
 'local.webhook_secrets_kms_key_arn': '"arn:aws:kms:us-east-1:879318057152:key/shared-secret-key"',
 'aws_iam_role.agent_authority_worker[0].arn': '"arn:aws:iam::879318057152:role/adp-dev-agent-authority-worker-role"',
 'aws_iam_role.agent_scaledjob.arn': '"arn:aws:iam::879318057152:role/adp-dev-agent-scaledjob-role"',
 'kubernetes_service_account.agent_authority_worker[0].metadata[0].name': '"agent-authority-worker-sa"',
 'kubernetes_service_account.agent_scaledjob_sa.metadata[0].name': '"agent-scaledjob-sa"',
 'aws_kms_key.dynamodb.arn': '"arn:aws:kms:us-east-1:879318057152:key/dynamodb-key"',
 'aws_cloudwatch_log_group.agent_logs.arn': '"arn:aws:logs:us-east-1:879318057152:log-group:/adp/dev/agent-factory/agent"',
 'aws_cloudwatch_log_group.agent_bootstrap.arn': '"arn:aws:logs:us-east-1:879318057152:log-group:/adp/dev/agent-factory/bootstrap"',
 'aws_secretsmanager_secret.marker_signing_key.arn': '"arn:aws:secretsmanager:us-east-1:879318057152:secret:adp/dev/marker"',
 'aws_sqs_queue.agent_submit.arn': '"arn:aws:sqs:us-east-1:879318057152:adp-dev-agent-submit.fifo"',
 'aws_s3_bucket.agent_run_logs.arn': '"arn:aws:s3:::adp-dev-agent-run-logs-879318057152"',
}
for ref, value in values.items():
 blocks = blocks.replace(ref, value)
with tempfile.TemporaryDirectory(prefix='adp-iam-render-') as td:
 Path(td, 'main.tf').write_text(blocks)
 result = subprocess.run(['terraform', 'console', '-no-color'], input='jsonencode(local.agent_authority_boundary)\n', cwd=td, capture_output=True, text=True, check=True)
 policy = json.loads(json.loads(result.stdout))
print(json.dumps(policy))
