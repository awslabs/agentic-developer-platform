# =============================================================================
# Hosted Agent Worker — KEDA ScaledJob
# =============================================================================
# Spawns agent pods from the adp-<env>-agent-submit.fifo queue. One pod per
# message; FIFO groups serialize per tenant (MessageGroupId = installation_id).
#
# Design ref: docs/hosted-platform-design.md §SQS queue + KEDA ScaledJob
# Issue: #346
#
# Note on why ScaledJob + TriggerAuthentication are applied via kubectl
# local-exec rather than `kubernetes_manifest`:
# The hashicorp/kubernetes provider's kubernetes_manifest reads the server-
# side object after apply and diffs it against the sent object. KEDA and
# the kube-apiserver mutate the ScaledJob in ways Terraform's schema can't
# model (status fields, empty-object defaults on rollout/scalingStrategy,
# nullable fields on jobTargetRef, etc.), producing either "Provider
# produced inconsistent result" or "Failed to update proposed state"
# errors. Using kubectl apply sidesteps the read-back reconciliation.
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
# KEDA TriggerAuthentication + ScaledJob — applied via kubectl local-exec.
# -----------------------------------------------------------------------------

locals {
  keda_trigger_auth_yaml = <<-YAML
    apiVersion: keda.sh/v1alpha1
    kind: TriggerAuthentication
    metadata:
      name: agent-scaledjob-aws-auth
      namespace: ${kubernetes_namespace.adp_agents.metadata[0].name}
      labels:
        app.kubernetes.io/name: agent-scaledjob
        app.kubernetes.io/part-of: adp-agent-factory
    spec:
      podIdentity:
        provider: aws-eks
        identityOwner: keda
  YAML

  keda_scaledjob_yaml = <<-YAML
    apiVersion: keda.sh/v1alpha1
    kind: ScaledJob
    metadata:
      name: agent-scaledjob
      namespace: ${kubernetes_namespace.adp_agents.metadata[0].name}
      labels:
        app.kubernetes.io/name: agent-scaledjob
        app.kubernetes.io/part-of: adp-agent-factory
    spec:
      pollingInterval: 5
      minReplicaCount: 0
      maxReplicaCount: 50
      successfulJobsHistoryLimit: 5
      failedJobsHistoryLimit: 5
      jobTargetRef:
        parallelism: 1
        completions: 1
        backoffLimit: 2
        activeDeadlineSeconds: ${var.agent_pod_deadline_seconds}
        template:
          metadata:
            labels:
              app.kubernetes.io/name: agent-scaledjob
              app.kubernetes.io/part-of: adp-agent-factory
          spec:
            serviceAccountName: ${kubernetes_service_account.agent_scaledjob_sa.metadata[0].name}
            restartPolicy: Never
            containers:
              - name: agent-worker
                image: ${var.agent_image}
                env:
                  - name: AWS_REGION
                    value: ${var.aws_region}
                  - name: QUEUE_URL
                    value: ${aws_sqs_queue.agent_submit.url}
                  - name: URL_ANALYSIS_EVIDENCE_BUCKET
                    value: adp-${var.environment}-url-analysis-evidence-v2-${local.account_id}
                  %{if var.gateway_apigw_invoke_url != ""}
                  - name: ADP_GATEWAY_ENDPOINT
                    value: ${var.gateway_apigw_invoke_url}
                  %{endif}
                resources:
                  requests:
                    cpu: "1"
                    memory: 4Gi
                    ephemeral-storage: 50Gi
                  limits:
                    cpu: "4"
                    memory: 8Gi
                    ephemeral-storage: 50Gi
      triggers:
        - type: aws-sqs-queue
          authenticationRef:
            name: agent-scaledjob-aws-auth
          metadata:
            queueURL: ${aws_sqs_queue.agent_submit.url}
            queueLength: "1"
            awsRegion: ${var.aws_region}
  YAML
}

resource "null_resource" "keda_trigger_auth" {
  triggers = {
    manifest_sha   = sha256(local.keda_trigger_auth_yaml)
    namespace      = kubernetes_namespace.adp_agents.metadata[0].name
    cluster_name   = var.eks_cluster_name
    cluster_region = var.aws_region
  }

  provisioner "local-exec" {
    command = <<-CMD
      set -e
      aws eks update-kubeconfig --name ${var.eks_cluster_name} --region ${var.aws_region} >/dev/null
      cat <<'EOF' | kubectl apply -f -
${local.keda_trigger_auth_yaml}
EOF
    CMD
  }

  # Destroy provisioner is best-effort. Terraform evaluates `depends_on` at
  # create/update time; on destroy the ordering may run kubectl before the
  # RBAC RoleBinding is in place (e.g., fresh apply into a cluster where the
  # runner SA doesn't yet have KEDA permissions). Swallow non-zero exit so
  # a missing RBAC doesn't leave the resource stuck in state. The re-apply
  # `kubectl apply` is idempotent and will replace whatever exists.
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "kubectl delete triggerauthentication agent-scaledjob-aws-auth -n ${self.triggers.namespace} --ignore-not-found || true"
  }

  # RBAC must exist before kubectl apply runs as the runner SA.
  depends_on = [
    kubernetes_service_account.agent_scaledjob_sa,
    kubernetes_role_binding.runner_keda_manage,
  ]
}

resource "null_resource" "keda_scaledjob" {
  triggers = {
    manifest_sha   = sha256(local.keda_scaledjob_yaml)
    namespace      = kubernetes_namespace.adp_agents.metadata[0].name
    cluster_name   = var.eks_cluster_name
    cluster_region = var.aws_region
  }

  provisioner "local-exec" {
    command = <<-CMD
      set -e
      aws eks update-kubeconfig --name ${var.eks_cluster_name} --region ${var.aws_region} >/dev/null
      cat <<'EOF' | kubectl apply -f -
${local.keda_scaledjob_yaml}
EOF
    CMD
  }

  # Destroy provisioner is best-effort — see the keda_trigger_auth comment.
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "kubectl delete scaledjob agent-scaledjob -n ${self.triggers.namespace} --ignore-not-found || true"
  }

  # RBAC must exist before kubectl apply runs as the runner SA.
  depends_on = [
    null_resource.keda_trigger_auth,
    kubernetes_role_binding.runner_keda_manage,
  ]
}
