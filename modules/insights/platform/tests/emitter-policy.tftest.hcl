mock_provider "aws" {}
mock_provider "kubernetes" {}

variables {
  environment    = "dev"
  aws_region     = "us-east-1"
  aws_account_id = "000000000000"
  deployment_role_boundaries = {
    "arn:aws:iam::000000000000:role/adp-dev-insights-emitter"   = "arn:aws:iam::000000000000:policy/insights-emitter-ceiling"
    "arn:aws:iam::000000000000:role/adp-dev-insights-distiller" = "arn:aws:iam::000000000000:policy/insights-distiller-ceiling"
  }
}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      vpc_id                     = "vpc-00000000000000000"
      eks_cluster_endpoint       = "https://example.invalid"
      eks_cluster_ca_certificate = "Y2VydA=="
      eks_cluster_name           = "example-eks-cluster"
      eks_oidc_issuer            = "oidc.eks.us-east-1.amazonaws.com/id/example"
      eks_oidc_provider_arn      = "arn:aws:iam::000000000000:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/example"
    }
  }
}

override_resource {
  target = aws_s3_bucket.evidence
  values = {
    arn = "arn:aws:s3:::adp-dev-insights-evidence"
    id  = "adp-dev-insights-evidence"
  }
}

override_data {
  target = data.aws_prefix_list.s3
  values = { cidr_blocks = ["198.51.100.0/24"] }
}

override_data {
  target = data.aws_vpc_endpoint.sts
  values = {
    vpc_endpoint_type     = "Interface"
    private_dns_enabled   = true
    state                 = "available"
    network_interface_ids = ["eni-00000000000000001", "eni-00000000000000002"]
  }
}

override_data {
  target = data.aws_network_interface.sts["eni-00000000000000001"]
  values = { private_ip = "10.0.1.10" }
}

override_data {
  target = data.aws_network_interface.sts["eni-00000000000000002"]
  values = { private_ip = "10.0.2.10" }
}

run "service_writes_are_prefix_limited" {
  command = apply
  variables {
    environment    = "dev"
    aws_region     = "us-east-1"
    aws_account_id = "000000000000"
  }

  assert {
    condition = alltrue([for service, role in aws_iam_role.service :
      role.permissions_boundary == var.deployment_role_boundaries["arn:aws:iam::000000000000:role/adp-dev-insights-${service}"]
    ])
    error_message = "Each service must retain its exact operator-approved permissions boundary."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.service["emitter"].policy).Statement[1].Action == ["s3:PutObject"]
    error_message = "Emitter must have only PutObject permission on evidence objects."
  }
  assert {
    condition     = length(regexall("^${replace(jsondecode(aws_iam_role_policy.service["emitter"].policy).Statement[1].Resource, "*", ".*")}$", "arn:aws:s3:::adp-dev-insights-evidence/assurance/v1/team/insights/report.json")) > 0
    error_message = "Emitter must write evidence inside the Insights prefix."
  }
  assert {
    condition     = length(regexall("^${replace(jsondecode(aws_iam_role_policy.service["emitter"].policy).Statement[1].Resource, "*", ".*")}$", "arn:aws:s3:::adp-dev-insights-evidence/assurance/v1/team/other/report.json")) == 0
    error_message = "Emitter must not write evidence outside the Insights prefix."
  }
  assert {
    condition     = length(regexall("^${replace(jsondecode(aws_iam_role_policy.service["emitter"].policy).Statement[1].Resource, "*", ".*")}$", "arn:aws:s3:::another-bucket/assurance/v1/team/insights/report.json")) == 0
    error_message = "Emitter must not write into other buckets."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.service["distiller"].policy).Statement[1].Action == ["s3:PutObject"]
    error_message = "Distiller must not write outside Insights results."
  }
  assert {
    condition     = length(regexall("^${replace(jsondecode(aws_iam_role_policy.service["distiller"].policy).Statement[1].Resource, "*", ".*")}$", "arn:aws:s3:::adp-dev-insights-evidence/assurance/v1/team/other/report.json")) == 0
    error_message = "Distiller must not write into other evidence prefixes."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.service["distiller"].policy).Statement[3].Action == ["s3:GetObject"]
    error_message = "Distiller may only read other assurance evidence."
  }
  assert {
    condition = (jsondecode(aws_iam_role_policy.service["emitter"].policy).Statement[3].Action == ["s3:GetObject"] &&
    jsondecode(aws_iam_role_policy.service["emitter"].policy).Statement[3].Resource == "arn:aws:s3:::adp-dev-insights-evidence/assurance/v1/l5/insights/contributions/*")
    error_message = "L5 readback must be limited to the emitter's own contribution objects."
  }
  assert {
    condition     = !strcontains(aws_iam_role_policy.service["emitter"].policy, "s3:DeleteObject")
    error_message = "The receiver must retain versioned history and must not delete objects."
  }
}

