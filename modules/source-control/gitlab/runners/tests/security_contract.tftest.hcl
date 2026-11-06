mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "external" {
  mock_data "external" { defaults = { result = { ready = "true" } } }
}

override_data {
  target = data.aws_lb.gitlab
  values = {
    arn_suffix         = "app/adp-test-gitlab-alb/0123456789abcdef"
    internal           = true
    load_balancer_type = "application"
    vpc_id             = "vpc-0123456789abcdef0"
    security_groups    = ["sg-0123456789abcdef1"]
    subnets            = ["subnet-0123456789abcdef0"]
  }
}
override_resource {
  target          = aws_security_group.cache_endpoint
  override_during = plan
  values          = { id = "sg-0123456789abcdef2" }
}
override_resource {
  target          = aws_vpc_endpoint.cache
  override_during = plan
  values          = { id = "vpce-0123456789abcdef0", network_interface_ids = ["eni-0123456789abcdef0"] }
}
override_data {
  target = data.aws_network_interface.endpoint["42/cache"]
  values = { private_ip = "10.0.0.14", subnet_id = "subnet-0123456789abcdef0", description = "VPC Endpoint Interface vpce-0123456789abcdef0", security_groups = ["sg-0123456789abcdef2"] }
}
override_data {
  target = data.aws_network_interface.endpoint["43/cache"]
  values = { private_ip = "10.0.0.14", subnet_id = "subnet-0123456789abcdef0", description = "VPC Endpoint Interface vpce-0123456789abcdef0", security_groups = ["sg-0123456789abcdef2"] }
}

override_resource {
  target          = aws_s3_bucket.cache
  override_during = plan
  values          = { arn = "arn:aws:s3:::adp-test-gitlab-runners-123456789012", id = "adp-test-gitlab-runners-123456789012" }
}

override_resource {
  target          = aws_autoscaling_group.job
  override_during = plan
  values          = { arn = "arn:aws:autoscaling:us-east-1:123456789012:autoScalingGroup:synthetic" }
}
mock_provider "aws" {
  mock_data "aws_ami" {
    defaults = { root_device_name = "/dev/xvda", block_device_mappings = [{ device_name = "/dev/xvda", ebs = { volume_size = "40" } }] }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0", map_public_ip_on_launch = false }
  }
  mock_data "aws_security_group" {
    defaults = { vpc_id = "vpc-0123456789abcdef0" }
  }
  mock_data "aws_network_interface" {
    defaults = { subnet_id = "subnet-0123456789abcdef0" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

override_data {
  target = data.aws_network_interface.endpoint["42/gitlab"]
  values = { private_ip = "10.0.0.10", subnet_id = "subnet-0123456789abcdef0", description = "ELB app/adp-test-gitlab-alb/0123456789abcdef", security_groups = ["sg-0123456789abcdef1"] }
}
override_data {
  target = data.aws_network_interface.endpoint["43/gitlab"]
  values = { private_ip = "10.0.0.10", subnet_id = "subnet-0123456789abcdef0", description = "ELB app/adp-test-gitlab-alb/0123456789abcdef", security_groups = ["sg-0123456789abcdef1"] }
}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      vpc_id                        = "vpc-0123456789abcdef0"
      private_subnet_ids            = ["subnet-0123456789abcdef0"]
      eks_security_group_id         = "sg-0123456789abcdef0"
      eks_cluster_security_group_id = "sg-0123456789abcdef2"
      eks_cluster_name              = "example-cluster"
      eks_cluster_endpoint          = "https://eks.example.test"
      eks_cluster_ca_certificate    = "Y2E="
      eks_oidc_provider_arn         = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
    }
  }
}

override_data {
  target = data.aws_ssm_parameter.runner_url
  values = {
    value = "https://gitlab.example.test/gitlab"
  }
}

override_data {
  target = data.aws_ssm_parameter.ca_secret_arn
  values = {
    value = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab/ca-chain-Ab12Cd"
  }
}

