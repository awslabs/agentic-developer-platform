mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_region" {
    defaults = { name = "us-east-1" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/pricing-test" }
  }
  mock_resource "aws_lambda_layer_version" {
    defaults = { arn = "arn:aws:lambda:us-east-1:123456789012:layer:pricing-test:1" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-east-1:123456789012:function:bedrockgw-test-pricing-refresh" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:bedrockgw-test-pricing-alarms" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:123456789012:rule/bedrockgw-test-pricing-refresh-schedule" }
  }
}

override_resource {
  target = aws_sqs_queue.pricing_delivery_failure
  values = {
    arn = "arn:aws:sqs:us-east-1:123456789012:bedrockgw-test-pricing-delivery-failure"
    url = "https://sqs.us-east-1.amazonaws.com/123456789012/bedrockgw-test-pricing-delivery-failure"
    id  = "https://sqs.us-east-1.amazonaws.com/123456789012/bedrockgw-test-pricing-delivery-failure"
  }
}

override_resource {
  target = aws_sqs_queue.pricing_execution_failure
  values = {
    arn = "arn:aws:sqs:us-east-1:123456789012:bedrockgw-test-pricing-execution-failure"
    url = "https://sqs.us-east-1.amazonaws.com/123456789012/bedrockgw-test-pricing-execution-failure"
    id  = "https://sqs.us-east-1.amazonaws.com/123456789012/bedrockgw-test-pricing-execution-failure"
  }
}

override_resource {
  target = aws_sqs_queue.pricing_alarm_inbox[0]
  values = {
    arn = "arn:aws:sqs:us-east-1:123456789012:bedrockgw-test-pricing-alarm-inbox"
    url = "https://sqs.us-east-1.amazonaws.com/123456789012/bedrockgw-test-pricing-alarm-inbox"
    id  = "https://sqs.us-east-1.amazonaws.com/123456789012/bedrockgw-test-pricing-alarm-inbox"
  }
}

variables {
  environment            = "test"
  name_prefix            = "bedrockgw-test"
  chat_logs_bucket_name  = "pricing-test-chat-logs"
  chat_logs_bucket_arn   = "arn:aws:s3:::pricing-test-chat-logs"
  vpc_id                 = "vpc-0123456789abcdef0"
  private_subnet_ids     = ["subnet-0123456789abcdef0"]
  rds_security_group_id  = "sg-0123456789abcdef0"
  db_host                = "database.example.invalid"
  db_name                = "pricing_test"
  db_username            = "bgadmin"
  lambda_artifact_bucket = "pricing-test-artifacts"
}

