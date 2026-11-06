"""Offline wiring checks for the single automatic repair consumer."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
INFRA = ROOT / "modules/L5-services/infra/modules/worker-runtime"


def test_one_minute_repair_uses_pinned_worker_archive_and_private_access():
    repair = (INFRA / "repair.tf").read_text()
    worker = (INFRA / "main.tf").read_text()
    permissions = (INFRA / "iam.tf").read_text()
    assert repair.count('schedule_expression = "rate(1 minute)"') == 1
    assert 'handler           = "l5_services.workers.recovery.lambda_handler"' in repair
    assert "aws_cloudwatch_event_target" in repair
    assert "arn       = aws_lambda_function.repair.arn" in repair
    assert 'principal     = "events.amazonaws.com"' in repair
    assert "source_arn    = aws_cloudwatch_event_rule.repair.arn" in repair
    for attribute in ("s3_bucket", "s3_key", "s3_object_version", "source_code_hash"):
        assert f"{attribute} " in repair
        assert f"{attribute} " in worker
    assert re.search(r"(?m)^\s*L5_WORKER_FACTORY\s*=\s*var\.worker_factory\s*$", repair)
    assert re.search(r"(?m)^\s*L5_WORK_QUEUE_URL\s*=\s*aws_sqs_queue\.work\.url\s*$", repair)
    assert "security_group_ids = [aws_security_group.worker.id]" in repair
    assert 'resource "aws_vpc_security_group_egress_rule" "gateway_api"' in worker
    assert 'cidr_ipv4         = "0.0.0.0/0"' in worker
    assert "aws_cloudwatch_log_group.repair.name" in permissions
    assert 'schedule_expression = "rate(1 minute)"' not in worker