variables {
  host_ami_id      = "ami-0123456789abcdef0"
  job_image_digest = "registry.example.test/job@sha256:${sha256("synthetic offline fixture")}"
  job_endpoints = {
    "42" = { gitlab_eni_id = "eni-00000000000000042" }
  }
  environment         = "test"
  aws_region          = "us-east-1"
  gitlab_private_zone = "example.test"
  platform_state = {
    bucket = "example-state"
    key    = "test/platform/terraform.tfstate"
    region = "us-east-1"
  }
  groups = {
    "42" = {
      tenant_id        = "tenant-example"
      path             = "tenant-${substr(sha256("tenant-example"), 0, 24)}"
      tags             = ["adp-group-42", "test-ci"]
      access_level     = "ref_protected"
      token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab-runner/42-Ab12Cd"
    }
  }
}

override_data {
  target = data.aws_iam_openid_connect_provider.github
  values = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
}

override_resource {
  target          = aws_launch_template.job["42"]
  override_during = plan
  values          = { id = "lt-0123456789abcdef0" }
}

override_resource {
  target          = aws_launch_template.job["43"]
  override_during = plan
  values          = { id = "lt-0123456789abcdef1" }
}

run "independent_owned_instance_reconciler" {
  command = plan
  assert {
    condition = (
      aws_ssm_parameter.manager_alive["42"].name == "/adp/test/gitlab-runners/42/manager-alive" &&
      aws_iam_role.reconciler.name == "adp-test-gitlab-runners-reconciler" &&
      jsondecode(aws_iam_role.reconciler.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:aws-e/adp:environment:adp-deploy-test" &&
      jsondecode(aws_iam_role_policy.manager["42"].policy).Statement[3].Resource == [
        "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gitlab-runners/42/manager-alive",
        "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gitlab-runners/42/registration-receipt"
      ] &&
      aws_ssm_parameter.registration_receipt["42"].name == "/adp/test/gitlab-runners/42/registration-receipt" &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[0].Resource == [
        "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gitlab-runners/42/manager-alive"
      ] &&
      contains(jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[1].Action, "ec2:DescribeLaunchTemplateVersions") &&
      length(jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement) == 4 &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[3].Action == ["ec2:TerminateInstances"] &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[3].Condition.StringEquals["ec2:ResourceTag/Component"] == "gitlab-job" &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[3].Condition.StringEquals["ec2:ResourceTag/Environment"] == "test" &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[3].Condition.StringEquals["ec2:ResourceTag/GroupId"] == "42" &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[3].Condition.StringEquals["ec2:ResourceTag/LaunchTemplateId"] == "lt-0123456789abcdef0" &&
      !contains(keys(jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[3].Condition.StringEquals), "ec2:ResourceTag/aws:autoscaling:groupName") &&
      one([for tag in aws_autoscaling_group.job["42"].tag : tag.value if tag.key == "LaunchTemplateId"]) == "lt-0123456789abcdef0" &&
      one([for tag in aws_autoscaling_group.job["42"].tag : tag.propagate_at_launch if tag.key == "LaunchTemplateId"])
    )
    error_message = "The scheduler must use a dedicated exact-trust role, per-group liveness and resource-tagged EC2 permissions."
  }
}

run "two_groups_have_distinct_terminate_permissions" {
  command = plan
  variables {
    job_endpoints = {
      "42" = { gitlab_eni_id = "eni-00000000000000042" }
      "43" = { gitlab_eni_id = "eni-00000000000000042" }
    }
    groups = merge(var.groups, {
      "43" = merge(var.groups["42"], {
        tags             = ["adp-group-43", "test-ci"]
        token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab-runner/43-Ab12Cd"
      })
    })
  }

  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement) == 5 &&
      length(distinct([for statement in jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement : statement.Sid])) == 5 &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[3].Sid == "TerminateDetachedOwnedInstances42" &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[4].Sid == "TerminateDetachedOwnedInstances43" &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[3].Condition.StringEquals["ec2:ResourceTag/GroupId"] == "42" &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[4].Condition.StringEquals["ec2:ResourceTag/GroupId"] == "43" &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[3].Condition.StringEquals["ec2:ResourceTag/LaunchTemplateId"] == "lt-0123456789abcdef0" &&
      jsondecode(aws_iam_role_policy.reconciler[0].policy).Statement[4].Condition.StringEquals["ec2:ResourceTag/LaunchTemplateId"] == "lt-0123456789abcdef1"
    )
    error_message = "Reconciliation must not cross-match group ownership or duplicate IAM statement IDs."
  }
}

