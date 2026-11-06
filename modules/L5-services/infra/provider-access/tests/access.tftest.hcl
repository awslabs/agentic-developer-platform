mock_provider "aws" {}

variables {
  environment          = "dev"
  aws_region           = "us-east-1"
  aws_account_id       = "000000000000"
  connection_role_name = "example-product-connection"
}

run "disabled_has_no_grant" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.native_services) == 0
    error_message = "An unselected service must not expand connection permissions."
  }
}

run "selected_security_and_finops_permissions" {
  command = plan
  variables {
    security = {
      enabled             = true
      source_bucket_names = ["example-security-inputs"]
      execution_role_arns = ["arn:aws:iam::000000000000:role/example-security-execution"]
    }
    finops = {
      agent_arns        = ["arn:aws:finops-agent:us-east-1:000000000000:agentspace/example"]
      billing_view_arns = ["arn:aws:billing::000000000000:billingview/example"]
      data_role_arns    = ["arn:aws:iam::000000000000:role/example-finops-data"]
    }
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[0].Resource == ["*"] && jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[1].Resource == [
      "arn:aws:securityagent:us-east-1:000000000000:agent-space/*"
    ] && jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[0].Condition.StringEquals["aws:RequestedRegion"] == var.aws_region
    error_message = "Only Security creation/discovery are regional wildcard operations; existing spaces use the documented ARN type."
  }
  assert {
    condition     = aws_iam_role_policy.native_services[0].role == var.connection_role_name
    error_message = "Native capability permissions must target only the selected existing connection role."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[3].Resource == [
      "arn:aws:s3:::example-security-inputs/security-agent/source/*"
    ]
    error_message = "Security may access only registered source prefixes."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[4].Condition.StringEquals["iam:PassedToService"] == "securityagent.amazonaws.com" && jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[6].Resource == [
      "*"
    ]
    error_message = "Role passing stays exact; native FinOps access uses only its documented regional scope."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[2] == {
      Sid      = "SecuritySetupIntentTagging"
      Effect   = "Allow"
      Action   = ["securityagent:TagResource"]
      Resource = ["arn:aws:securityagent:us-east-1:000000000000:agent-space/*"]
      Condition = {
        StringEquals                = { "aws:RequestedRegion" = "us-east-1" }
        "ForAllValues:StringEquals" = { "aws:TagKeys" = ["adp-setup-operation"] }
        Null                        = { "aws:RequestTag/adp-setup-operation" = "false" }
      }
    }
    error_message = "Tag-on-create must require the setup tag and exclude unrelated tags, resource types, accounts and regions."
  }
  assert {
    condition = alltrue([for statement in jsondecode(aws_iam_role_policy.native_services[0].policy).Statement :
      alltrue([for action in statement.Action : !contains(["*", "iam:*", "sts:AssumeRole", "s3:ListBucket", "finops-agent:CreateAgentSpace"], action)])
    ])
    error_message = "Provider setup must not grant broad IAM, credential assumption, unrelated storage or native FinOps creation."
  }
}

run "foreign_security_execution_role_is_rejected" {
  command = plan
  variables {
    security = {
      enabled             = true
      source_bucket_names = ["example-security-inputs"]
      execution_role_arns = ["arn:aws:iam::000000000001:role/example-security-execution"]
    }
  }
  expect_failures = [var.security]
}

run "wildcard_security_execution_role_is_rejected" {
  command = plan
  variables {
    security = {
      enabled             = true
      source_bucket_names = ["example-security-inputs"]
      execution_role_arns = ["arn:aws:iam::000000000000:role/*"]
    }
  }
  expect_failures = [var.security]
}

run "foreign_billing_scope_is_rejected" {
  command = plan
  variables {
    finops = {
      agent_arns        = ["arn:aws:finops-agent:us-east-1:000000000000:agentspace/example"]
      billing_view_arns = ["arn:aws:billing::000000000001:billingview/example"]
      data_role_arns    = ["arn:aws:iam::000000000000:role/example-finops-data"]
    }
  }
  expect_failures = [var.finops]
}

run "selected_devops_space_only" {
  command = plan
  variables {
    devops = {
      agent_space_arns = ["arn:aws:aidevops:us-east-1:000000000000:agentspace/example"]
    }
  }
  assert {
    condition = length(jsondecode(aws_iam_role_policy.native_services[0].policy).Statement) == 2 && jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[0].Resource == [
      "arn:aws:aidevops:us-east-1:000000000000:agentspace/example"
      ] && jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[1].Resource == [
      "arn:aws:aidevops:us-east-1:000000000000:agentspace/example/association/*"
    ] && jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[1].Action == ["aidevops:GetAssociation"]
    error_message = "DevOps adoption must grant only the selected native space."
  }
  assert {
    condition = alltrue([for action in jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[0].Action :
      !contains(["aidevops:CreateAgentSpace", "aidevops:DeleteAgentSpace", "aidevops:AssociateService", "aidevops:UpdateGoal", "aidevops:CreateAsset", "aidevops:UpdateAsset"], action)
    ])
    error_message = "Selected-space adoption must not grant creation, deletion, integration mutation or preventive-goal changes."
  }
}

run "knowledge_requires_explicit_selected_spaces" {
  command = plan
  variables {
    devops = {
      agent_space_arns           = []
      knowledge_agent_space_arns = ["arn:aws:aidevops:us-east-1:000000000000:agentspace/knowledge-example"]
    }
  }
  assert {
    condition = length(jsondecode(aws_iam_role_policy.native_services[0].policy).Statement) == 1 && jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[0].Resource == [
      "arn:aws:aidevops:us-east-1:000000000000:agentspace/knowledge-example"
      ] && toset(jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[0].Action) == toset([
        "aidevops:CreateAsset", "aidevops:UpdateAsset", "aidevops:GetAsset", "aidevops:GetAssetContent", "aidevops:ListAssets"
    ]) && jsondecode(aws_iam_role_policy.native_services[0].policy).Statement[0].Condition.StringEquals["aws:RequestedRegion"] == var.aws_region
    error_message = "Knowledge grants require explicit exact-space selection and only the five publication/readback actions."
  }
}

run "foreign_knowledge_space_is_rejected" {
  command = plan
  variables {
    devops = {
      agent_space_arns           = []
      knowledge_agent_space_arns = ["arn:aws:aidevops:us-east-1:000000000001:agentspace/knowledge-example"]
    }
  }
  expect_failures = [var.devops]
}

run "foreign_devops_space_is_rejected" {
  command = plan
  variables {
    devops = { agent_space_arns = ["arn:aws:aidevops:us-east-1:000000000001:agentspace/example"] }
  }
  expect_failures = [var.devops]
}
