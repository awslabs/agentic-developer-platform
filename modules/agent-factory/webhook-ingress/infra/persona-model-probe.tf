# =============================================================================
# PMM-03 — faithful persona/model invocability probe scheduler (#5420)
# =============================================================================
#
# This CronJob is only a periodic tick.  The Gateway owns cycle admission,
# chooses the model and destination, reserves the worst-case spend, and releases
# credentials only after a slot is durably marked started.  The pod cannot turn
# a catalogue read or user save into a paid invocation.
#
# The resource deliberately exists while disabled but is suspended.  In
# addition, the Gateway defaults to disabled, zero slots and zero budget.  All
# three controls must be changed by PMM-09 after explicit account/spend approval.

# The probe receives short-lived credentials for the Gateway-selected Bedrock
# destination after durable admission. It therefore MUST NOT share the ordinary
# agent worker identity: that role is used by every hosted chat and webhook pod.
# Keep this role, service account, and registry row one-to-one so only this
# suspended CronJob can authenticate as persona-model-probe.
resource "aws_iam_role" "persona_model_probe" {
  name = "${local.name_prefix}-persona-model-probe-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:adp-agents:persona-model-probe-sa"
          "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = {
    Name      = "${local.name_prefix}-persona-model-probe-role"
    Component = "persona-model-probe"
  }
}

resource "aws_iam_role_policy" "persona_model_probe" {
  name = "persona-model-probe-gateway-only"
  role = aws_iam_role.persona_model_probe.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "ProbeGatewayOnly"
      Effect = "Allow"
      Action = ["execute-api:Invoke"]
      Resource = [
        "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/persona-model-probes/claim",
        "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/persona-model-probes/*/start",
        "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/persona-model-probes/*/complete",
      ]
    }]
  })
}

resource "kubernetes_service_account" "persona_model_probe" {
  metadata {
    name      = "persona-model-probe-sa"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.persona_model_probe.arn
    }

    labels = {
      "app.kubernetes.io/name"       = "persona-model-probe"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

data "aws_ssm_parameter" "persona_model_probe_agent_registry" {
  name = "/adp/${var.environment}/gateway/agent-registry-table"
}

# This internal-scope row cannot be created through the public registry API and
# is managed with the role it authenticates, preventing a user-selected role or
# ordinary worker alias from becoming the credential-bearing probe principal.
resource "aws_dynamodb_table_item" "persona_model_probe_agent_registry" {
  table_name = data.aws_ssm_parameter.persona_model_probe_agent_registry.value
  hash_key   = "agent_id"
  item = jsonencode({
    agent_id              = { S = "persona-model-probe" }
    role_arn              = { S = aws_iam_role.persona_model_probe.arn }
    agent_name            = { S = "persona-model-probe" }
    org_id                = { S = "__platform__" }
    team_id               = { S = "__agents__" }
    owner                 = { S = "platform" }
    scope                 = { S = "internal" }
    requires_run_identity = { BOOL = false }
    status                = { S = "active" }
    allowed_models        = { SS = ["*"] }
    budget_config_id      = { S = "" }
    description           = { S = "Dedicated faithful persona/model invocability probe" }
  })
}

resource "kubernetes_cron_job_v1" "persona_model_probe" {
  metadata {
    name      = "persona-model-invocability-probe"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    labels = {
      "app.kubernetes.io/name"       = "agent-scaledjob"
      "app.kubernetes.io/component"  = "persona-model-probe"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  spec {
    schedule                      = var.persona_model_probe_schedule
    suspend                       = !var.persona_model_probe_enabled
    concurrency_policy            = "Forbid"
    starting_deadline_seconds     = 60
    successful_jobs_history_limit = 1
    failed_jobs_history_limit     = 3

    job_template {
      metadata {
        labels = {
          "app.kubernetes.io/name"      = "agent-scaledjob"
          "app.kubernetes.io/component" = "persona-model-probe"
          "app.kubernetes.io/part-of"   = "adp-agent-factory"
        }
      }

      spec {
        parallelism             = 1
        completions             = 1
        backoff_limit           = 0
        active_deadline_seconds = var.persona_model_probe_deadline_seconds

        template {
          metadata {
            labels = {
              # Reuse the worker egress NetworkPolicy.  Changing this label
              # silently leaves the pod selected only by default-deny egress.
              "app.kubernetes.io/name"      = "agent-scaledjob"
              "app.kubernetes.io/component" = "persona-model-probe"
              "app.kubernetes.io/part-of"   = "adp-agent-factory"
            }
          }

          spec {
            service_account_name = kubernetes_service_account.persona_model_probe.metadata[0].name
            restart_policy       = "Never"

            security_context {
              run_as_non_root = true
              run_as_user     = 1001
              run_as_group    = 1001
              fs_group        = 1001

              seccomp_profile {
                type = "RuntimeDefault"
              }
            }

            container {
              name    = "persona-model-probe"
              image   = local.agent_image
              command = ["node", "/app/dist/invocability-probe/index.js"]

              security_context {
                allow_privilege_escalation = false
                read_only_root_filesystem  = true

                capabilities {
                  drop = ["ALL"]
                }
              }

              env {
                name  = "AWS_REGION"
                value = var.aws_region
              }

              env {
                name  = "ADP_GATEWAY_ENDPOINT"
                value = data.aws_ssm_parameter.gateway_apigw_invoke_url.value
              }

              # Defence in depth only. Durable slot and spend admission lives in
              # the Gateway, not in this scheduler manifest.
              env {
                name  = "ADP_PERSONA_MODEL_PROBE_ENABLED"
                value = tostring(var.persona_model_probe_enabled)
              }

              resources {
                requests = {
                  cpu    = "100m"
                  memory = "256Mi"
                }
                limits = {
                  cpu    = "500m"
                  memory = "512Mi"
                }
              }

              volume_mount {
                name       = "tmp"
                mount_path = "/tmp"
              }
            }

            volume {
              name = "tmp"
              empty_dir {
                size_limit = "64Mi"
              }
            }
          }
        }
      }
    }
  }

  depends_on = [aws_dynamodb_table_item.persona_model_probe_agent_registry]
}
