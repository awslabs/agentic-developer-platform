# Optional network for environments without an existing execute-api endpoint.
# Do not enable wildcard execute-api private DNS: it shadows the platform's
# public REGIONAL API. AgentCore routingDomain preserves the API Host and TLS SNI
# while directing only this target through the endpoint-specific DNS name.
variable "managed_network" {
  description = "Explicit VPC and two or more private subnets for a dedicated Knowledge endpoint and AgentCore resource gateway; null reuses execute_api_vpc_endpoint_id."
  type = object({
    vpc_id     = string
    subnet_ids = list(string)
  })
  default = null
  validation {
    condition = var.managed_network == null ? true : (
      can(regex("^vpc-[a-f0-9]{8,17}$", var.managed_network.vpc_id)) &&
      length(var.managed_network.subnet_ids) >= 2 &&
      length(distinct(var.managed_network.subnet_ids)) == length(var.managed_network.subnet_ids) &&
      alltrue([for id in var.managed_network.subnet_ids : can(regex("^subnet-[a-f0-9]{8,17}$", id))]) &&
      var.execute_api_vpc_endpoint_id == ""
    )
    error_message = "Select either an existing endpoint or a managed VPC with at least two distinct private subnets."
  }
}

locals {
  manage_network              = var.enabled && var.managed_network != null
  execute_api_vpc_endpoint_id = local.manage_network ? aws_vpc_endpoint.knowledge[0].id : var.execute_api_vpc_endpoint_id
  routing_domains = local.manage_network ? [for entry in aws_vpc_endpoint.knowledge[0].dns_entry : entry.dns_name if can(regex(
    "^${aws_vpc_endpoint.knowledge[0].id}-[a-z0-9]+\\.execute-api\\.${var.region}\\.vpce\\.amazonaws\\.com$", entry.dns_name
  ))] : []
}

data "aws_subnet" "knowledge" {
  for_each = local.manage_network ? toset(var.managed_network.subnet_ids) : toset([])
  id       = each.value
}

resource "aws_security_group" "endpoint" {
  count       = local.manage_network ? 1 : 0
  name_prefix = "${var.name_prefix}-knowledge-endpoint-"
  description = "HTTPS from the dedicated Knowledge resource gateway only"
  vpc_id      = var.managed_network.vpc_id
}

resource "aws_security_group" "resource_gateway" {
  count       = local.manage_network ? 1 : 0
  name_prefix = "${var.name_prefix}-knowledge-routing-"
  description = "Knowledge AgentCore resource gateway; HTTPS to its endpoint only"
  vpc_id      = var.managed_network.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "endpoint" {
  count                        = local.manage_network ? 1 : 0
  security_group_id            = aws_security_group.endpoint[0].id
  referenced_security_group_id = aws_security_group.resource_gateway[0].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_egress_rule" "resource_gateway" {
  count                        = local.manage_network ? 1 : 0
  security_group_id            = aws_security_group.resource_gateway[0].id
  referenced_security_group_id = aws_security_group.endpoint[0].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_endpoint" "knowledge" {
  count               = local.manage_network ? 1 : 0
  vpc_id              = var.managed_network.vpc_id
  subnet_ids          = var.managed_network.subnet_ids
  service_name        = "com.amazonaws.${var.region}.execute-api"
  vpc_endpoint_type   = "Interface"
  ip_address_type     = "ipv4"
  private_dns_enabled = false
  security_group_ids  = [aws_security_group.endpoint[0].id]
  lifecycle {
    precondition {
      condition = (
        alltrue([for subnet in data.aws_subnet.knowledge : subnet.vpc_id == var.managed_network.vpc_id && !subnet.map_public_ip_on_launch]) &&
        length(distinct([for subnet in data.aws_subnet.knowledge : subnet.availability_zone_id])) == length(var.managed_network.subnet_ids)
      )
      error_message = "Knowledge endpoint subnets must belong to the selected VPC, disable public IP assignment and occupy distinct availability zones."
    }
  }
}

# A separate policy avoids the endpoint/API association dependency cycle. The
# endpoint, exact API resource policy and endpoint policy must all finish before
# publishing these bindings or enabling the AgentCore target.
resource "aws_vpc_endpoint_policy" "knowledge" {
  count           = local.manage_network ? 1 : 0
  vpc_endpoint_id = aws_vpc_endpoint.knowledge[0].id
  policy = local.retained_handoff ? data.external.retained_handoff[0].result.endpoint_policy : jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = var.gateway_role_arn }
      Action    = "execute-api:Invoke"
      Resource  = "${aws_api_gateway_rest_api.knowledge[0].execution_arn}/${var.stage}/POST${local.route}"
    }]
  })
}

output "managed_private_endpoint" {
  description = "Exact AgentCore target routing bindings; null for dormant or externally managed network."
  value = local.manage_network ? {
    vpc_id             = var.managed_network.vpc_id
    subnet_ids         = var.managed_network.subnet_ids
    security_group_ids = [aws_security_group.resource_gateway[0].id]
    endpoint_id        = aws_vpc_endpoint.knowledge[0].id
    routing_domain     = one(local.routing_domains)
  } : null
  precondition {
    condition     = !local.manage_network || length(local.routing_domains) == 1
    error_message = "Knowledge needs one regional endpoint-specific execute-api DNS name; do not substitute wildcard private DNS or a zonal endpoint."
  }
  depends_on = [
    aws_vpc_endpoint_policy.knowledge, aws_api_gateway_stage.knowledge, aws_iam_role_policy.invoke,
    aws_vpc_security_group_ingress_rule.endpoint, aws_vpc_security_group_egress_rule.resource_gateway
  ]
}
