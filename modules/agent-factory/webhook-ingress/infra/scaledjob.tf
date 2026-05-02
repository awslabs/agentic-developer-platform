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
    manifest_sha    = sha256(local.keda_trigger_auth_yaml)
    namespace       = kubernetes_namespace.adp_agents.metadata[0].name
    cluster_name    = var.eks_cluster_name
    cluster_region  = var.aws_region
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

  provisioner "local-exec" {
    when    = destroy
    command = "kubectl delete triggerauthentication agent-scaledjob-aws-auth -n ${self.triggers.namespace} --ignore-not-found"
  }

  depends_on = [kubernetes_service_account.agent_scaledjob_sa]
}

resource "null_resource" "keda_scaledjob" {
  triggers = {
    manifest_sha    = sha256(local.keda_scaledjob_yaml)
    namespace       = kubernetes_namespace.adp_agents.metadata[0].name
    cluster_name    = var.eks_cluster_name
    cluster_region  = var.aws_region
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

  provisioner "local-exec" {
    when    = destroy
    command = "kubectl delete scaledjob agent-scaledjob -n ${self.triggers.namespace} --ignore-not-found"
  }

  depends_on = [null_resource.keda_trigger_auth]
}
