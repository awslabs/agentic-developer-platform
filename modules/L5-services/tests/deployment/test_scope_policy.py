"""Keep the Security-specific gateway grant send-only on its exact queue."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
GATEWAY_IAM = ROOT / "modules/gateway/infra/gateway-role-policies.tf"


def qualifies_gateway_sender(source):
    match = re.search(
        r"var\.enable_l5_security \? \[(.*?)\] : \[\]",
        source,
        re.DOTALL,
    )
    if match is None:
        return False
    return bool(
        re.fullmatch(
            r'\s*\{\s*Sid\s*=\s*"L5SecuritySQSPublish"\s*Effect\s*=\s*"Allow"'
            r'\s*Action\s*=\s*\["sqs:SendMessage"\]'
            r"\s*Resource\s*=\s*module\.l5_security\[0\]\.work_queue_arn\s*\}\s*",
            match.group(1),
        )
    )


def test_gateway_grant_is_exact_send_only_and_rejects_expansion():
    source = GATEWAY_IAM.read_text()
    assert qualifies_gateway_sender(source)
    assert not qualifies_gateway_sender(
        source.replace('["sqs:SendMessage"]', '["sqs:*"]')
    )
    assert not qualifies_gateway_sender(
        source.replace(
            "Resource = module.l5_security[0].work_queue_arn", 'Resource = "*"'
        )
    )
    assert not qualifies_gateway_sender(
        source.replace(
            "var.enable_l5_security ? [",
            "true ? [",
        )
    )
