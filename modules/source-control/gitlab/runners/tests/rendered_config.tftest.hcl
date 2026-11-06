mock_provider "aws" {
  mock_data "aws_ami" { defaults = { root_device_name = "/dev/xvda", block_device_mappings = [{ device_name = "/dev/xvda", ebs = { volume_size = "40" } }] } }
  mock_data "aws_subnet" { defaults = { vpc_id = "vpc-0123456789abcdef0", map_public_ip_on_launch = false } }
  mock_data "aws_security_group" { defaults = { vpc_id = "vpc-0123456789abcdef0" } }
  mock_data "aws_network_interface" { defaults = { subnet_id = "subnet-0123456789abcdef0" } }
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
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
  target          = aws_autoscaling_group.job["42"]
  override_during = plan
  values          = { arn = "arn:aws:autoscaling:us-east-1:123456789012:autoScalingGroup:group-42:autoScalingGroupName:adp-test-gitlab-job-42" }
}
override_resource {
  target          = aws_autoscaling_group.job["43"]
  override_during = plan
  values          = { arn = "arn:aws:autoscaling:us-east-1:123456789012:autoScalingGroup:group-43:autoScalingGroupName:adp-test-gitlab-job-43" }
}
mock_provider "kubernetes" {}
mock_provider "external" {
  mock_data "external" { defaults = { result = { ready = "true" } } }
}
override_data {
  target = data.terraform_remote_state.platform
  values = { outputs = {
    vpc_id                        = "vpc-0123456789abcdef0"
    private_subnet_ids            = ["subnet-0123456789abcdef0"]
    eks_security_group_id         = "sg-0123456789abcdef0"
    eks_cluster_security_group_id = "sg-0123456789abcdef2"
    eks_cluster_name              = "example-cluster"
    eks_cluster_endpoint          = "https://eks.example.test"
    eks_cluster_ca_certificate    = "Y2E="
    eks_oidc_provider_arn         = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
  } }
}
override_data {
  target = data.aws_ssm_parameter.runner_url
  values = { value = "https://gitlab.example.test/gitlab" }
}
override_data {
  target = data.aws_ssm_parameter.ca_secret_arn
  values = { value = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab/ca-chain-Ab12Cd" }
}
variables {
  environment      = "test"
  aws_region       = "us-east-1"
  host_ami_id      = "ami-0123456789abcdef0"
  job_image_digest = "registry.example.test/job@sha256:${sha256("synthetic offline fixture")}"
  job_endpoints = {
    "42" = { gitlab_eni_id = "eni-00000000000000042" }
    "43" = { gitlab_eni_id = "eni-00000000000000042" }
  }
  gitlab_private_zone = "example.test"
  platform_state      = { bucket = "example-state", key = "test/platform/terraform.tfstate", region = "us-east-1" }
  groups = {
    "43" = {
      tenant_id        = "tenant-second"
      path             = "tenant-${substr(sha256("tenant-second"), 0, 24)}"
      tags             = ["adp-group-43"]
      access_level     = "ref_protected"
      token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab-runner/43-Ab12Cd"
    }
    "42" = {
      tenant_id        = "tenant-example"
      path             = "tenant-${substr(sha256("tenant-example"), 0, 24)}"
      tags             = ["adp-group-42"]
      access_level     = "ref_protected"
      token_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gitlab-runner/42-Ab12Cd"
    }
  }
}
run "render_production_manager" {
  command = plan
  assert {
    condition = (
      length(data.helm_template.manager) == 2 &&
      var.groups["42"].token_secret_arn != var.groups["43"].token_secret_arn &&
      alltrue([
        for group_id in ["42", "43"] :
        toset(keys(nonsensitive(data.helm_template.manager[group_id].manifests))) == toset(["templates/configmap.yaml", "templates/deployment.yaml"]) &&
        yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).metadata.namespace == "gitlab-runner-${group_id}" &&
        yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.serviceAccountName == "gitlab-runner" &&
        yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].image == local.release_lock.manager_image &&
        helm_release.manager[group_id].values[0] == local.manager_values[group_id] &&
        yamldecode(local.manager_values[group_id]).runners.runUntagged == false &&
        strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["register-the-runner"], "RUN_UNTAGGED=\"\"") &&
        !strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["register-the-runner"], "--run-untagged=true") &&
        strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["register-the-runner"], "--name=\"gitlab-group-${group_id}\"") &&
        strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "name = \"adp-test-gitlab-job-${group_id}\"") &&
        strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "BucketName = \"adp-test-gitlab-runners-123456789012\"") &&
        strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "Path = \"group/${group_id}\"") &&
        !strcontains(nonsensitive(data.helm_template.manager[group_id].manifest), var.groups[group_id].token_secret_arn)
      ])
    )
    error_message = "The pinned chart must render distinct group managers/ASGs/cache with no credential-bearing Secret."
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

