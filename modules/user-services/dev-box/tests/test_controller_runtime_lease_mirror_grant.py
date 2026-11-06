"""The controller runtime role can fence the Crew host-lease mirror — issue #9620.

``src/cloud_desktop/host_fencing.py`` rewrites the owner's lease item in the gateway's
chat-context table with the controller pod's own credentials (the IRSA runtime role, not
the assumed controller role). Dev 2026-11-05 08:59Z logged ``AccessDeniedException`` on
``dynamodb:PutItem`` because the runtime role carried only STS and RDS statements. The
grant must be exactly: PutItem on that one table limited to ``crew#*`` leading keys, and
the table's customer KMS key through DynamoDB only. These checks read the HCL and the
deploy script as text so they cannot pass on a policy that quietly widens or vanishes.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
INFRA = ROOT / "modules/user-services/dev-box/infra/self_managed.tf"
DEPLOY = ROOT / "modules/user-services/dev-box/scripts/deploy-dev-box.sh"


def _runtime_policy_block() -> str:
    text = INFRA.read_text()
    start = text.index('resource "aws_iam_role_policy" "controller_runtime"')
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def test_runtime_policy_grants_the_lease_mirror_put_scoped_to_crew_keys():
    block = _runtime_policy_block()
    fence = block[block.index('Sid       = "CrewHostLeaseMirrorFence"') :]
    fence = fence[: fence.index("}]")]
    assert 'Action    = ["dynamodb:PutItem"]' in fence, "only PutItem: the fence rewrites the item, it never deletes or updates"
    assert "Resource  = var.chat_context_table_arn" in fence
    assert '"ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["crew#*"] }' in fence
    for widened in ("dynamodb:DeleteItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan", '"*"'):
        assert widened not in fence, widened


def test_runtime_policy_grants_the_table_key_through_dynamodb_only():
    block = _runtime_policy_block()
    kms = block[block.index('Sid       = "CrewHostLeaseMirrorKms"') :]
    kms = kms[: kms.index("}]")]
    assert 'Action    = ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"]' in kms
    assert "Resource  = var.chat_context_kms_key_arn" in kms
    assert '"kms:ViaService" = "dynamodb.${var.region}.amazonaws.com"' in kms
    assert "kms:Encrypt" not in kms and "kms:*" not in kms


def test_both_statements_are_absent_when_the_inputs_are_empty():
    block = _runtime_policy_block()
    assert 'var.chat_context_table_arn == "" ? [] : [{' in block
    assert 'var.chat_context_table_arn == "" || var.chat_context_kms_key_arn == "" ? [] : [{' in block
    text = INFRA.read_text()
    for name in ("chat_context_table_arn", "chat_context_kms_key_arn"):
        declaration = text[text.index(f'variable "{name}"') :]
        declaration = declaration[: declaration.index("\n}\n")]
        assert 'default = ""' in declaration, name
        assert "validation" in declaration, name


def test_existing_runtime_statements_are_unchanged():
    block = _runtime_policy_block()
    assert 'Sid = "AssumeDevBoxController", Effect = "Allow", Action = ["sts:AssumeRole"], Resource = aws_iam_role.controller.arn' in block
    assert 'Sid      = "GatewayDatabaseIamAuth"' in block and 'Action   = ["rds-db:connect"]' in block


def test_deploy_script_resolves_the_table_and_key_and_passes_both_inputs():
    script = DEPLOY.read_text()
    assert 'describe-table --region "$AWS_REGION" --table-name "adp-${ENVIRONMENT}-chat-context"' in script
    assert "Table.SSEDescription.KMSMasterKeyArn" in script
    assert '-var "chat_context_table_arn=$CHAT_CONTEXT_TABLE_ARN"' in script
    assert '-var "chat_context_kms_key_arn=$CHAT_CONTEXT_KMS_KEY_ARN"' in script
    # Both values are validated before they reach Terraform.
    assert re.search(r'CHAT_CONTEXT_TABLE_ARN.*=~ \^arn:aws:dynamodb', script)
    assert re.search(r'CHAT_CONTEXT_KMS_KEY_ARN.*=~ \^arn:aws:kms', script)
