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

output "allowed_caller_role_arns" {
  description = <<-EOT
    The role ARNs the fixture API's resource policy permits on the internal plane.

    Published so `fixture-lifecycle.sh verify` can prove its wrong-role probe signs
    as an identity that is genuinely NOT allowlisted. Without that check, a 403
    observed while signing as a PERMITTED role would be recorded as the Deny
    working — a false proof of the one control that exercises this component's own
    resource policy.

    Role ARNs are not secret.
  EOT
  value       = var.allowed_caller_role_arns
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

# =============================================================================
# THE NETWORKPOLICY SEAM WITH #3968 — AN OBSERVATION, NOT A POLICY CHANGE
# =============================================================================
# Root's blocker 6: #3968's fixture gateway policy allows ingress on 8080 only from
# pods in adp-gateway / adp-agents via namespaceSelector, and ALB connections are
# not namespace-selected pod sources. A narrow ALB source observation/output had to
# be agreed with #3968, with no global relaxation of the ordinary policy.
#
# This output IS that agreed artifact, and its shape is chosen to make the wrong
# thing hard:
#
#   * It publishes /32 addresses — the narrowest true statement of the source.
#     NOT a subnet CIDR (which would admit the ordinary gateway's ALB: verified,
#     both sit in subnet-03ae2ea2ebdf611bb), and not 0.0.0.0/0.
#   * It is READ from the live load balancer's own network interfaces. Nothing
#     here creates, edits or reads a NetworkPolicy, and this component touches no
#     file under platform/scripts/operator/wave2 — the two changes compose through
#     this value.
#   * It is bound to the run, so a value captured from one run cannot be pasted
#     into another's policy and quietly admit a stale address.
#   * It is a REFUSAL when empty rather than an empty list, because an ipBlock that
#     matches nothing denies the edge while reading as configured (see the
#     precondition in main.tf).
#
# WHAT #3968 STILL OWNS, AND WHY THE SPLIT IS HERE
# -----------------------------------------------
# render_fixture.render_policies builds the policy; this component must not edit
# it. What it needs from here is the one fact it cannot know: the source addresses
# of an ALB that did not exist when the renderer ran. The composition is therefore
# "this component observes, #3968 renders", which also means neither side can
# silently widen the other: an ipBlock rule added from this value admits exactly
# these addresses on exactly the fixture gateway's pods, and the ORDINARY gateway's
# policy is not touched by either side.
#
# Addresses are not secret.
output "fixture_alb_network_policy_source" {
  description = <<-EOT
    The traffic source #3968's fixture gateway NetworkPolicy must admit for the
    trusted edge to reach the fixture pod, as /32 CIDRs.

    WHY THIS IS NEEDED. The fixture gateway policy's only ingress rule admits pods
    by namespaceSelector (adp-gateway, adp-agents). With target-type `ip` the ALB
    connects from its OWN network interfaces, which belong to no pod and are in no
    namespace, so that rule does not admit this edge. The policy blocks both
    health checks and requests to IP targets on the pod port. Targets become
    unhealthy and the worker cannot bootstrap; establish the network path first.

    HOW TO USE IT. Add ONE ingress rule to the FIXTURE gateway policy (the
    run-scoped one #3968 renders — never the ordinary gateway's):

      - from:
          - ipBlock:
              cidr: <each entry of source_cidrs, one rule entry per address>
        ports:
          - protocol: TCP
            port: <container_port below>

    Do NOT widen it to the subnet CIDR: both ordinary gateway ALBs share a subnet
    with this one, so a subnet rule would admit production's edge to the fixture.

    Refresh the live interface observation and matching owned policy before
    execution. New addresses missing from the policy deny traffic; retired
    addresses can be reassigned and leave unintended access. Saved output and
    a matching nonce do not prove freshness. Remove owned allowances on cleanup.

    Addresses are not secret.
  EOT
  value = {
    source_cidrs = try(local.fixture_alb_source_cidrs, [])
    source_ips   = try(local.fixture_alb_source_ips, [])

    # The port the policy rule must name. Read from the same place the Ingress
    # template targets, so this cannot disagree with what the ALB actually connects
    # to: the fixture Service maps 80 -> 8080 and the pods listen on 8080.
    container_port = 8080

    # The listener port, recorded separately. These two are DIFFERENT numbers and
    # naming the wrong one produces a policy that blocks the flow under test: the
    # ALB listens on ${var.fixture_alb_listener_port} and connects to the pod on
    # 8080.
    alb_listener_port = var.fixture_alb_listener_port

    # Which policy this belongs on. Stated so the value cannot be applied to the
    # ordinary gateway's policy by someone reading only the output.
    apply_to = "the run-scoped FIXTURE gateway NetworkPolicy rendered by #3968's render_fixture.render_policies — NOT the ordinary gateway policy, whose selectors #5836 preserves"

    # Run binding, so a value from another run is visibly not this one's.
    run_nonce = var.run_nonce
    alb_arn   = try(data.aws_lb.fixture[0].arn, "")

    # How to re-derive it without Terraform, for a reviewer who wants to check.
    verify = try(
      "aws ec2 describe-network-interfaces --filters Name=description,Values='ELB ${data.aws_lb.fixture[0].arn_suffix}' --query 'NetworkInterfaces[].PrivateIpAddress'",
      ""
    )
  }
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
        kind = "apigateway-rest-api"
        # The TERRAFORM RESOURCE TYPE, which is what each plan line reports as
        # `type`. Recorded because an id ALONE cannot establish complete ownership:
        # aws_api_gateway_rest_api_policy's id IS the rest-api id (the policy is an
        # attribute of the API, not a separate object), so those two entries share
        # one identifier. Compared as a set of ids, a plan deleting only the API
        # satisfies both, and omitting the policy could not be detected — while the
        # policy IS the wrong-role refusal, so silently leaving it is not benign.
        # Keying on (type, id) pairs keeps the two individually accounted for.
        type   = "aws_api_gateway_rest_api"
        id     = try(aws_api_gateway_rest_api.fixture[0].id, "")
        name   = try(aws_api_gateway_rest_api.fixture[0].name, "")
        verify = "aws apigateway get-rest-api --rest-api-id ${try(aws_api_gateway_rest_api.fixture[0].id, "")} # EXPECT NotFoundException after teardown"
      },
      {
        # Was missing from the previous revision. It is the wrong-role refusal, so
        # an inventory that omits it cannot show the refusal was removed with the API.
        kind = "apigateway-rest-api-policy"
        type = "aws_api_gateway_rest_api_policy"
        # Shares the REST API's id — see the `type` note above for why that makes a
        # set of ids insufficient.
        id     = try(aws_api_gateway_rest_api_policy.fixture[0].id, "")
        name   = "resource policy on ${try(aws_api_gateway_rest_api.fixture[0].name, "")}"
        verify = "deleted with the REST API above; no separate probe exists"
      },
      {
        kind   = "apigateway-deployment"
        type   = "aws_api_gateway_deployment"
        id     = try(aws_api_gateway_deployment.fixture[0].id, "")
        name   = "deployment of ${try(aws_api_gateway_rest_api.fixture[0].id, "")}"
        verify = "deleted with the REST API above"
      },
      {
        kind = "apigateway-stage"
        # `.id`, NOT `.stage_name`. The provider sets the stage's id to
        # "ags-<rest-api-id>-<stage-name>" (aws/internal/service/apigateway/stage.go:
        # d.SetId(fmt.Sprintf("ags-%s-%s", apiID, stageName))), so recording the bare
        # stage_name ("dev") recorded an identifier NO PLAN EVER CARRIES.
        #
        # This was not a cosmetic mismatch: the destroy guard compares the plan's
        # change.before.id against these ids, so the stage's real line never matched,
        # the guard concluded NOT OWNED, and LEGITIMATE TEARDOWN WAS BLOCKED —
        # pushing the operator toward deleting by hand, which is the exact outcome the
        # guard exists to prevent. Root reproduced this with a real-shaped plan.
        #
        # Every entry here now uses the resource's own `.id` for the same reason: the
        # receipt's identifiers must be the ones Terraform will actually present.
        type   = "aws_api_gateway_stage"
        id     = try(aws_api_gateway_stage.fixture[0].id, "")
        name   = try(aws_api_gateway_stage.fixture[0].stage_name, "")
        verify = "aws apigateway get-stage --rest-api-id ${try(aws_api_gateway_rest_api.fixture[0].id, "")} --stage-name ${try(aws_api_gateway_stage.fixture[0].stage_name, "")} # EXPECT NotFoundException"
      },
      {
        kind = "ssm-parameter"
        type = "aws_ssm_parameter"
        # The provider's id for an SSM parameter IS its name.
        id     = try(aws_ssm_parameter.fixture_provenance_secret[0].id, "")
        name   = try(aws_ssm_parameter.fixture_provenance_secret[0].name, "")
        verify = "aws ssm get-parameter --name ${try(aws_ssm_parameter.fixture_provenance_secret[0].name, "")} # EXPECT ParameterNotFound (never print the value)"
      },
      {
        kind = "cloudwatch-log-group"
        type = "aws_cloudwatch_log_group"
        # The provider's id for a log group IS its name.
        id     = try(aws_cloudwatch_log_group.fixture[0].id, "")
        name   = try(aws_cloudwatch_log_group.fixture[0].name, "")
        verify = "aws logs describe-log-groups --log-group-name-prefix ${try(aws_cloudwatch_log_group.fixture[0].name, "")} --query 'logGroups[].logGroupName' # EXPECT empty"
      },
    ] : []
  }
}
