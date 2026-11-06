# The same reviewed rollout input configures the scheduled dispatcher and its
# gateway read model. This parameter is deployment configuration, never inferred
# from a flow, user request or browser session. Existing deployments must retain
# their accepted value explicitly when adopting this input.
resource "aws_ssm_parameter" "product_story_dispatch_enabled" {
  name  = "/adp/${var.environment}/gateway/product-story-dispatch-enabled"
  type  = "String"
  value = tostring(var.orchestration_product_story_dispatch_enabled)
}