run "services_satisfy_restricted_pod_security" {
  command = plan
  variables {
    environment     = "dev"
    aws_region      = "us-east-1"
    aws_account_id  = "000000000000"
    emitter_image   = "example.invalid/emitter@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    distiller_image = "example.invalid/distiller@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }

  assert {
    condition     = kubernetes_namespace_v1.insights.metadata[0].labels["pod-security.kubernetes.io/enforce"] == "restricted"
    error_message = "Insights must retain restricted pod admission."
  }
  assert {
    condition = length(kubernetes_deployment_v1.service) == 2 && alltrue([
      for deployment in kubernetes_deployment_v1.service :
      try(deployment.spec[0].template[0].spec[0].security_context[0].seccomp_profile[0].type == "RuntimeDefault", false)
    ])
    error_message = "Both Insights services must explicitly select RuntimeDefault seccomp to satisfy restricted pod admission."
  }
  assert {
    condition = alltrue([
      for service_account in kubernetes_service_account_v1.service :
      lookup(service_account.metadata[0].annotations, "eks.amazonaws.com/sts-regional-endpoints", "false") == "true"
    ])
    error_message = "Both service identities must use regional STS to reach the platform's private endpoint."
  }
  assert {
    condition     = alltrue([for deployment in kubernetes_deployment_v1.service : tonumber(deployment.spec[0].replicas) == 0])
    error_message = "Installation must leave unqualified consumers stopped."
  }
  assert {
    condition     = kubernetes_service_v1.l5_emitter[0].spec[0].type == "ClusterIP" && length(kubernetes_network_policy_v1.l5_emitter_ingress.spec[0].ingress[0].from) == 1
    error_message = "The receiver must be private and ingress must use one combined namespace/pod peer."
  }
  assert {
    condition = anytrue([
      for rule in kubernetes_network_policy_v1.allowed.spec[0].egress :
      toset(flatten([for peer in rule.to : [for block in peer.ip_block : block.cidr]])) == toset(["10.0.1.10/32", "10.0.2.10/32"]) &&
      length(rule.ports) == 1 && rule.ports[0].port == "443" && rule.ports[0].protocol == "TCP"
    ])
    error_message = "STS egress must allow only HTTPS to the platform endpoint's exact interface addresses."
  }
}

run "reject_sts_without_private_dns" {
  command = plan
  variables {
    environment    = "dev"
    aws_region     = "us-east-1"
    aws_account_id = "000000000000"
  }
  override_data {
    target = data.aws_vpc_endpoint.sts
    values = {
      vpc_endpoint_type     = "Interface"
      private_dns_enabled   = false
      state                 = "available"
      network_interface_ids = ["eni-00000000000000001"]
    }
  }
  expect_failures = [data.aws_vpc_endpoint.sts]
}

run "reject_sts_without_interfaces" {
  command = plan
  variables {
    environment    = "dev"
    aws_region     = "us-east-1"
    aws_account_id = "000000000000"
  }
  override_data {
    target = data.aws_vpc_endpoint.sts
    values = {
      vpc_endpoint_type     = "Interface"
      private_dns_enabled   = true
      state                 = "available"
      network_interface_ids = []
    }
  }
  expect_failures = [data.aws_vpc_endpoint.sts]
}

run "reject_missing_role_admission" {
  command = plan
  variables { deployment_role_boundaries = {} }
  expect_failures = [aws_iam_role.service]
}

run "reject_unlisted_service_role" {
  command = plan
  variables {
    deployment_role_boundaries = {
      "arn:aws:iam::000000000000:role/adp-prod-insights-emitter"  = "arn:aws:iam::000000000000:policy/insights-emitter-ceiling"
      "arn:aws:iam::000000000000:role/adp-dev-insights-distiller" = "arn:aws:iam::000000000000:policy/insights-distiller-ceiling"
    }
  }
  expect_failures = [aws_iam_role.service]
}

run "reject_missing_boundary" {
  command = plan
  variables {
    deployment_role_boundaries = {
      "arn:aws:iam::000000000000:role/adp-dev-insights-emitter"   = null
      "arn:aws:iam::000000000000:role/adp-dev-insights-distiller" = "arn:aws:iam::000000000000:policy/insights-distiller-ceiling"
    }
  }
  expect_failures = [var.deployment_role_boundaries]
}

run "reject_wildcard_boundary" {
  command = plan
  variables {
    deployment_role_boundaries = {
      "arn:aws:iam::000000000000:role/adp-dev-insights-emitter"   = "arn:aws:iam::000000000000:policy/*"
      "arn:aws:iam::000000000000:role/adp-dev-insights-distiller" = "arn:aws:iam::000000000000:policy/insights-distiller-ceiling"
    }
  }
  expect_failures = [var.deployment_role_boundaries]
}

run "reject_cross_account_boundary" {
  command = plan
  variables {
    deployment_role_boundaries = {
      "arn:aws:iam::000000000000:role/adp-dev-insights-emitter"   = "arn:aws:iam::000000000001:policy/insights-emitter-ceiling"
      "arn:aws:iam::000000000000:role/adp-dev-insights-distiller" = "arn:aws:iam::000000000000:policy/insights-distiller-ceiling"
    }
  }
  expect_failures = [aws_iam_role.service]
}
