# Deployment discovers private STS endpoint groups in the selected VPC. Private
# DNS sends ordinary SDK calls to these interfaces instead of the public API.
variable "sts_endpoint_security_group_ids" {
  type        = set(string)
  default     = []
  description = "Security groups of regional STS endpoints with private DNS, discovered by the deployment scripts. Empty for VPCs without private STS."
  validation {
    condition     = alltrue([for id in var.sts_endpoint_security_group_ids : can(regex("^sg-[0-9a-f]{8,17}$", id))])
    error_message = "Supply exact STS endpoint security group IDs, not CIDRs."
  }
}

resource "aws_vpc_security_group_ingress_rule" "sts_from_guest" {
  for_each                     = local.gateway_execute_arn != "" ? var.sts_endpoint_security_group_ids : toset([])
  security_group_id            = each.key
  referenced_security_group_id = aws_security_group.guest.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "Dev Box HTTPS to private AWS STS endpoint"
  tags                         = local.tags
}