run "reject_incomplete_release" {
  command = plan
  override_data {
    target = data.external.release_ready
    values = { result = { ready = "false" } }
  }
  expect_failures = [terraform_data.release_ready]
}

run "exclude_other_group_from_real_chart_and_iam" {
  command = plan
  assert {
    condition = alltrue([
      for group_id in ["42", "43"] :
      alltrue([
        for other_id in ["42", "43"] :
        group_id == other_id || (
          !strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "name = \"adp-test-gitlab-job-${other_id}\"") &&
          !strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "Path = \"group/${other_id}\"") &&
          !strcontains(nonsensitive(data.helm_template.manager[group_id].manifest), var.groups[other_id].token_secret_arn) &&
          jsondecode(aws_iam_role.manager[group_id].assume_role_policy).Statement[0].Condition.StringEquals["${local.oidc_issuer}:sub"] != "system:serviceaccount:gitlab-runner-${other_id}:gitlab-runner" &&
          !contains(jsondecode(aws_iam_role_policy.manager[group_id].policy).Statement[4].Resource, var.groups[other_id].token_secret_arn) &&
          jsondecode(aws_iam_role_policy.manager[group_id].policy).Statement[0].Resource != aws_autoscaling_group.job[other_id].arn &&
          jsondecode(aws_iam_role_policy.manager[group_id].policy).Statement[2].Condition.StringEquals["ec2:ResourceTag/aws:autoscaling:groupName"] != aws_autoscaling_group.job[other_id].name &&
          !strcontains(jsondecode(aws_iam_role_policy.manager[group_id].policy).Statement[5].Resource, "/group/${other_id}/") &&
          !contains(jsondecode(aws_iam_role_policy.manager[group_id].policy).Statement[6].Condition.StringLike["s3:prefix"], "group/${other_id}/") &&
          !contains(jsondecode(aws_iam_role_policy.manager[group_id].policy).Statement[6].Condition.StringLike["s3:prefix"], "group/${other_id}/*")
        )
      ])
    ])
    error_message = "A real manager render or IAM identity must not authorize the other group's ASG, cache or token."
  }
}

run "reject_shared_runner_token" {
  command = plan
  variables {
    groups = {
      "42" = var.groups["42"]
      "43" = merge(var.groups["43"], { token_secret_arn = var.groups["42"].token_secret_arn })
    }
  }
  expect_failures = [var.groups]
}

run "launch_only_baked_fail_closed_host" {
  command = plan
  assert {
    condition = (
      length(aws_launch_template.job) == 2 &&
      alltrue([
        for group_id in ["42", "43"] :
        strcontains(base64decode(nonsensitive(aws_launch_template.job[group_id].user_data)), filesha256("${path.module}/templates/job-user-data.sh.tftpl")) &&
        strcontains(base64decode(nonsensitive(aws_launch_template.job[group_id].user_data)), "--allow") &&
        aws_launch_template.job[group_id].metadata_options[0].http_tokens == "required" &&
        aws_launch_template.job[group_id].metadata_options[0].http_protocol_ipv6 == "disabled"
      ])
    )
    error_message = "Every group must boot through the reviewed fail-closed host entry point."
  }
}

run "reject_unsafe_real_chart_executor_configuration" {
  command = plan
  assert {
    condition = alltrue([
      for group_id in ["42", "43"] :
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "debug_trace_disabled = true") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "allowed_images = [\"${var.job_image_digest}\"]") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "user = \"10001:10001\"") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "services_limit = 0") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "cap_drop = [\"ALL\"]") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "security_opt = [\"no-new-privileges:true\"]") &&
      !strcontains(nonsensitive(data.helm_template.manager[group_id].manifest), "/var/run/docker.sock")
    ])
    error_message = "The real pinned chart must retain non-root, non-privileged, exact-image job restrictions."
  }
}