run "verified_group_binding" {
  command = plan

  assert {
    condition     = local.group_bindings["42"].manager_namespace == "gitlab-runner-42" && local.group_bindings["42"].job_namespace == "gitlab-jobs-42" && local.group_bindings["42"].token_secret_arn == var.groups["42"].token_secret_arn
    error_message = "Group identity must remain bound to its own manager, job namespace and reference-only token."
  }

  assert {
    condition     = data.terraform_remote_state.platform.config.key == "test/platform/terraform.tfstate" && data.aws_ssm_parameter.runner_url.name == "/adp/test/gitlab/runner-url" && data.aws_ssm_parameter.ca_secret_arn.name == "/adp/test/gitlab/ca-chain-secret-arn"
    error_message = "Discovery must only read platform state and private S1 metadata."
  }
}

run "reject_other_group_tag" {
  command = plan
  variables {
    groups = {
      "42" = merge(var.groups["42"], { tags = ["adp-group-42", "adp-group-43"] })
    }
  }
  expect_failures = [var.groups]
}

run "reject_wrong_tenant_path" {
  command = plan
  variables {
    groups = {
      "42" = merge(var.groups["42"], { path = "tenant-other" })
    }
  }
  expect_failures = [var.groups]
}

run "reject_other_group_secret" {
  command = plan
  variables {
    groups = {
      "42" = merge(var.groups["42"], { token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab-runner/43-Ab12Cd" })
    }
  }
  expect_failures = [var.groups]
}

run "reject_unprotected_policy" {
  command = plan
  variables {
    groups = {
      "42" = merge(var.groups["42"], { access_level = "instance_type" })
    }
  }
  expect_failures = [var.groups]
}

run "reject_noncanonical_group_id" {
  command = plan
  variables {
    groups = {
      "042" = merge(var.groups["42"], { tags = ["adp-group-042"], token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab-runner/042-Ab12Cd" })
    }
  }
  expect_failures = [var.groups]
}

run "reject_public_runner_url" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.runner_url
    values = { value = "https://gitlab.outside.test/gitlab" }
  }
  expect_failures = [terraform_data.discovery]
}

run "reject_missing_ca_reference" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.ca_secret_arn
    values = { value = "" }
  }
  expect_failures = [terraform_data.discovery]
}

run "reject_wrong_ca_secret" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.ca_secret_arn
    values = { value = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab/another-Ab12Cd" }
  }
  expect_failures = [terraform_data.discovery]
}

run "reject_other_platform_state" {
  command = plan
  variables {
    platform_state = {
      bucket = "example-state"
      key    = "test/modules/gitlab/terraform.tfstate"
      region = "us-east-1"
    }
  }
  expect_failures = [var.platform_state]
}

