mock_provider "aws" {}

variables {
  selection = {
    account_id                  = "111122223333"
    region                      = "us-east-1"
    tenant_id                   = "tenant-one"
    product_id                  = "product-one"
    project_id                  = "project-one"
    configuration_id            = "configuration-one"
    source_scope_sha256         = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    connection_role_arn         = "arn:aws:iam::111122223333:role/connection-one"
    operator_role_arn           = "arn:aws:iam::111122223333:role/operator-one"
    agent_space_id              = "as-00000000-0000-0000-0000-000000000002"
    agent_space_name            = "adp-00000000-0000-0000-0000-000000000001"
    original_setup_operation_id = "00000000-0000-0000-0000-000000000001"
    original_intent_hash        = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
}

run "isolated_permissions" {
  command = plan
  assert {
    condition     = output.review.execution_trust.Statement[0].Condition.ArnEquals["aws:SourceArn"] == output.review.native_space_arn
    error_message = "Service assumption must be bound to the exact selected space."
  }
  assert {
    condition = toset(flatten([for statement in output.review.execution_permissions.Statement : statement.Action])) == toset([
      "iam:GetRole", "iam:SimulatePrincipalPolicy", "s3:GetObject", "s3:ListBucket", "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"
    ])
    error_message = "No native jobs, pentest, secrets, writes to source, or unrelated IAM actions belong on the service role."
  }
  assert {
    condition     = aws_s3_bucket.source.force_destroy == false && aws_s3_bucket_public_access_block.source.block_public_policy && aws_s3_bucket_ownership_controls.source.rule[0].object_ownership == "BucketOwnerEnforced"
    error_message = "The dedicated bucket must remain private and retained."
  }
  assert {
    condition     = output.review.log_retention_enforced == false
    error_message = "Do not claim retention on service-created logs is already enforced."
  }
}

run "reject_unresolved_selection" {
  command = plan
  variables {
    selection = {
      account_id                  = "111122223333", region = "us-east-1", tenant_id = "tenant-one", product_id = "product-one", project_id = "project-one", configuration_id = "configuration-one"
      source_scope_sha256         = "UNRESOLVED"
      connection_role_arn         = "arn:aws:iam::111122223333:role/connection-one", operator_role_arn = "arn:aws:iam::111122223333:role/operator-one"
      agent_space_id              = "as-00000000-0000-0000-0000-000000000002", agent_space_name = "adp-00000000-0000-0000-0000-000000000001"
      original_setup_operation_id = "00000000-0000-0000-0000-000000000001", original_intent_hash = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  }
  expect_failures = [var.selection]
}

run "reject_foreign_role" {
  command = plan
  variables {
    selection = {
      account_id                  = "111122223333", region = "us-east-1", tenant_id = "tenant-one", product_id = "product-one", project_id = "project-one", configuration_id = "configuration-one"
      source_scope_sha256         = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      connection_role_arn         = "arn:aws:iam::999988887777:role/connection-one", operator_role_arn = "arn:aws:iam::111122223333:role/operator-one"
      agent_space_id              = "as-00000000-0000-0000-0000-000000000002", agent_space_name = "adp-00000000-0000-0000-0000-000000000001"
      original_setup_operation_id = "00000000-0000-0000-0000-000000000001", original_intent_hash = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }
  }
  expect_failures = [var.selection]
}