run "verify_rendered_job_admission_path" {
  command = plan
  assert {
    condition = alltrue([
      for group_id in ["42", "43"] :
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "executor = \"docker-autoscaler\"") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "allowed_images = [\"${var.job_image_digest}\"]") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "user = \"10001:10001\"") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "services_limit = 0") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "cap_drop = [\"ALL\"]") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "security_opt = [\"no-new-privileges:true\"]") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "extra_hosts = [\"gitlab.example.test:${data.aws_network_interface.endpoint["${group_id}/gitlab"].private_ip}\", \"s3.us-east-1.amazonaws.com:${data.aws_network_interface.endpoint["${group_id}/cache"].private_ip}\"]") &&
      !strcontains(nonsensitive(data.helm_template.manager[group_id].manifest), "/var/run/docker.sock") &&
      strcontains(base64decode(nonsensitive(aws_launch_template.job[group_id].user_data)), "test \"$(sha256sum /usr/local/libexec/adp-job-host | cut -d' ' -f1)\" = \"${filesha256("${path.module}/templates/job-user-data.sh.tftpl")}\"") &&
      strcontains(base64decode(nonsensitive(aws_launch_template.job[group_id].user_data)), "systemctl start adp-job-observer.service\n/usr/local/libexec/adp-job-host --allow \"${data.aws_network_interface.endpoint["${group_id}/gitlab"].private_ip}\" \"${data.aws_network_interface.endpoint["${group_id}/cache"].private_ip}\"") &&
      strcontains(base64decode(nonsensitive(aws_launch_template.job[group_id].user_data)), "systemctl stop adp-job-observer.service\nsystemctl restart adp-job-firewall.service\nsystemctl restart docker.service\nsystemctl start adp-job-observer.service\ntest \"$(sha256sum /usr/local/libexec/adp-job-images | cut -d' ' -f1)\" = \"${filesha256("${path.module}/bootstrap/verify_images.py")}\"\npython3 /usr/local/libexec/adp-job-images '${var.job_image_digest}' '${try(local.release_lock.helper_image, "")}'\n/usr/local/libexec/adp-job-host --ready\nsystemctl restart sshd.service\n/usr/local/libexec/adp-job-host --verify")
    ])
    error_message = "The real chart's restricted job configuration must match the checked host script, group endpoints and deny-before-SSH boot order."
  }
  assert {
    condition = (
      strcontains(file("${path.module}/templates/job-user-data.sh.tftpl"), "chain forward {\n    type filter hook forward priority -150; policy drop;") &&
      strcontains(file("${path.module}/templates/job-user-data.sh.tftpl"), "chain output {\n    type filter hook output priority -150; policy drop;") &&
      strcontains(file("${path.module}/templates/job-user-data.sh.tftpl"), "ip daddr 169.254.169.254 drop") &&
      strcontains(file("${path.module}/templates/job-user-data.sh.tftpl"), "ip6 daddr fd00:ec2::254 drop") &&
      strcontains(file("${path.module}/templates/job-user-data.sh.tftpl"), "Before=network-pre.target docker.service sshd.service") &&
      strcontains(file("${path.module}/templates/job-user-data.sh.tftpl"), "ExecStartPre=/usr/local/libexec/adp-job-host --verify")
    )
    error_message = "The baked entry point must deny forwarded traffic and metadata before SSH admission."
  }
}

run "verify_one_use_manager_restart_gate" {
  command = plan
  assert {
    condition = alltrue([
      for group_id in ["42", "43"] :
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.strategy.type == "Recreate" &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[1].name == "manager-guard" &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[1].image == try(local.release_lock.bootstrap_image, "") &&
      strcontains(join(" ", yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[1].readinessProbe.exec.command), "('alive','lease')") &&
      strcontains(join(" ", yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[1].readinessProbe.exec.command), "(p/'ack').read_text() == (p/'request').read_text()") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["pre-entrypoint-script"], "/runner-guard/ack") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "max_use_count = 1") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "capacity_per_instance = 1") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "max_instances = 2") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "idle_count = 0")
    ])
    error_message = "The shipped manager must drain its own previous one-use hosts before serving jobs."
  }
}

