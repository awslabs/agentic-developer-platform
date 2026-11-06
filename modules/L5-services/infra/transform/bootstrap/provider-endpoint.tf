variable "provider_endpoint" {
  description = "Optional private Transform endpoint. Empty authorizations deny all provider API calls during infrastructure qualification."
  type = object({
    vpc_id              = string
    subnet_ids          = list(string)
    private_dns_enabled = optional(bool, false)
    authorizations = optional(list(object({
      role_arn     = string
      package_arns = list(string)
    })), [])
  })
  default  = null
  nullable = true
  validation {
    condition = var.provider_endpoint == null ? true : (
      var.isolated_node_pool != null && length(var.provider_endpoint.subnet_ids) > 0 &&
      !var.provider_endpoint.private_dns_enabled &&
      alltrue([for authorization in var.provider_endpoint.authorizations : (
        can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$", authorization.role_arn)) &&
        length(authorization.package_arns) > 0 &&
        alltrue([for arn in authorization.package_arns : can(regex("^arn:aws:transform-custom:${var.aws_region}:[0-9]{12}:package/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$", arn))])
      )])
    )
    error_message = "Select a dedicated pool, subnets and exact regional provider role/package authorizations; VPC-wide private DNS must remain disabled."
  }
}

locals {
  provider_endpoint_enabled = var.provider_endpoint == null ? {} : { selected = var.provider_endpoint }
  provider_endpoint_ref = var.provider_endpoint == null ? null : {
    schema_version = 1
    account_id     = var.aws_account_id
    region         = var.aws_region
    service_name   = aws_vpc_endpoint.provider["selected"].service_name
    vpc_id         = aws_vpc_endpoint.provider["selected"].vpc_id
    endpoint_id    = aws_vpc_endpoint.provider["selected"].id
    dns_name = one([for entry in aws_vpc_endpoint.provider["selected"].dns_entry : entry.dns_name
    if can(regex("^vpce-[a-f0-9]{17}-[a-z0-9]+\\.transform-custom\\.${var.aws_region}\\.vpce\\.amazonaws\\.com$", entry.dns_name))])
  }
  provider_endpoint_policy = var.provider_endpoint == null ? null : length(var.provider_endpoint.authorizations) == 0 ? jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Deny", Principal = "*", Action = ["*"], Resource = ["*"] }
    ]
    }) : jsonencode({
    Version = "2012-10-17"
    Statement = flatten([for authorization in var.provider_endpoint.authorizations : [
      {
        Effect    = "Allow", Principal = { AWS = authorization.role_arn }
        Action    = ["transform-custom:GetTransformationPackageUrl", "transform-custom:ExecuteTransformation"]
        Resource  = authorization.package_arns
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Effect    = "Allow", Principal = { AWS = authorization.role_arn }
        Action    = ["transform-custom:ConverseStream"]
        Resource  = ["*"]
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      }
    ]])
  })
}

# Identity-only SG attached beside the existing cluster node SG. It grants no
# traffic itself; only this dedicated node identity may reach the endpoint SG.
resource "aws_security_group" "isolated_node_endpoint" {
  for_each    = local.provider_endpoint_enabled
  name        = "${local.isolated_pool_name}-endpoint-nodes"
  description = "Dedicated Transform node identity for private provider access"
  vpc_id      = each.value.vpc_id
  ingress     = []
  egress      = []
}

resource "aws_security_group" "provider_endpoint" {
  for_each    = local.provider_endpoint_enabled
  name        = "${local.isolated_pool_name}-provider-endpoint"
  description = "Private Transform provider endpoint from dedicated executor nodes only"
  vpc_id      = each.value.vpc_id
  egress      = []
}

resource "aws_vpc_security_group_ingress_rule" "provider_endpoint" {
  for_each                     = local.provider_endpoint_enabled
  security_group_id            = aws_security_group.provider_endpoint[each.key].id
  referenced_security_group_id = aws_security_group.isolated_node_endpoint[each.key].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "HTTPS from dedicated Transform nodes"
}

resource "aws_vpc_endpoint" "provider" {
  for_each            = local.provider_endpoint_enabled
  vpc_id              = each.value.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.transform-custom"
  vpc_endpoint_type   = "Interface"
  ip_address_type     = "ipv4"
  private_dns_enabled = each.value.private_dns_enabled
  subnet_ids          = each.value.subnet_ids
  security_group_ids  = [aws_security_group.provider_endpoint[each.key].id]
  policy              = local.provider_endpoint_policy
  tags                = { Name = "${local.isolated_pool_name}-provider" }
}

output "provider_endpoint_id" {
  value = try(aws_vpc_endpoint.provider["selected"].id, null)
}

output "provider_endpoint_network_interface_ids" {
  description = "Read these exact ENIs after apply, then pin their private IPv4 /32s in executor.provider_cidrs."
  value       = try(aws_vpc_endpoint.provider["selected"].network_interface_ids, [])
}

output "provider_endpoint_dns_entries" {
  value = try(aws_vpc_endpoint.provider["selected"].dns_entry, [])
}

output "provider_endpoint_ref" {
  description = "Protected installation reference from the actual AWS endpoint readback; never a Product/source-controlled option."
  value       = local.provider_endpoint_ref
}
