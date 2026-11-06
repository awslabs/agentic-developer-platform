locals {
  job_subnet_ids = toset(data.terraform_remote_state.platform.outputs.private_subnet_ids)
  endpoint_interfaces = merge([
    for group_id, endpoints in var.job_endpoints : {
      "${group_id}/gitlab" = endpoints.gitlab_eni_id
      "${group_id}/cache"  = one(aws_vpc_endpoint.cache.network_interface_ids)
    }
  ]...)
}

data "aws_lb" "gitlab" {
  name = "adp-${var.environment}-gitlab-alb"
}

resource "aws_security_group" "cache_endpoint" {
  name   = "adp-${var.environment}-gitlab-runner-cache-endpoint${local.qualification_suffix}"
  vpc_id = data.terraform_remote_state.platform.outputs.vpc_id
  tags   = { Component = "gitlab-runner-cache", Environment = var.environment }
}

resource "aws_vpc_security_group_ingress_rule" "cache_endpoint" {
  for_each                     = local.group_bindings
  security_group_id            = aws_security_group.cache_endpoint.id
  referenced_security_group_id = aws_security_group.job[each.key].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_ingress_rule" "cache_manager" {
  security_group_id            = aws_security_group.cache_endpoint.id
  referenced_security_group_id = data.aws_security_group.eks_nodes.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_endpoint" "cache" {
  vpc_id              = data.terraform_remote_state.platform.outputs.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = false
  subnet_ids          = [sort(tolist(local.job_subnet_ids))[0]]
  security_group_ids  = [aws_security_group.cache_endpoint.id]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"]
      Resource  = [aws_s3_bucket.cache.arn, "${aws_s3_bucket.cache.arn}/*"]
    }]
  })
  tags = { Component = "gitlab-runner-cache", Environment = var.environment }
}

data "aws_network_interface" "endpoint" {
  for_each = local.endpoint_interfaces
  id       = each.value
}

data "aws_subnet" "endpoint" {
  for_each = data.aws_network_interface.endpoint
  id       = each.value.subnet_id
}

data "aws_subnet" "job" {
  for_each = local.job_subnet_ids
  id       = each.value
}

data "aws_security_group" "eks_nodes" {
  # Managed and Karpenter nodes use the cluster group. The additional EKS
  # control-plane group is not necessarily attached to their network interfaces.
  id = data.terraform_remote_state.platform.outputs.eks_cluster_security_group_id
}

resource "aws_security_group" "job" {
  for_each    = local.group_bindings
  name        = "adp-${var.environment}-gitlab-job-${each.key}"
  description = "Dedicated disposable job hosts for GitLab group ${each.key}"
  vpc_id      = data.terraform_remote_state.platform.outputs.vpc_id
  tags        = { Component = "gitlab-runner", Environment = var.environment, GroupId = each.key }

  lifecycle {
    precondition {
      condition = contains(keys(var.job_endpoints), each.key) && alltrue([
        for subnet in data.aws_subnet.job :
        subnet.vpc_id == data.terraform_remote_state.platform.outputs.vpc_id && !subnet.map_public_ip_on_launch
      ]) && data.aws_security_group.eks_nodes.vpc_id == data.terraform_remote_state.platform.outputs.vpc_id
      error_message = "Job subnets and cluster node security group must be private and in the EKS VPC."
    }
    precondition {
      condition = try(
        data.aws_lb.gitlab.internal &&
        data.aws_lb.gitlab.load_balancer_type == "application" &&
        data.aws_lb.gitlab.vpc_id == data.terraform_remote_state.platform.outputs.vpc_id &&
        length(setsubtract(toset(data.aws_lb.gitlab.subnets), local.job_subnet_ids)) == 0 &&
        length(distinct([for endpoints in var.job_endpoints : endpoints.gitlab_eni_id])) == 1 &&
        alltrue([for service in ["gitlab", "cache"] :
          data.aws_subnet.endpoint["${each.key}/${service}"].vpc_id == data.terraform_remote_state.platform.outputs.vpc_id &&
          !data.aws_subnet.endpoint["${each.key}/${service}"].map_public_ip_on_launch &&
          can(regex("^(10\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|192\\.168\\.)", data.aws_network_interface.endpoint["${each.key}/${service}"].private_ip))
        ]) &&
        data.aws_network_interface.endpoint["${each.key}/gitlab"].description == "ELB ${data.aws_lb.gitlab.arn_suffix}" &&
        length(setintersection(toset(data.aws_network_interface.endpoint["${each.key}/gitlab"].security_groups), toset(data.aws_lb.gitlab.security_groups))) > 0 &&
        contains(data.aws_lb.gitlab.subnets, data.aws_network_interface.endpoint["${each.key}/gitlab"].subnet_id) &&
        data.aws_network_interface.endpoint["${each.key}/cache"].description == "VPC Endpoint Interface ${aws_vpc_endpoint.cache.id}" &&
        contains(data.aws_network_interface.endpoint["${each.key}/cache"].security_groups, aws_security_group.cache_endpoint.id) &&
        length(distinct([for service in ["gitlab", "cache"] : data.aws_network_interface.endpoint["${each.key}/${service}"].private_ip])) == 2,
      false)
      error_message = "GitLab must be the shared internal S1 ALB; the private S3 cache endpoint must be distinct, qualified and in the EKS VPC."
    }
  }
}

resource "aws_vpc_security_group_egress_rule" "job_https" {
  for_each          = local.endpoint_interfaces
  security_group_id = aws_security_group.job[split("/", each.key)[0]].id
  cidr_ipv4         = "${data.aws_network_interface.endpoint[each.key].private_ip}/32"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "manager_connector" {
  for_each                     = local.group_bindings
  security_group_id            = aws_security_group.job[each.key].id
  referenced_security_group_id = data.aws_security_group.eks_nodes.id
  ip_protocol                  = "tcp"
  from_port                    = 22
  to_port                      = 22
}