run "render_shared_gitlab_and_exact_cache_route" {
  command = plan
  assert {
    condition = (
      var.job_endpoints["42"].gitlab_eni_id == var.job_endpoints["43"].gitlab_eni_id &&
      data.aws_lb.gitlab.name == "adp-test-gitlab-alb" &&
      length(aws_vpc_security_group_egress_rule.job_https) == 4 &&
      alltrue([for group_id in ["42", "43"] :
        aws_vpc_security_group_egress_rule.job_https["${group_id}/gitlab"].cidr_ipv4 == "10.0.0.10/32" &&
        aws_vpc_security_group_egress_rule.job_https["${group_id}/cache"].cidr_ipv4 == "10.0.0.14/32" &&
        !aws_vpc_endpoint.cache.private_dns_enabled &&
        try(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.hostAliases, []) == [{ ip = data.aws_network_interface.endpoint["${group_id}/cache"].private_ip, hostnames = ["s3.us-east-1.amazonaws.com"] }] &&
        strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "ServerAddress = \"s3.us-east-1.amazonaws.com\"") &&
        strcontains(base64decode(nonsensitive(aws_launch_template.job[group_id].user_data)), "\"10.0.0.14\" \"s3.us-east-1.amazonaws.com\" >> /etc/hosts") &&
        strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "PathStyle = true") &&
        strcontains(base64decode(nonsensitive(aws_launch_template.job[group_id].user_data)), "s3.us-east-1.amazonaws.com") &&
        !strcontains(base64decode(nonsensitive(aws_launch_template.job[group_id].user_data)), "\"registry.example.test\" >> /etc/hosts") &&
        length(regexall(">> /etc/hosts", base64decode(nonsensitive(aws_launch_template.job[group_id].user_data)))) == 2 &&
        !strcontains(base64decode(nonsensitive(aws_launch_template.job[group_id].user_data)), "10.0.0.11") &&
        strcontains(base64decode(nonsensitive(aws_launch_template.job[group_id].user_data)), "/usr/local/libexec/adp-job-host --allow \"10.0.0.10\" \"10.0.0.14\"")
      ])
    )
    error_message = "Both isolated managers must reach only the actual shared GitLab ALB and private cache interface."
  }
}

run "verified_plugin_before_registration" {
  command = plan
  assert {
    condition = alltrue([for group_id in ["42", "43"] :
      length(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers) == 2 &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[0].name == "manager-plugin" &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[1].name == "manager-credentials" &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[0].image == try(local.release_lock.bootstrap_image, "") &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[0].command == ["python3", "/opt/manager/install_plugin.py"] &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[0].args == ["/opt/fleeting/fleeting-plugin-aws", "/runner-plugin", local.release_lock.fleeting_plugin.sha256] &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[0].securityContext.runAsUser == 10001 &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[0].securityContext.readOnlyRootFilesystem &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[0].securityContext.capabilities.drop == ["ALL"] &&
      contains([for mount in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[0].volumeMounts : mount.mountPath if mount.name == "manager-plugin" && !try(mount.readOnly, false)], "/runner-plugin") &&
      contains([for mount in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].volumeMounts : mount.mountPath if mount.name == "manager-plugin" && mount.readOnly], "/runner-plugin") &&
      contains([for volume in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.volumes : volume.name if try(volume.emptyDir.medium, "") == "Memory"], "manager-plugin") &&
      contains([for env in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].env : env.name if try(env.value, "") == "/runner-plugin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"], "PATH") &&
      kubernetes_config_map_v1.manager_plugin_installer[group_id].data["install_plugin.py"] == file("${path.module}/bootstrap/install_plugin.py") &&
      local.release_lock.plugin_installer_sha256 == filesha256("${path.module}/bootstrap/install_plugin.py")
    ])
    error_message = "Only the reviewed connector may be copied before manager registration and read from Runner's executable PATH."
  }
}

run "runtime_tls_init_path" {
  command = plan
  assert {
    condition = alltrue([
      for group_id in ["42", "43"] :
      length(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers) == 2 &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[1].name == "manager-credentials" &&
      yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[1].securityContext.runAsNonRoot &&
      contains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[1].args, "adp/test/gitlab-runner/${group_id}") &&
      contains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers[1].args, "--system-id-dir") &&
      contains([for env in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].env : env.name if try(env.value, "") == "/home/gitlab-runner"], "HOME") &&
      contains([for mount in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].volumeMounts : mount.mountPath if mount.name == "etc-gitlab-runner" && !try(mount.readOnly, false)], "/home/gitlab-runner/.gitlab-runner") &&
      contains([for mount in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].volumeMounts : mount.mountPath], "/secrets") &&
      contains([for mount in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].volumeMounts : mount.mountPath], "/home/gitlab-runner/.gitlab-runner/certs") &&
      contains([for env in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].env : env.name if try(coalesce(env.value, "unused") == "/secrets/ca.pem", false)], "CI_SERVER_TLS_CA_FILE") &&
      !contains([for env in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].env : env.name], "GIT_SSL_NO_VERIFY") &&
      !contains([for env in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].env : env.name], "CI_SERVER_TLS_SKIP_VERIFY") &&
      contains([for volume in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.volumes : volume.name if try(volume.emptyDir.medium, "") == "Memory"], "manager-secrets") &&
      contains([for volume in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.volumes : volume.name if try(volume.emptyDir.medium, "") == "Memory"], "manager-certs") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "tls-ca-file = \"/secrets/ca.pem\"") &&
      !strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "tls-skip-verify") &&
      !strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "GIT_SSL_NO_VERIFY") &&
      kubernetes_config_map_v1.manager_loader[group_id].data["load_credentials.py"] == file("${path.module}/bootstrap/load_credentials.py") &&
      !strcontains(nonsensitive(data.helm_template.manager[group_id].manifest), var.groups[group_id].token_secret_arn)
    ])
    error_message = "Runner must receive a non-root, memory-only verified token and CA before chart registration."
  }
}