run "default_operational_delivery" {
  command = apply

  assert {
    condition = (
      aws_lambda_function.pricing_refresh.timeout == 180 &&
      aws_cloudwatch_event_rule.pricing_refresh.schedule_expression == "cron(0 6 * * ? *)" &&
      aws_cloudwatch_event_rule.pricing_refresh.state == "DISABLED"
    )
    error_message = "Fresh deployment must have the bounded refresh runtime and a disabled daily schedule until release verification."
  }

  assert {
    condition = (
      aws_cloudwatch_event_target.pricing_refresh.retry_policy[0].maximum_retry_attempts == 2 &&
      aws_cloudwatch_event_target.pricing_refresh.retry_policy[0].maximum_event_age_in_seconds == 3600 &&
      aws_lambda_function_event_invoke_config.pricing_refresh.maximum_retry_attempts == 2 &&
      aws_lambda_function_event_invoke_config.pricing_refresh.maximum_event_age_in_seconds == 3600 &&
      aws_lambda_function_event_invoke_config.pricing_refresh.function_name == aws_lambda_function.pricing_refresh.function_name &&
      aws_cloudwatch_event_target.pricing_refresh.dead_letter_config[0].arn != aws_lambda_function_event_invoke_config.pricing_refresh.destination_config[0].on_failure[0].destination
    )
    error_message = "Delivery and execution failures need separate queues and independently bounded retries on the actual target."
  }

  assert {
    condition = alltrue([
      for q in [aws_sqs_queue.pricing_delivery_failure, aws_sqs_queue.pricing_execution_failure, aws_sqs_queue.pricing_alarm_inbox[0]] :
      q.message_retention_seconds == 1209600 && q.sqs_managed_sse_enabled
    ])
    error_message = "Failure queues and operational inbox must retain 14 days with working SQS-managed encryption."
  }

  assert {
    condition = (
      jsondecode(aws_sqs_queue_policy.pricing_delivery_failure.policy).Statement[0].Principal.Service == "events.amazonaws.com" &&
      jsondecode(aws_sqs_queue_policy.pricing_delivery_failure.policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == aws_cloudwatch_event_rule.pricing_refresh.arn &&
      jsondecode(aws_sqs_queue_policy.pricing_delivery_failure.policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012" &&
      jsondecode(aws_iam_role_policy.pricing_refresh.policy).Statement[0].Resource == aws_sqs_queue.pricing_execution_failure.arn &&
      jsondecode(aws_iam_role_policy.pricing_refresh.policy).Statement[1].Condition.StringEquals["cloudwatch:namespace"] == "ADP/Gateway"
    )
    error_message = "Failure producers and metrics must retain scoped permissions."
  }

  assert {
    condition = (
      length(aws_sns_topic.pricing_alarms) == 1 &&
      aws_sns_topic_subscription.pricing_alarm_inbox[0].protocol == "sqs" &&
      aws_sns_topic_subscription.pricing_alarm_inbox[0].endpoint == aws_sqs_queue.pricing_alarm_inbox[0].arn &&
      aws_sns_topic.pricing_alarms[0].kms_master_key_id == aws_kms_key.pricing_alarms[0].arn &&
      jsondecode(aws_kms_key.pricing_alarms[0].policy).Statement[1].Principal.Service == "cloudwatch.amazonaws.com" &&
      jsondecode(aws_kms_key.pricing_alarms[0].policy).Statement[1].Condition.StringEquals["kms:EncryptionContext:aws:sns:topicArn"] == aws_sns_topic.pricing_alarms[0].arn &&
      jsondecode(aws_sns_topic_policy.pricing_alarms[0].policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012" &&
      jsondecode(aws_sqs_queue_policy.pricing_alarm_inbox[0].policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == aws_sns_topic.pricing_alarms[0].arn
    )
    error_message = "Default notification delivery requires the complete CloudWatch/KMS/SNS/SQS permission and subscription chain."
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.pricing_full_refresh_missing.evaluation_periods == 30 &&
      aws_cloudwatch_metric_alarm.pricing_full_refresh_missing.datapoints_to_alarm == 30 &&
      aws_cloudwatch_metric_alarm.pricing_full_refresh_missing.treat_missing_data == "breaching" &&
      aws_cloudwatch_metric_alarm.pricing_oldest_source.threshold == 48 &&
      aws_cloudwatch_metric_alarm.pricing_oldest_source.treat_missing_data == "breaching" &&
      length(aws_cloudwatch_metric_alarm.pricing_source_age) == 12 &&
      aws_cloudwatch_metric_alarm.unknown_model_pricing.treat_missing_data == "notBreaching" &&
      aws_lambda_permission.usage_tracker_s3.source_account == "123456789012" &&
      aws_lambda_permission.usage_tracker_s3.source_arn == var.chat_logs_bucket_arn
    )
    error_message = "Freshness and anomaly alarms require different missing-data semantics; preserve the tracker S3 account boundary."
  }

  # AWS rejects service wildcards in topic policies, even though IAM accepts
  # them. This service contract is documented in the SNS API permissions table.
  assert {
    condition = (
      jsondecode(aws_sns_topic_policy.pricing_alarms[0].policy).Statement[1].Effect == "Deny" &&
      jsondecode(aws_sns_topic_policy.pricing_alarms[0].policy).Statement[1].Principal == "*" &&
      jsondecode(aws_sns_topic_policy.pricing_alarms[0].policy).Statement[1].Condition.Bool["aws:SecureTransport"] == "false" &&
      contains(jsondecode(aws_sns_topic_policy.pricing_alarms[0].policy).Statement[1].Action, "sns:Publish") &&
      alltrue([
        for action in jsondecode(aws_sns_topic_policy.pricing_alarms[0].policy).Statement[1].Action :
        can(regex("^sns:(AddPermission|DeleteTopic|GetDataProtectionPolicy|GetTopicAttributes|ListSubscriptionsByTopic|ListTagsForResource|Publish|PutDataProtectionPolicy|RemovePermission|SetTopicAttributes|Subscribe)$", action))
      ])
    )
    error_message = "Transport denial must use explicit SNS topic-policy actions accepted by the service."
  }
}

run "supplied_notification_topics" {
  command = apply
  variables {
    alarm_actions = ["arn:aws:sns:us-east-1:123456789012:existing-budget-alarms"]
  }

  assert {
    condition = (
      length(aws_sns_topic.pricing_alarms) == 0 &&
      length(aws_sqs_queue.pricing_alarm_inbox) == 0 &&
      length(aws_kms_key.pricing_alarms) == 0 &&
      output.pricing_alarm_inbox == null &&
      aws_cloudwatch_metric_alarm.pricing_oldest_source.alarm_actions == toset(var.alarm_actions) &&
      aws_cloudwatch_metric_alarm.unknown_model_pricing.alarm_actions == toset(var.alarm_actions)
    )
    error_message = "Supplied destinations must be used without creating a redundant inbox or changing anomaly delivery."
  }
}