run "restricted_manager_per_group" {
  command = plan
  assert {
    condition = (
      kubernetes_namespace_v1.manager["42"].metadata[0].name == "gitlab-runner-42" &&
      kubernetes_namespace_v1.manager["42"].metadata[0].labels["pod-security.kubernetes.io/enforce"] == "restricted" &&
      kubernetes_service_account_v1.manager["42"].metadata[0].namespace == "gitlab-runner-42" &&
      jsondecode(aws_iam_role.manager["42"].assume_role_policy).Statement[0].Condition.StringEquals["${local.oidc_issuer}:sub"] == "system:serviceaccount:gitlab-runner-42:gitlab-runner"
    )
    error_message = "Manager needs its own restricted namespace and exclusively scoped IRSA trust."
  }
  assert {
    condition = (
      helm_release.manager["42"].namespace == "gitlab-runner-42" &&
      yamldecode(helm_release.manager["42"].values[0]).rbac.create == false &&
      yamldecode(helm_release.manager["42"].values[0]).runners.executor == "docker-autoscaler" &&
      helm_release.manager["42"].wait && helm_release.manager["42"].atomic
    )
    error_message = "The verified local chart must not gain Kubernetes job permissions."
  }
}

run "reject_unverified_manager_archive" {
  command = plan
  variables {
    runner_chart_archive = "tests/unverified-chart.tgz"
  }
  expect_failures = [helm_release.manager["42"]]
}

run "two_groups_have_distinct_runtime_resources" {
  command = plan
  variables {
    job_endpoints = merge(var.job_endpoints, {
      "43" = { gitlab_eni_id = "eni-00000000000000042" }
    })
    groups = {
      "42" = var.groups["42"]
      "43" = merge(var.groups["42"], {
        tenant_id        = "tenant-second"
        path             = "tenant-${substr(sha256("tenant-second"), 0, 24)}"
        tags             = ["adp-group-43"]
        token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab-runner/43-Ab12Cd"
      })
    }
  }
  assert {
    condition = (
      length(aws_autoscaling_group.job) == 2 &&
      aws_autoscaling_group.job["42"].name != aws_autoscaling_group.job["43"].name &&
      aws_autoscaling_group.job["42"].min_size == 0 &&
      aws_autoscaling_group.job["42"].desired_capacity == 0 &&
      aws_autoscaling_group.job["42"].max_size == 2 &&
      tostring(aws_launch_template.job["42"].network_interfaces[0].associate_public_ip_address) == "false" &&
      aws_launch_template.job["43"].metadata_options[0].http_tokens == "required"
    )
    error_message = "Every group needs a separate, bounded, private job ASG."
  }
  assert {
    condition = (
      strcontains(yamldecode(helm_release.manager["42"].values[0]).runners.config, "name = \"adp-test-gitlab-job-42\"") &&
      strcontains(yamldecode(helm_release.manager["42"].values[0]).runners.config, "Path = \"group/42\"") &&
      !strcontains(yamldecode(helm_release.manager["42"].values[0]).runners.config, "group/43") &&
      strcontains(yamldecode(helm_release.manager["43"].values[0]).runners.config, "group/43") &&
      yamldecode(helm_release.manager["42"].values[0]).runners.cache.disableGlobalCredentialsExport
    )
    error_message = "The actual manager chart values must bind each group to only its own ASG and cache."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.manager["42"].policy).Statement[4].Resource[0] == var.groups["42"].token_secret_arn &&
      !contains(jsondecode(aws_iam_role_policy.manager["42"].policy).Statement[4].Resource, var.groups["43"].token_secret_arn) &&
      jsondecode(aws_iam_role_policy.manager["42"].policy).Statement[6].Condition.StringLike["s3:prefix"] == ["group/42/", "group/42/*"] &&
      jsondecode(aws_iam_role_policy.manager["43"].policy).Statement[2].Condition.StringEquals["ec2:ResourceTag/aws:autoscaling:groupName"] == aws_autoscaling_group.job["43"].name
    )
    error_message = "Manager policy must use exact group token, cache prefix, and owned instance connector tag."
  }
}

run "refuse_incomplete_release" {
  command = plan
  override_data {
    target = data.external.release_ready
    values = { result = { ready = "false" } }
  }
  expect_failures = [terraform_data.release_ready]
}

