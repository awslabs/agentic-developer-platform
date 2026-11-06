mock_provider "aws" {}
variables {
  name                 = "example-investigation"
  aws_account_id       = "000000000000"
  aws_region           = "us-east-1"
  connection_role_name = "example-product-connection"
  lambda_arn           = "arn:aws:lambda:us-east-1:000000000000:function:example"
  log_group_arn        = "arn:aws:logs:us-east-1:000000000000:log-group:/aws/lambda/example:*"
}
run "empty_selection_has_no_authority" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.connection) == 0 && length(aws_iam_role.reader) == 0
    error_message = "An unselected workload cannot create permissions."
  }
}
run "setup_is_tag_bound" {
  command = plan
  variables { setup_fingerprint = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }
  assert {
    condition     = jsondecode(aws_iam_role_policy.connection[0].policy).Statement[0].Action == ["aidevops:CreateAgentSpace", "aidevops:TagResource"] && jsondecode(aws_iam_role_policy.connection[0].policy).Statement[0].Condition["ForAllValues:StringEquals"]["aws:TagKeys"] == ["adp:scope"]
    error_message = "Tagged creation needs its dependent tagging permission, limited to the ownership tag."
  }
  assert {
    condition     = length(aws_iam_role.reader) == 0 && length(local.workload_statements) == 0 && jsondecode(aws_iam_role_policy.connection[0].policy).Statement[0].Condition.StringEquals["aws:RequestTag/adp:scope"] == var.setup_fingerprint && jsondecode(aws_iam_role_policy.connection[0].policy).Statement[1].Condition.StringEquals["aws:ResourceTag/adp:scope"] == var.setup_fingerprint
    error_message = "Creation and goal changes require the exact ADP operation fingerprint."
  }
}
run "workload_removes_creation_and_separates_reader" {
  command = plan
  variables {
    setup_fingerprint = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    native_space_id   = "example-space"
  }
  assert {
    condition = length(local.setup_statements) == 0 && jsondecode(aws_iam_role.reader[0].assume_role_policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == local.space_arn && jsondecode(aws_iam_role_policy.reader[0].policy).Statement == [
      { Effect = "Allow", Action = "lambda:GetFunction", Resource = var.lambda_arn },
      { Effect = "Allow", Action = "logs:FilterLogEvents", Resource = var.log_group_arn }
    ]
    error_message = "The native reader must have only the exact workload reads and exact-space trust."
  }
  assert {
    condition     = local.workload_statements[0].Resource == local.space_arn && !contains(local.workload_statements[0].Action, "aidevops:UpdateGoal") && local.workload_statements[3].Condition.StringEquals["iam:PassedToService"] == "aidevops.amazonaws.com"
    error_message = "Workload access must not modify goals; passing the reader role is DevOps-only."
  }
  assert {
    condition     = local.workload_statements[6].Action == ["aidevops:GetAssociation"] && local.workload_statements[6].Resource == "${local.space_arn}/association/*"
    error_message = "Association readback requires its resource type under only the selected native space."
  }
}
run "foreign_lambda_is_rejected" {
  command = plan
  variables {
    native_space_id = "example-space"
    lambda_arn      = "arn:aws:lambda:us-east-1:111111111111:function:foreign"
  }
  expect_failures = [aws_iam_role_policy.connection]
}
