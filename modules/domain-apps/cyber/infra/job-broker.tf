locals {
  cyber_broker_role_arn  = data.terraform_remote_state.platform.outputs.gateway_service_irsa_role_arn
  cyber_broker_role_name = data.terraform_remote_state.platform.outputs.gateway_service_irsa_role_name
  cyber_sample_bucket    = "adp-${var.environment}-chat-artifacts-${data.aws_caller_identity.current.account_id}"
}

# A caller that bypasses the application cannot enqueue forged identity fields.
# Only the gateway that verifies GitHub+STS identity may register work.
resource "aws_sqs_queue_policy" "cyber_broker_only" {
  for_each = {
    triage = { url = aws_sqs_queue.cyber_triage_tasks.url, arn = aws_sqs_queue.cyber_triage_tasks.arn }
    static = { url = aws_sqs_queue.cyber_static_tasks.url, arn = aws_sqs_queue.cyber_static_tasks.arn }
  }
  queue_url = each.value.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyUnregisteredJobProducers", Effect = "Deny", Principal = "*",
      Action    = ["sqs:SendMessage"], Resource = each.value.arn,
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = local.cyber_broker_role_arn } }
    }]
  })
}

resource "aws_iam_role_policy" "cyber_job_broker" {
  name = "${local.name_prefix}-job-broker"
  role = local.cyber_broker_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "VersionedSampleCapabilities", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"],
        Resource = "arn:aws:s3:::${local.cyber_sample_bucket}/o/*/t/*/u/*/s/*/*/in/*"
      },
      {
        Sid      = "RegisterJobs", Effect = "Allow", Action = ["sqs:SendMessage"],
        Resource = [aws_sqs_queue.cyber_triage_tasks.arn, aws_sqs_queue.cyber_static_tasks.arn]
      },
      {
        Sid      = "OwnRunResults", Effect = "Allow", Action = ["dynamodb:Query"],
        Resource = aws_dynamodb_table.cyber_analysis_results.arn
      },
      {
        Sid       = "ResultEncryption", Effect = "Allow", Action = ["kms:Decrypt", "kms:DescribeKey"],
        Resource  = aws_kms_key.dynamodb.arn,
        Condition = { StringEquals = { "kms:ViaService" = "dynamodb.${var.aws_region}.amazonaws.com" } }
      },
    ]
  })
}

output "cyber_gateway_config" {
  description = "Merge into bedrockgateway-config before rolling the new workers. No credentials."
  value = {
    CYBER_SAMPLE_BUCKET = local.cyber_sample_bucket
    CYBER_TRIAGE_QUEUE  = aws_sqs_queue.cyber_triage_tasks.url
    CYBER_STATIC_QUEUE  = aws_sqs_queue.cyber_static_tasks.url
    CYBER_RESULTS_TABLE = aws_dynamodb_table.cyber_analysis_results.name
  }
}
