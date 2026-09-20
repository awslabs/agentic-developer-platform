# The standard gateway renderer consumes this in CI and self-managed installs.
# The optional feature-agent-models parameter can still override UI visibility.
resource "aws_ssm_parameter" "persona_model_mapping_enabled" {
  name  = "/adp/${var.environment}/gateway/persona-model-mapping-enabled"
  type  = "String"
  value = tostring(var.persona_model_mapping_enabled)
}
