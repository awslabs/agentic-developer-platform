# Interface endpoints with private DNS receive task traffic even when the task
# subnet has NAT. Admit only this service's security group on HTTPS.
resource "aws_vpc_security_group_ingress_rule" "endpoints" {
  for_each                     = var.endpoint_security_group_ids
  security_group_id            = each.value
  referenced_security_group_id = aws_security_group.svc.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "HTTPS from Gbrain tasks to private AWS endpoints"
}
