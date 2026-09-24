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
    An INVENTORY RECEIPT of what this run's state owns — not a delete authority.

    That distinction is the correction to the previous revision. It enumerated three
    resources with name/tag-derived `verify` commands, implying teardown could work
    by looking resources up and deleting them. It cannot, safely: a name or a tag
    cannot distinguish the object this run created from a same-named replacement
    somebody else created afterwards, so post-hoc name-derived deletion is not
    replacement-safe. It also under-counted, omitting the policy, deployment and
    stage.

    HOW EACH CLASS OF RESOURCE IS ACTUALLY TORN DOWN
      * These Terraform-owned resources — by `terraform destroy` against the
        ISOLATED per-run state file, reviewed as an exact plan first
        (scripts/fixture-lifecycle.sh destroy). State is a stronger ownership record
        than a name prefix: it holds the server-assigned ids of the exact objects
        this run created.
      * The fixture Ingress (and therefore its ALB) and the fixture Secret — by
        #3968's `90-cleanup-ledger.sh`, uid-gated. They are Kubernetes objects, so
        its existing `k8s` ledger bucket already covers them and no new cleanup
        type was required.

    ORDERING: the edge must be destroyed BEFORE the fixture Ingress. main.tf reads
    the fixture ALB, and Terraform re-reads data sources during destroy, so removing
    the ALB first makes the destroy unplannable (verified on Terraform 1.15.3).

    This receipt exists so a human reading the ledger can see that these resources
    exist and how they are removed. The `verify` field is an EXISTENCE probe for
    confirming absence after teardown — deliberately not a "prove it is mine and
    delete it" instruction.
  EOT
  value = {
    run_nonce   = var.run_nonce
    account_id  = var.expected_account_id
    region      = var.aws_region
    environment = var.environment
    rest_api_id = try(aws_api_gateway_rest_api.fixture[0].id, "")

    # How the objects in `resources` are removed, stated in the receipt itself so
    # it cannot be read as authorising a name-based sweep.
    teardown = {
      mechanism       = "terraform destroy against the isolated per-run state key"
      state_key       = "fixture-edge/${var.environment}/${var.expected_account_id}/${var.run_nonce}/terraform.tfstate"
      command         = "scripts/fixture-lifecycle.sh destroy --nonce ${var.run_nonce} --account-id ${var.expected_account_id}"
      must_run_before = "the fixture Ingress/ALB deletion — this edge READS that ALB, so removing it first blocks the destroy plan"
      not_supported   = "deletion by name or tag prefix: it cannot distinguish this run's object from a later same-named replacement"
    }

    # Kubernetes objects this component's tooling creates, recorded in #3968's
    # ledger with server-assigned uids at creation time. Listed here for
    # completeness; the ledger, not this output, is their record.
    ledger_owned_k8s = [
      {
        kind      = "Ingress"
        note      = "created by scripts/create-fixture-alb.sh; deleting it removes the fixture ALB"
        recorded  = "ownership.py record-k8s --kind Ingress --uid <server-assigned>"
        delete_by = "#3968 90-cleanup-ledger.sh (uid-gated)"
      },
      {
        kind      = "Secret"
        note      = "per-run provenance value; created and ATTACHED by scripts/fixture-lifecycle.sh handoff"
        recorded  = "ownership.py record-k8s --kind Secret --uid <server-assigned>"
        delete_by = "#3968 90-cleanup-ledger.sh (uid-gated)"
      },
    ]

    resources = var.fixture_edge_enabled ? [
      {
        kind   = "apigateway-rest-api"
        id     = try(aws_api_gateway_rest_api.fixture[0].id, "")
        name   = try(aws_api_gateway_rest_api.fixture[0].name, "")
        verify = "aws apigateway get-rest-api --rest-api-id ${try(aws_api_gateway_rest_api.fixture[0].id, "")} # EXPECT NotFoundException after teardown"
      },
      {
        # Was missing from the previous revision. It is the wrong-role refusal, so
        # an inventory that omits it cannot show the refusal was removed with the API.
        kind   = "apigateway-rest-api-policy"
        id     = try(aws_api_gateway_rest_api_policy.fixture[0].id, "")
        name   = "resource policy on ${try(aws_api_gateway_rest_api.fixture[0].name, "")}"
        verify = "deleted with the REST API above; no separate probe exists"
      },
      {
        kind   = "apigateway-deployment"
        id     = try(aws_api_gateway_deployment.fixture[0].id, "")
        name   = "deployment of ${try(aws_api_gateway_rest_api.fixture[0].id, "")}"
        verify = "deleted with the REST API above"
      },
      {
        kind   = "apigateway-stage"
        id     = try(aws_api_gateway_stage.fixture[0].stage_name, "")
        name   = try(aws_api_gateway_stage.fixture[0].stage_name, "")
        verify = "aws apigateway get-stage --rest-api-id ${try(aws_api_gateway_rest_api.fixture[0].id, "")} --stage-name ${try(aws_api_gateway_stage.fixture[0].stage_name, "")} # EXPECT NotFoundException"
      },
      {
        kind   = "ssm-parameter"
        id     = try(aws_ssm_parameter.fixture_provenance_secret[0].name, "")
        name   = try(aws_ssm_parameter.fixture_provenance_secret[0].name, "")
        verify = "aws ssm get-parameter --name ${try(aws_ssm_parameter.fixture_provenance_secret[0].name, "")} # EXPECT ParameterNotFound (never print the value)"
      },
      {
        kind   = "cloudwatch-log-group"
        id     = try(aws_cloudwatch_log_group.fixture[0].name, "")
        name   = try(aws_cloudwatch_log_group.fixture[0].name, "")
        verify = "aws logs describe-log-groups --log-group-name-prefix ${try(aws_cloudwatch_log_group.fixture[0].name, "")} --query 'logGroups[].logGroupName' # EXPECT empty"
      },
    ] : []
  }
}
