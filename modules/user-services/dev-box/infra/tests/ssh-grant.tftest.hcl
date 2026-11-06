mock_provider "aws" {
  mock_resource "aws_iam_instance_profile" {
    defaults = { arn = "arn:aws:iam::000000000000:instance-profile/example-guest" }
  }
}

variables {
  environment                    = "test"
  account_id                     = "000000000000"
  region                         = "us-east-1"
  deployment_id                  = "example-deployment"
  vpc_id                         = "vpc-0123456789abcdef0"
  private_subnet_id              = "subnet-0123456789abcdef0"
  gateway_security_group_id      = "sg-11111111111111111"
  ssm_endpoint_security_group_id = "sg-22222222222222222"
  controller_principal_arn       = "arn:aws:iam::000000000000:role/example-controller"
  image_id                       = "ami-0123456789abcdef0"
  source_revision                = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  template_revision              = "example-r1"
  instance_size                  = "t3.small"
  workspace_disk_gb              = 40
  kms_key_arn                    = "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000"
}

run "absent_without_exact_gateway" {
  command = plan
  assert {
    condition     = length(aws_iam_role.ssh_grant) == 0 && length(aws_iam_role_policy.ssh_grant) == 0 && output.ssh_grant_role_arn == ""
    error_message = "No SSH grant role may exist until the exact gateway role is configured."
  }
  assert {
    condition     = length(aws_iam_role_policy.gateway_ssh_grant) == 0 && aws_ssm_parameter.stage["ssh-grant-role-arn"].value == " "
    error_message = "Without the gateway role nothing is attached to it and the renderers read the empty sentinel (gateway renders BG_DEV_BOX_SSH_GRANT_ROLE_ARN=\"\" and the route stays 503)."
  }
}

run "gateway_only_issues_scoped_session" {
  command = plan
  variables {
    environment           = "test"
    gateway_principal_arn = "arn:aws:iam::000000000000:role/example-gateway"
  }

  assert {
    condition     = length(aws_iam_role.ssh_grant) == 1 && length(aws_iam_role_policy.ssh_grant) == 1 && jsondecode(aws_iam_role.ssh_grant[0].assume_role_policy).Statement[0].Principal.AWS == var.gateway_principal_arn
    error_message = "Only the selected gateway role may assume the SSH grant role."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.ssh_grant[0].policy).Statement == [
      {
        Sid      = "StartTaggedBoxSession"
        Effect   = "Allow"
        Action   = ["ssm:StartSession"]
        Resource = "arn:aws:ec2:us-east-1:000000000000:instance/*"
        Condition = {
          StringEquals = { "aws:ResourceTag/adp:deployment-id" = "example-deployment", "aws:ResourceTag/adp:component" = "dev-box" }
          Null         = { "aws:ResourceTag/adp:owner-id" = "false" }
        }
      },
      {
        Sid      = "UseSSHDocument"
        Effect   = "Allow"
        Action   = ["ssm:StartSession"]
        Resource = "arn:aws:ssm:us-east-1::document/AWS-StartSSHSession"
      },
      {
        Sid      = "PushOneOSUserKey"
        Effect   = "Allow"
        Action   = ["ec2-instance-connect:SendSSHPublicKey"]
        Resource = "arn:aws:ec2:us-east-1:000000000000:instance/*"
        Condition = {
          StringEquals = { "aws:ResourceTag/adp:deployment-id" = "example-deployment", "aws:ResourceTag/adp:component" = "dev-box", "ec2:osuser" = "ec2-user" }
          Null         = { "aws:ResourceTag/adp:owner-id" = "false" }
        }
      },
    ]
    error_message = "Only tagged boxes, the SSH document, and ec2-user Instance Connect are permitted; the gateway's session policy narrows the instance ARN."
  }
  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.dashboard) == 0
    error_message = "SSH grant cannot enable a guest ingress rule."
  }
  assert {
    condition     = length(aws_iam_role_policy.gateway_ssh_grant) == 1 && aws_iam_role_policy.gateway_ssh_grant[0].role == "example-gateway" && aws_iam_role_policy.gateway_ssh_grant[0].name == "adp-dev-box-assume-ssh-grant"
    error_message = "The gateway role named in gateway_principal_arn receives the inline sts:AssumeRole grant; no other principal does."
  }
}

run "reject_foreign_gateway" {
  command = plan
  variables {
    environment           = "test"
    gateway_principal_arn = "arn:aws:iam::111111111111:role/foreign-gateway"
  }
  expect_failures = [aws_iam_role.ssh_grant]
}

run "reject_controller_as_gateway" {
  command = plan
  variables {
    environment           = "test"
    gateway_principal_arn = "arn:aws:iam::000000000000:role/example-controller"
  }
  expect_failures = [aws_iam_role.ssh_grant]
}
