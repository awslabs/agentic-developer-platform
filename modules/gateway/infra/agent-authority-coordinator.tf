# The gateway and scheduled engine must resolve issue numbers in the same repo.
# Empty remains unconfigured; an agent cannot supply a replacement repository.
resource "aws_ssm_parameter" "orchestration_dispatch_repo" {
  name   = "/adp/${var.environment}/gateway/orchestration-dispatch-repo"
  type   = "SecureString"
  key_id = aws_kms_key.secrets.arn
  value  = var.orchestration_dispatch_repo != "" ? var.orchestration_dispatch_repo : "disabled"
  tags   = local.common_tags
}