run "qualified_network_only" {
  command = plan
  assert {
    condition = (
      length(aws_vpc_security_group_egress_rule.job_https) == 2 &&
      aws_vpc_security_group_egress_rule.job_https["42/gitlab"].cidr_ipv4 == "10.0.0.10/32" &&
      aws_vpc_security_group_egress_rule.job_https["42/cache"].cidr_ipv4 == "10.0.0.14/32" &&
      aws_vpc_endpoint.cache.vpc_endpoint_type == "Interface" &&
      !aws_vpc_endpoint.cache.private_dns_enabled &&
      length(aws_vpc_endpoint.cache.dns_options) == 0 &&
      jsondecode(aws_vpc_endpoint.cache.policy).Statement[0].Resource == [aws_s3_bucket.cache.arn, "${aws_s3_bucket.cache.arn}/*"] &&
      aws_vpc_security_group_ingress_rule.cache_manager.referenced_security_group_id == data.aws_security_group.eks_nodes.id &&
      alltrue([for rule in aws_vpc_security_group_egress_rule.job_https : rule.ip_protocol == "tcp" && rule.from_port == 443 && rule.to_port == 443]) &&
      aws_vpc_security_group_ingress_rule.manager_connector["42"].from_port == 22 &&
      aws_vpc_security_group_ingress_rule.manager_connector["42"].referenced_security_group_id == data.aws_security_group.eks_nodes.id &&
      aws_vpc_security_group_ingress_rule.manager_connector["42"].referenced_security_group_id == "sg-0123456789abcdef2" &&
      tostring(aws_launch_template.job["42"].network_interfaces[0].associate_public_ip_address) == "false" &&
      aws_launch_template.job["42"].metadata_options[0].http_tokens == "required" &&
      aws_launch_template.job["42"].metadata_options[0].http_put_response_hop_limit == 1 &&
      aws_launch_template.job["42"].metadata_options[0].http_protocol_ipv6 == "disabled" &&
      strcontains(yamldecode(helm_release.manager["42"].values[0]).runners.config, "extra_hosts = [\"gitlab.example.test:10.0.0.10\", \"s3.us-east-1.amazonaws.com:10.0.0.14\"]") &&
      yamldecode(helm_release.manager["42"].values[0]).hostAliases == [{ ip = "10.0.0.14", hostnames = ["s3.us-east-1.amazonaws.com"] }]
    )
    error_message = "Cache DNS must stay client-scoped while private endpoint policy, job routes and host-only IMDSv2 remain restricted."
  }
}

run "reject_missing_endpoints" {
  command = plan
  variables { job_endpoints = {} }
  expect_failures = [aws_security_group.job["42"]]
}

run "reject_wrong_vpc_endpoint" {
  command = plan
  override_data {
    target = data.aws_subnet.endpoint["42/gitlab"]
    values = { vpc_id = "vpc-00000000000000000", map_public_ip_on_launch = false }
  }
  expect_failures = [aws_security_group.job["42"]]
}

run "reject_public_job_subnet" {
  command = plan
  override_data {
    target = data.aws_subnet.job["subnet-0123456789abcdef0"]
    values = { vpc_id = "vpc-0123456789abcdef0", map_public_ip_on_launch = true }
  }
  expect_failures = [aws_security_group.job["42"]]
}

run "reject_metadata_endpoint" {
  command = plan
  override_data {
    target = data.aws_network_interface.endpoint["42/gitlab"]
    values = { private_ip = "169.254.169.254", subnet_id = "subnet-0123456789abcdef0", tags = { Component = "gitlab-runner-gitlab", Environment = "test", GroupId = "42" } }
  }
  expect_failures = [aws_security_group.job["42"]]
}

