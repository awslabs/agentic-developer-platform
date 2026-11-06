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
}
run "no_implicit_image_update" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.runner_image_update) == 0
    error_message = "Activation alone must not grant image updates."
  }
}
run "bound_template_and_group_only" {
  command = plan
  variables {
    runner_image_update = {
      group_id           = 37
      launch_template_id = "lt-0123456789abcdef0"
      asg_arn            = "arn:aws:autoscaling:us-west-2:123456789012:autoScalingGroup:11111111-2222-3333-4444-555555555555:autoScalingGroupName/adp-test-gitlab-job-37"
    }
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.runner_image_update[0].policy) == jsondecode(jsonencode({
      Version = "2012-10-17"
      Statement = [
        { Sid = "VersionBoundJobTemplate", Effect = "Allow", Action = "ec2:CreateLaunchTemplateVersion", Resource = "arn:aws:ec2:us-west-2:123456789012:launch-template/lt-0123456789abcdef0", Condition = { StringEquals = { "aws:RequestedRegion" = "us-west-2" } } },
        { Sid = "AdvanceBoundJobGroup", Effect = "Allow", Action = "autoscaling:UpdateAutoScalingGroup", Resource = "arn:aws:autoscaling:us-west-2:123456789012:autoScalingGroup:11111111-2222-3333-4444-555555555555:autoScalingGroupName/adp-test-gitlab-job-37", Condition = { StringEquals = { "aws:RequestedRegion" = "us-west-2" } } }
      ]
    }))
    error_message = "Only version the existing template and update the exact ASG, without instance control or PassRole."
  }
}
run "reject_other_group" {
  command = plan
  variables {
    runner_image_update = {
      group_id           = 99
      launch_template_id = "lt-0123456789abcdef0"
      asg_arn            = "arn:aws:autoscaling:us-west-2:123456789012:autoScalingGroup:11111111-2222-3333-4444-555555555555:autoScalingGroupName/adp-test-gitlab-job-99"
    }
  }
  expect_failures = [var.runner_image_update]
}
run "reject_other_account" {
  command = plan
  variables {
    runner_image_update = {
      group_id           = 37
      launch_template_id = "lt-0123456789abcdef0"
      asg_arn            = "arn:aws:autoscaling:us-west-2:999999999999:autoScalingGroup:11111111-2222-3333-4444-555555555555:autoScalingGroupName/adp-test-gitlab-job-37"
    }
  }
  expect_failures = [var.runner_image_update]
}