run "runtime_registration_receipt" {
  command = plan
  assert {
    condition = alltrue([
      for group_id in ["42", "43"] :
      contains([for mount in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[1].volumeMounts : mount.mountPath if try(mount.readOnly, false)], "/runner-config") &&
      contains([for mount in yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[1].volumeMounts : mount.mountPath if try(mount.readOnly, false)], "/runner-credentials") &&
      strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["pre-entrypoint-script"], "/entrypoint verify --url= >/dev/null 2>&1") &&
      aws_ssm_parameter.registration_receipt[group_id].value == "pending" &&
      contains(jsondecode(aws_iam_role_policy.manager[group_id].policy).Statement[3].Resource,
      "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gitlab-runners/${group_id}/registration-receipt") &&
      !strcontains(nonsensitive(data.helm_template.manager[group_id].manifest), var.groups[group_id].token_secret_arn)
    ])
    error_message = "Manager registration must be verified against the init receipt before non-secret group receipt publication."
  }
}

run "render_private_clone_and_transfer_routes" {
  command = plan
  assert {
    condition = (
      local.private_gitlab_hostname == "gitlab.example.test" &&
      data.aws_ssm_parameter.runner_url.value == "https://gitlab.example.test/gitlab" &&
      alltrue([for group_id in ["42", "43"] :
        yamldecode(helm_release.manager[group_id].values[0]).gitlabUrl == data.aws_ssm_parameter.runner_url.value &&
        strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "clone_url = \"https://gitlab.example.test/gitlab\"") &&
        strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "tls-ca-file = \"/secrets/ca.pem\"") &&
        strcontains(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/configmap.yaml"])).data["config.template.toml"], "extra_hosts = [\"gitlab.example.test:10.0.0.10\", \"s3.us-east-1.amazonaws.com:10.0.0.14\"]") &&
        aws_vpc_security_group_egress_rule.job_https["${group_id}/gitlab"].cidr_ipv4 == "10.0.0.10/32" &&
        data.aws_network_interface.endpoint["${group_id}/gitlab"].description == "ELB ${data.aws_lb.gitlab.arn_suffix}"
      ])
    )
    error_message = "Both rendered managers must use private HTTPS for checkout and API routes; jobs must map that host to the verified shared ALB only."
  }
}

run "manager_pods_fit_quota" {
  command = plan
  assert {
    condition = alltrue([for group_id in ["42", "43"] :
      kubernetes_resource_quota_v1.manager[group_id].spec[0].hard["pods"] == "2" && kubernetes_resource_quota_v1.manager[group_id].spec[0].hard["requests.cpu"] == "2" && kubernetes_resource_quota_v1.manager[group_id].spec[0].hard["requests.memory"] == "4Gi" &&
      try(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers[0].resources, {}) == {
        requests = { cpu = "500m", memory = "512Mi" }, limits = { cpu = "1", memory = "1Gi" }
      } &&
      alltrue([for container in concat(
        slice(yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers, 1, 2),
        yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers
      ) : try(container.resources, {}) == { requests = { cpu = "100m", memory = "128Mi" }, limits = { cpu = "250m", memory = "256Mi" } }]) &&
      2 * sum([for container in concat(
        yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers,
        yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers
      ) : tonumber(trimsuffix(try(container.resources.requests.cpu, "0m"), "m"))]) <= 1000 * tonumber(kubernetes_resource_quota_v1.manager[group_id].spec[0].hard["requests.cpu"]) &&
      2 * sum([for container in concat(
        yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.containers,
        yamldecode(nonsensitive(data.helm_template.manager[group_id].manifests["templates/deployment.yaml"])).spec.template.spec.initContainers
      ) : tonumber(trimsuffix(try(container.resources.requests.memory, "0Mi"), "Mi"))]) <= 1024 * tonumber(trimsuffix(kubernetes_resource_quota_v1.manager[group_id].spec[0].hard["requests.memory"], "Gi"))
    ])
    error_message = "Both chart-rendered pods must request and limit resources for Runner, guard and both init containers within the namespace quota."
  }
}
