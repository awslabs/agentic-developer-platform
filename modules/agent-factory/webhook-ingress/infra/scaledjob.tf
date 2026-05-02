# =============================================================================
# Hosted Agent Worker — KEDA ScaledJob
# =============================================================================
# Spawns agent pods from the adp-<env>-agent-submit.fifo queue. One pod per
# message; FIFO groups serialize per tenant (MessageGroupId = installation_id).
#
# Design ref: docs/hosted-platform-design.md §SQS queue + KEDA ScaledJob
# Issue: #346
# =============================================================================

# -----------------------------------------------------------------------------
# Namespace
# -----------------------------------------------------------------------------

resource "kubernetes_namespace" "adp_agents" {
  metadata {
    name = "adp-agents"

    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "hosted-agent-worker"
    }
  }
}

# -----------------------------------------------------------------------------
# Service Account (IRSA-annotated)
# -----------------------------------------------------------------------------

resource "kubernetes_service_account" "agent_scaledjob_sa" {
  metadata {
    name      = "agent-scaledjob-sa"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.agent_scaledjob.arn
    }

    labels = {
      "app.kubernetes.io/name"       = "agent-scaledjob-sa"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# -----------------------------------------------------------------------------
# KEDA TriggerAuthentication — IRSA via aws-eks pod identity
# -----------------------------------------------------------------------------
# Uses the KEDA operator's own SA credentials for SQS queue-depth polling
# (identityOwner: keda). Never uses access keys.

resource "kubernetes_manifest" "trigger_auth" {
  manifest = {
    apiVersion = "keda.sh/v1alpha1"
    kind       = "TriggerAuthentication"
    metadata = {
      name      = "agent-scaledjob-aws-auth"
      namespace = kubernetes_namespace.adp_agents.metadata[0].name
      labels = {
        "app.kubernetes.io/name"    = "agent-scaledjob"
        "app.kubernetes.io/part-of" = "adp-agent-factory"
      }
    }
    spec = {
      podIdentity = {
        provider      = "aws-eks"
        identityOwner = "keda"
      }
    }
  }

  field_manager {
    force_conflicts = true
  }
}

# -----------------------------------------------------------------------------
# KEDA ScaledJob
# -----------------------------------------------------------------------------

resource "kubernetes_manifest" "agent_scaledjob" {
  manifest = {
    apiVersion = "keda.sh/v1alpha1"
    kind       = "ScaledJob"
    metadata = {
      name      = "agent-scaledjob"
      namespace = kubernetes_namespace.adp_agents.metadata[0].name
      labels = {
        "app.kubernetes.io/name"    = "agent-scaledjob"
        "app.kubernetes.io/part-of" = "adp-agent-factory"
      }
    }
    spec = {
      jobTargetRef = {
        parallelism           = 1
        completions           = 1
        backoffLimit          = 2
        activeDeadlineSeconds = var.agent_pod_deadline_seconds
        template = {
          metadata = {
            labels = {
              "app.kubernetes.io/name"    = "agent-scaledjob"
              "app.kubernetes.io/part-of" = "adp-agent-factory"
            }
          }
          spec = {
            serviceAccountName = kubernetes_service_account.agent_scaledjob_sa.metadata[0].name
            restartPolicy      = "Never"
            containers = [{
              name  = "agent-worker"
              image = var.agent_image
              env = [
                { name = "AWS_REGION", value = var.aws_region },
                { name = "QUEUE_URL", value = aws_sqs_queue.agent_submit.url },
              ]
              resources = {
                requests = {
                  cpu                 = "1"
                  memory              = "4Gi"
                  "ephemeral-storage" = "50Gi"
                }
                limits = {
                  cpu                 = "4"
                  memory              = "8Gi"
                  "ephemeral-storage" = "50Gi"
                }
              }
            }]
          }
        }
      }
      pollingInterval            = 5
      minReplicaCount            = 0
      maxReplicaCount            = 50
      successfulJobsHistoryLimit = 5
      failedJobsHistoryLimit     = 5
      triggers = [{
        type = "aws-sqs-queue"
        authenticationRef = {
          name = "agent-scaledjob-aws-auth"
        }
        metadata = {
          queueURL    = aws_sqs_queue.agent_submit.url
          queueLength = "1"
          awsRegion   = var.aws_region
        }
      }]
    }
  }

  # KEDA operator reconciles the ScaledJob and defaults fields on the pod
  # template (imagePullPolicy, terminationMessagePath, etc.). Force ownership
  # of only the fields we set — let KEDA own what it defaults.
  field_manager {
    force_conflicts = true
  }

  # KEDA + the kube-apiserver populate many server-side defaults on
  # this CRD (container imagePullPolicy, terminationMessagePath, dnsPolicy,
  # schedulerName, securityContext, resource rounding, etc.). Listing each
  # one here turns into whack-a-mole — every defaulted field produces
  # "Provider produced inconsistent result after apply" until it's named.
  #
  # Mark the entire spec tree as server-managed. Terraform still creates
  # the resource with the spec we define; the provider just doesn't try to
  # reconcile read-back shape against the sent shape. If the spec itself
  # drifts in the cluster (someone hand-edits it), we won't catch it — but
  # KEDA ScaledJobs aren't the kind of resource that gets hand-edited.
  computed_fields = [
    "metadata.labels",
    "metadata.annotations",
    "spec",
  ]

  depends_on = [kubernetes_manifest.trigger_auth]
}
