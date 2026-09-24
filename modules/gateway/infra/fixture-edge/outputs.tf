# =============================================================================
# Outputs — Wave 2 fixture trusted edge (Issue #5836)
# =============================================================================
# Every output is bound to the run nonce / account / region, so an artifact
# captured from one run cannot be mistaken for another's.
#
# DELIBERATELY ABSENT: the provenance secret. It is available only via the
# SecureString SSM parameter (ssm_provenance_parameter_name below). Terraform
# outputs are written to state and echoed to the console, and #5836 requires
# secrets to stay out of logs and out of state artifacts published to GitHub. An
# output marked `sensitive` would still be plain text in state, so marking is not
# sufficient here — omission is.
# =============================================================================

output "fixture_edge_enabled" {
  description = "Whether this component created anything. False is the default and produces no resources."
  value       = var.fixture_edge_enabled
}

output "run_nonce" {
  description = "Run nonce every resource in this component is bound to."
  value       = var.run_nonce
}

output "rest_api_id" {
  description = "Fixture REST API id. Empty when disabled."
  value       = try(aws_api_gateway_rest_api.fixture[0].id, "")
}

output "worker_control_endpoint" {
  description = <<-EOT
    The exact value to set as ADP_AGENT_CONTROL_ENDPOINT on the fixture worker.

    Shape traced from lib/run_identity.py rather than assumed: the client appends
    "/bootstrap" to this base and rejects any endpoint that is not https with a
    bare host, so this must be the HTTPS execute-api URL and must already include
    the /internal/v1/agent path the pod registers its routes under.
  EOT
  value = try(
    "https://${aws_api_gateway_rest_api.fixture[0].id}.execute-api.${var.aws_region}.amazonaws.com/${aws_api_gateway_stage.fixture[0].stage_name}/internal/v1/agent",
    ""
  )
}

output "ssm_provenance_parameter_name" {
  description = <<-EOT
    SecureString parameter holding the fixture edge's provenance proof. The
    fixture gateway's BG_APIGW_PROVENANCE_SECRET must be populated from THIS
    parameter (aws ssm get-parameter --with-decryption), not from the ordinary
    gateway's path — otherwise the fixture would share production's secret and
    the two edges would no longer be isolated from each other.

    The NAME is safe to publish; the VALUE must never be echoed or committed.
  EOT
  value       = try(aws_ssm_parameter.fixture_provenance_secret[0].name, "")
}

output "ownership" {
  description = <<-EOT
    Ownership facts for the #3968 cleanup ledger. Recorded so teardown can delete
    ONLY what this run created: a name prefix alone is not ownership, so the
    server-assigned REST API id is included and must be re-verified (together
    with the AdpFixtureRun tag) before any delete.
  EOT
  value = {
    run_nonce   = var.run_nonce
    account_id  = var.expected_account_id
    region      = var.aws_region
    environment = var.environment
    rest_api_id = try(aws_api_gateway_rest_api.fixture[0].id, "")
    resources = var.fixture_edge_enabled ? [
      {
        kind    = "apigateway-rest-api"
        id      = try(aws_api_gateway_rest_api.fixture[0].id, "")
        name    = try(aws_api_gateway_rest_api.fixture[0].name, "")
        run_tag = var.run_nonce
        delete  = true
        verify  = "aws apigateway get-rest-api --rest-api-id <id> --query 'tags.AdpFixtureRun'"
      },
      {
        kind    = "ssm-parameter"
        id      = try(aws_ssm_parameter.fixture_provenance_secret[0].name, "")
        name    = try(aws_ssm_parameter.fixture_provenance_secret[0].name, "")
        run_tag = var.run_nonce
        delete  = true
        verify  = "aws ssm list-tags-for-resource --resource-type Parameter --resource-id <name>"
      },
      {
        kind    = "cloudwatch-log-group"
        id      = try(aws_cloudwatch_log_group.fixture[0].name, "")
        name    = try(aws_cloudwatch_log_group.fixture[0].name, "")
        run_tag = var.run_nonce
        delete  = true
        verify  = "aws logs list-tags-for-resource --resource-arn <arn>"
      },
    ] : []
  }
}