run "reject_aggregate_capacity_above_sixteen_vcpu" {
  command = plan
  variables {
    groups = {
      for offset in range(5) : tostring(offset + 42) => merge(var.groups["42"], {
        tags             = ["adp-group-${offset + 42}"]
        token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab-runner/${offset + 42}-Ab12Cd"
      })
    }
  }
  expect_failures = [var.groups]
}

run "reject_unrelated_alb_interface" {
  command = plan
  override_data {
    target = data.aws_network_interface.endpoint["42/gitlab"]
    values = { private_ip = "10.0.0.10", subnet_id = "subnet-0123456789abcdef0", description = "ELB app/other-alb/0123456789abcdef", security_groups = ["sg-0123456789abcdef1"] }
  }
  expect_failures = [aws_security_group.job["42"]]
}

run "reject_cross_group_gitlab_endpoint" {
  command = plan
  variables {
    groups = merge(var.groups, { "43" = merge(var.groups["42"], {
      tenant_id        = "tenant-second"
      path             = "tenant-${substr(sha256("tenant-second"), 0, 24)}"
      tags             = ["adp-group-43"]
      token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab-runner/43-Ab12Cd"
    }) })
    job_endpoints = merge(var.job_endpoints, {
      "43" = { gitlab_eni_id = "eni-00000000000000044" }
    })
  }
  expect_failures = [aws_security_group.job["42"], aws_security_group.job["43"]]
}

run "reject_unqualified_cache_interface" {
  command = plan
  override_data {
    target = data.aws_network_interface.endpoint["42/cache"]
    values = { private_ip = "10.0.0.14", subnet_id = "subnet-0123456789abcdef0", description = "VPC Endpoint Interface vpce-11111111111111111", security_groups = ["sg-0123456789abcdef2"] }
  }
  expect_failures = [aws_security_group.job["42"]]
}

run "reject_alb_interface_with_wrong_security_group" {
  command = plan
  override_data {
    target = data.aws_network_interface.endpoint["42/gitlab"]
    values = { private_ip = "10.0.0.10", subnet_id = "subnet-0123456789abcdef0", description = "ELB app/adp-test-gitlab-alb/0123456789abcdef", security_groups = ["sg-11111111111111111"] }
  }
  expect_failures = [aws_security_group.job["42"]]
}

run "reject_public_cache_endpoint_address" {
  command = plan
  override_data {
    target = data.aws_network_interface.endpoint["42/cache"]
    values = { private_ip = "8.8.8.8", subnet_id = "subnet-0123456789abcdef0", description = "VPC Endpoint Interface vpce-0123456789abcdef0", security_groups = ["sg-0123456789abcdef2"] }
  }
  expect_failures = [aws_security_group.job["42"]]
}

run "foundation_has_no_manager_or_unbound_runtime_policy" {
  command = plan
  variables {
    manager_activation_enabled   = false
    runtime_iam_policies_enabled = false
  }
  assert {
    condition = (
      length(helm_release.manager) == 0 &&
      length(kubernetes_namespace_v1.manager) == 1 &&
      length(kubernetes_service_account_v1.manager) == 1 &&
      length(kubernetes_config_map_v1.manager_loader) == 0 &&
      length(aws_iam_role_policy.manager) == 0 &&
      length(aws_iam_role_policy.reconciler) == 0 &&
      length(aws_iam_role.manager) == 1 &&
      aws_autoscaling_group.job["42"].desired_capacity == 0
    )
    error_message = "Foundation must leave all managers inactive, capacity at zero, and unbound IAM policies absent."
  }
}

run "bind_exact_policies_before_manager_activation" {
  command = plan
  variables {
    manager_activation_enabled   = false
    runtime_iam_policies_enabled = true
  }
  assert {
    condition = (
      length(helm_release.manager) == 0 &&
      length(aws_iam_role_policy.manager) == 1 &&
      length(aws_iam_role_policy.reconciler) == 1 &&
      jsondecode(aws_iam_role_policy.manager["42"].policy).Statement[0].Resource == aws_autoscaling_group.job["42"].arn
    )
    error_message = "Policy binding must use actual capacity identifiers while managers remain inactive."
  }
}

