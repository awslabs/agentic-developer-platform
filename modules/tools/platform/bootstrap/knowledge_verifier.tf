# The private Knowledge mediator verifies the facade assertion in the Gateway
# service. It never signs assertions and is distinct from the AgentCore target
# role. A resource policy on this key deliberately does not delegate Verify to
# account IAM. An identity policy alone is insufficient; provision its exact
# role grant together with an explicit key-policy principal.
variable "knowledge_mediator_role_arn" {
  description = "Existing Gateway service role that verifies Knowledge assertions. Required with the private knowledge endpoint; never the facade or AgentCore target role."
  type        = string
  default     = ""
  validation {
    condition = var.knowledge_mediator_role_arn == "" ? var.knowledge_door_endpoint == "" : (
      can(regex("^arn:aws:iam::${var.aws_account_id}:role/[A-Za-z0-9_+=,.@/-]+$", var.knowledge_mediator_role_arn)) &&
      var.knowledge_mediator_role_arn != var.tools_facade_role_arn &&
      var.knowledge_mediator_role_arn != "arn:aws:iam::${var.aws_account_id}:role/adp-tools-${var.environment}-gateway"
    )
    error_message = "A private knowledge endpoint requires the exact Gateway service IAM role in this account, distinct from the facade and AgentCore target roles."
  }
  validation {
    condition     = var.knowledge_mediator_role_arn == "" || var.knowledge_door_endpoint == "" || local.assertion_key_count == 1
    error_message = "Knowledge requires an independently configured facade, interceptor or credential assertion key; disabling Knowledge must retain that key."
  }
}

resource "aws_iam_role_policy" "knowledge_mediator_verify" {
  count = var.knowledge_door_endpoint == "" || local.assertion_key_count == 0 ? 0 : 1
  name  = "adp-tools-knowledge-mediator-verify"
  role  = element(reverse(split("/", var.knowledge_mediator_role_arn)), 0)
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "kms:Verify", Resource = aws_kms_key.assertion[0].arn
  }] })
}
