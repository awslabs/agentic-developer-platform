# =============================================================================
# Independent agent-codex-reviewer
# =============================================================================
# This worker deliberately does not share the agent-submit queue, Claude worker
# image, agent-worker.ts entrypoint, persona loader, or reviewer finalizer.

resource "aws_sqs_queue" "codex_review_dlq" {
  name                        = "${local.name_prefix}-codex-review-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  message_retention_seconds   = 1209600
}

resource "aws_sqs_queue" "codex_review" {
  name                        = "${local.name_prefix}-codex-review.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  visibility_timeout_seconds  = var.sqs_visibility_timeout
  message_retention_seconds   = var.sqs_message_retention
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.codex_review_dlq.arn
    maxReceiveCount     = var.sqs_max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "codex_review" {
  queue_url = aws_sqs_queue.codex_review_dlq.url
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.codex_review.arn]
  })
}

resource "aws_iam_role" "codex_reviewer" {
  name = "${local.name_prefix}-codex-reviewer-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Federated = local.oidc_provider_arn }
        Action    = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:adp-agents:codex-reviewer-sa"
            "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
          }
        }
      },
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.keda_operator.arn }
        Action    = "sts:AssumeRole"
      }
    ]
  })
  tags = {
    Name      = "${local.name_prefix}-codex-reviewer-role"
    Component = "agent-codex-reviewer"
  }
}

resource "aws_iam_role_policy" "codex_reviewer" {
  name = "codex-reviewer-scoped-permissions"
  role = aws_iam_role.codex_reviewer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReviewQueue"
        Effect = "Allow"
        Action = [
          "sqs:ChangeMessageVisibility",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ReceiveMessage"
        ]
        Resource = aws_sqs_queue.codex_review.arn
      },
      {
        Sid      = "InvocationStatusOnly"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.webhook_events.arn
        Condition = {
          "ForAllValues:StringEquals" = {
            "dynamodb:Attributes" = [
              "event_id",
              "arrived_at",
              "status",
              "status_updated_at",
              "summary",
              "error_message",
              "run_id"
            ]
          }
        }
      },
      {
        Sid    = "GatewayOnly"
        Effect = "Allow"
        Action = ["execute-api:Invoke"]
        Resource = [
          "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/agent/*",
          "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/github-installation-token"
        ]
      }
    ]
  })
}

