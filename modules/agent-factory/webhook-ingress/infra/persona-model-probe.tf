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
            service_account_name = kubernetes_service_account.agent_scaledjob_sa.metadata[0].name
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
}