run "reject_activation_without_bound_runtime_policies" {
  command = plan
  variables {
    manager_activation_enabled   = true
    runtime_iam_policies_enabled = false
  }
  expect_failures = [var.manager_activation_enabled]
}

run "deployment_access_is_only_the_owned_manager_namespace" {
  command = plan
  variables {
    manager_activation_enabled   = false
    runtime_iam_policies_enabled = false
    deployment_role_arn          = "arn:aws:iam::123456789012:role/adp-test-gitlab-trusted-deployment"
  }
  assert {
    condition = (
      aws_eks_access_entry.deployment[0].principal_arn == var.deployment_role_arn &&
      aws_eks_access_policy_association.deployment[0].policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminPolicy" &&
      aws_eks_access_policy_association.deployment[0].access_scope[0].type == "namespace" &&
      aws_eks_access_policy_association.deployment[0].access_scope[0].namespaces == toset(["gitlab-runner-42"]) &&
      length(helm_release.manager) == 0
    )
    error_message = "Runtime access must remain inside the owned manager namespace and must not activate it."
  }
}

run "reject_foreign_deployment_principal" {
  command = plan
  variables {
    deployment_role_arn = "arn:aws:iam::999999999999:role/adp-test-gitlab-trusted-deployment"
  }
  expect_failures = [aws_eks_access_entry.deployment]
}

run "qualification_has_independent_shared_resources" {
  command = plan
  variables {
    qualification_run_id         = "4ed14edc-f4c3-4e42-b0e7-bc61466ab27f"
    manager_activation_enabled   = false
    runtime_iam_policies_enabled = false
  }
  assert {
    condition = (
      endswith(aws_s3_bucket.cache.bucket, "-q-4ed14edcf4c34e42") &&
      endswith(aws_security_group.cache_endpoint.name, "-q-4ed14edcf4c34e42") &&
      aws_iam_role.reconciler.name == "adp-test-gitlab-runners-reconciler-q-4ed14edcf4c34e42" &&
      length(aws_eks_access_entry.deployment) == 0 &&
      length(helm_release.manager) == 0 &&
      length(terraform_data.qualification_scope) == 1
    )
    error_message = "Qualification must own distinct shared resources without taking the production EKS access entry or activating managers."
  }
}

run "qualification_cannot_own_shared_eks_entry" {
  command = plan
  variables {
    qualification_run_id = "4ed14edc-f4c3-4e42-b0e7-bc61466ab27f"
    deployment_role_arn  = "arn:aws:iam::123456789012:role/adp-test-gitlab-trusted-deployment"
  }
  expect_failures = [terraform_data.qualification_scope]
}

run "root_volume_inherits_the_reviewed_ami_snapshot_size" {
  command = plan
  assert {
    condition     = one(one(aws_launch_template.job["42"].block_device_mappings).ebs).volume_size == 40
    error_message = "Do not shrink the approved AMI snapshot with a fixed launch-template root size."
  }
}

run "larger_reviewed_ami_sets_its_own_root_device_and_size" {
  command = plan
  override_data {
    target = data.aws_ami.job
    values = { root_device_name = "/dev/sda1", block_device_mappings = [{ device_name = "/dev/sda1", ebs = { volume_size = "80" } }] }
  }
  assert {
    condition = (
      one(aws_launch_template.job["42"].block_device_mappings).device_name == "/dev/sda1" &&
      one(one(aws_launch_template.job["42"].block_device_mappings).ebs).volume_size == 80 &&
      one(data.aws_ami.job.filter).name == "image-id" &&
      one(one(data.aws_ami.job.filter).values) == var.host_ami_id
    )
    error_message = "Read root metadata from the exact reviewed AMI; never select an image or shrink its snapshot."
  }
}
