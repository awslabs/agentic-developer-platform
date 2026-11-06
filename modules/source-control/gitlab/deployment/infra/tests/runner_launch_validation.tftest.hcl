mock_provider "aws" {
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}
variables {
  account_id  = "123456789012"
  aws_region  = "us-west-2"
  environment = "test"
  repository  = "example/platform"
  runner_activation = {
    group_id              = 37
    cluster_name          = "adp-test-eks-cluster"
    access_entry_arn      = "arn:aws:eks:us-west-2:123456789012:access-entry/adp-test-eks-cluster/role/123456789012/adp-test-gitlab-trusted-deployment/11111111-2222-3333-4444-555555555555"
    eks_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-west-2.amazonaws.com/id/ABC123"
    vpc_id                = "vpc-0123456789abcdef0"
  }
  runner_image_update = {
    group_id           = 37
    launch_template_id = "lt-0123456789abcdef0"
    asg_arn            = "arn:aws:autoscaling:us-west-2:123456789012:autoScalingGroup:11111111-2222-3333-4444-555555555555:autoScalingGroupName/adp-test-gitlab-job-37"
  }

}

run "no_implicit_launch_validation" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.runner_launch_validation) == 0
    error_message = "Template update alone must not grant RunInstances."
  }
}
run "exact_job_resources_and_template_required" {
  command = plan
  variables {
    runner_launch_validation = {
      image_id          = "ami-0123456789abcdef0"
      security_group_id = "sg-0123456789abcdef0"
      subnet_ids        = ["subnet-0123456789abcdef0"]
    }
  }
  assert {
    condition = length(jsondecode(aws_iam_role_policy.runner_launch_validation[0].policy).Statement) == 3 && alltrue([
      for statement in jsondecode(aws_iam_role_policy.runner_launch_validation[0].policy).Statement : contains(["ec2:RunInstances", "ec2:CreateTags"], statement.Action)
    ])
    error_message = "Only Auto Scaling's launch-validation and tag dependencies are allowed."
  }
  assert {
    condition = toset(jsondecode(aws_iam_role_policy.runner_launch_validation[0].policy).Statement[0].Resource) == toset([
      "arn:aws:ec2:us-west-2:123456789012:launch-template/lt-0123456789abcdef0",
      "arn:aws:ec2:us-west-2::image/ami-0123456789abcdef0",
      "arn:aws:ec2:us-west-2:123456789012:security-group/sg-0123456789abcdef0",
      "arn:aws:ec2:us-west-2:123456789012:subnet/subnet-0123456789abcdef0",
      "arn:aws:ec2:us-west-2:123456789012:volume/*",
      "arn:aws:ec2:us-west-2:123456789012:network-interface/*"
    ]) && alltrue([for statement in slice(jsondecode(aws_iam_role_policy.runner_launch_validation[0].policy).Statement, 0, 2) : statement.Condition.ArnEquals["ec2:LaunchTemplate"] == "arn:aws:ec2:us-west-2:123456789012:launch-template/lt-0123456789abcdef0"])
    error_message = "No unbound image, subnet, security group or template may be used."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.runner_launch_validation[0].policy).Statement[1].Condition.Null["ec2:InstanceProfile"] == "true" && jsondecode(aws_iam_role_policy.runner_launch_validation[0].policy).Statement[1].Condition.StringEquals["ec2:MetadataHttpTokens"] == "required" && jsondecode(aws_iam_role_policy.runner_launch_validation[0].policy).Statement[2].Condition.StringEquals["ec2:CreateAction"] == "RunInstances" && jsondecode(aws_iam_role_policy.runner_launch_validation[0].policy).Statement[2].Condition.StringEquals["aws:RequestTag/GroupId"] == "37"
    error_message = "Require IMDSv2, no profile, and tags only on a group-bound launch."
  }
}
run "reject_unbound_image" {
  command = plan
  variables {
    runner_launch_validation = {
      image_id          = "*"
      security_group_id = "sg-0123456789abcdef0"
      subnet_ids        = ["subnet-0123456789abcdef0"]
    }
  }
  expect_failures = [var.runner_launch_validation]
}