resource "kubernetes_service_account" "codex_reviewer" {
  metadata {
    name      = "codex-reviewer-sa"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.codex_reviewer.arn
    }
    labels = {
      "app.kubernetes.io/name"       = "agent-codex-reviewer"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# Register the dedicated IRSA role with the gateway authorizer. This is a
# model-neutral authentication boundary; it grants no Claude worker access.
data "aws_ssm_parameter" "codex_reviewer_agent_registry" {
  name = "/adp/${var.environment}/gateway/agent-registry-table"
}

locals {
  codex_reviewer_credential_scopes = compact([
    var.codex_reviewer_apply_fixes ? "codex:branch-write" : "",
    var.codex_reviewer_merge_enabled ? "codex:merge" : ""
  ])
}

resource "aws_dynamodb_table_item" "codex_reviewer_agent_registry" {
  table_name = data.aws_ssm_parameter.codex_reviewer_agent_registry.value
  hash_key   = "agent_id"
  item = jsonencode(merge({
    agent_id              = { S = "codex-reviewer" }
    role_arn              = { S = aws_iam_role.codex_reviewer.arn }
    agent_name            = { S = "agent-codex-reviewer" }
    org_id                = { S = "__platform__" }
    team_id               = { S = "__agents__" }
    owner                 = { S = "platform" }
    scope                 = { S = "internal" }
    requires_run_identity = { BOOL = false }
    status                = { S = "active" }
    allowed_models        = { SS = ["*"] }
    budget_config_id      = { S = "" }
    description           = { S = "Independent Codex SDK pull-request reviewer" }
    }, length(local.codex_reviewer_credential_scopes) > 0 ? {
    credential_scopes = { SS = local.codex_reviewer_credential_scopes }
  } : {}))
}

locals {
  codex_reviewer_trigger_auth_yaml = <<-YAML
    apiVersion: keda.sh/v1alpha1
    kind: TriggerAuthentication
    metadata:
      name: codex-reviewer-aws-auth
      namespace: ${kubernetes_namespace.adp_agents.metadata[0].name}
    spec:
      podIdentity:
        provider: aws-eks
        identityOwner: keda
  YAML

  codex_reviewer_scaledjob_yaml = <<-YAML
    apiVersion: keda.sh/v1alpha1
    kind: ScaledJob
    metadata:
      name: agent-codex-reviewer
      namespace: ${kubernetes_namespace.adp_agents.metadata[0].name}
      labels:
        app.kubernetes.io/name: agent-codex-reviewer
        app.kubernetes.io/part-of: adp-agent-factory
    spec:
      pollingInterval: 5
      minReplicaCount: 0
      maxReplicaCount: 20
      successfulJobsHistoryLimit: 1
      failedJobsHistoryLimit: 5
      jobTargetRef:
        backoffLimit: 0
        activeDeadlineSeconds: 7200
        ttlSecondsAfterFinished: 3600
        template:
          metadata:
            labels:
              app.kubernetes.io/name: agent-codex-reviewer
          spec:
            serviceAccountName: codex-reviewer-sa
            restartPolicy: Never
            securityContext:
              runAsNonRoot: true
              seccompProfile:
                type: RuntimeDefault
            containers:
              - name: reviewer
                image: ${local.codex_reviewer_image}
                imagePullPolicy: Always
                securityContext:
                  allowPrivilegeEscalation: false
                  capabilities:
                    drop: ["ALL"]
                env:
                  - name: CODEX_REVIEWER_ENABLED
                    value: "${var.codex_reviewer_enabled}"
                  - name: CODEX_REVIEWER_APPLY_FIXES
                    value: "${var.codex_reviewer_apply_fixes}"
                  - name: CODEX_REVIEWER_MERGE_ENABLED
                    value: "${var.codex_reviewer_merge_enabled}"
                  - name: CODEX_REVIEWER_MODEL
                    value: "${var.codex_reviewer_model}"
                  - name: CODEX_REVIEWER_VISIBILITY_SECONDS
                    value: "${var.sqs_visibility_timeout}"
                  - name: CODEX_REVIEW_QUEUE_URL
                    value: ${aws_sqs_queue.codex_review.url}
                  - name: AWS_REGION
                    value: ${var.aws_region}
                  - name: ADP_GATEWAY_ENDPOINT
                    value: ${data.aws_ssm_parameter.gateway_apigw_invoke_url.value}
                  - name: SIGV4_PROXY_TARGET
                    value: ${data.aws_ssm_parameter.gateway_apigw_invoke_url.value}/agent
                  - name: WEBHOOK_EVENTS_TABLE
                    value: ${aws_dynamodb_table.webhook_events.name}
                resources:
                  requests:
                    cpu: "1"
                    memory: 2Gi
                    ephemeral-storage: 20Gi
                  limits:
                    cpu: "4"
                    memory: 8Gi
                    ephemeral-storage: 50Gi
      triggers:
        - type: aws-sqs-queue
          authenticationRef:
            name: codex-reviewer-aws-auth
          metadata:
            queueURL: ${aws_sqs_queue.codex_review.url}
            queueLength: "1"
            awsRegion: ${var.aws_region}
  YAML
}

resource "null_resource" "codex_reviewer_keda" {
  triggers = {
    auth_sha       = sha256(local.codex_reviewer_trigger_auth_yaml)
    scaledjob_sha  = sha256(local.codex_reviewer_scaledjob_yaml)
    namespace      = kubernetes_namespace.adp_agents.metadata[0].name
    cluster_name   = var.eks_cluster_name
    cluster_region = var.aws_region
  }

  provisioner "local-exec" {
    environment = { KUBECONFIG = "/tmp/adp-deploy-kubeconfig" }
    command     = <<-CMD
      set -e
      aws eks update-kubeconfig --name ${var.eks_cluster_name} --region ${var.aws_region} --kubeconfig /tmp/adp-deploy-kubeconfig >/dev/null
      cat <<'EOF' | kubectl apply -f -
${local.codex_reviewer_trigger_auth_yaml}
---
${local.codex_reviewer_scaledjob_yaml}
EOF
    CMD
  }

  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = "kubectl delete scaledjob agent-codex-reviewer -n ${self.triggers.namespace} --ignore-not-found || true; kubectl delete triggerauthentication codex-reviewer-aws-auth -n ${self.triggers.namespace} --ignore-not-found || true"
  }

  depends_on = [
    kubernetes_service_account.codex_reviewer,
    aws_dynamodb_table_item.codex_reviewer_agent_registry,
    kubernetes_role_binding.runner_keda_manage,
    helm_release.keda,
  ]
}
