# One role per AWS account, shared by managed-private AgentCore targets. The
# foundation owner creates/adopts it; a tools release keeps its IAM mutation deny.
# Keep this enabled after first adoption even when a Knowledge target is disabled.
variable "enable_gateway_network_service_linked_role" {
  description = "Own the account's AgentCore Gateway network service-linked role in this bootstrap state. Retain an existing owner; import only a previously unmanaged role after review. Never adopt it into two environment states. Retained after target withdrawal."
  type        = bool
  default     = false
}

resource "aws_iam_service_linked_role" "gateway_network" {
  count            = var.enable_gateway_network_service_linked_role ? 1 : 0
  aws_service_name = "bedrock-agentcore.amazonaws.com"
  lifecycle {
    prevent_destroy = true
  }
}
