# =============================================================================
# ADOT Collector — OpenTelemetry telemetry sink for agent-worker pods
# =============================================================================
# Receives OTLP from the Claude Agent SDK (traces, metrics, logs) and exports:
#   - Traces → AWS X-Ray (awsxray exporter)
#   - Metrics → CloudWatch via EMF (awsemf exporter) — token/cost business lens
#   - Logs → CloudWatch Logs (awscloudwatchlogs exporter)
#
# Gated by var.enable_agent_otel (default false). When disabled, none of these
# resources are created and agent pods have no OTEL env vars.
#
# Design: module-local Deployment in adp-agents namespace (not the platform
# addon) for flag-gated iteration. Can migrate to platform-level addon later.
#
# Issue: #1630
# =============================================================================

# -----------------------------------------------------------------------------
# Collector ConfigMap — pipeline configuration
# -----------------------------------------------------------------------------

resource "kubernetes_config_map" "otel_collector_config" {
  count = var.enable_agent_otel ? 1 : 0

  metadata {
    name      = "adot-collector-config"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    labels = {
      "app.kubernetes.io/name"       = "adot-collector"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "observability"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  data = {
    "collector-config.yaml" = <<-YAML
      receivers:
        otlp:
          protocols:
            grpc:
              endpoint: 0.0.0.0:4317
            http:
              endpoint: 0.0.0.0:4318

      processors:
        batch:
          timeout: 5s
          send_batch_size: 256
        memory_limiter:
          check_interval: 5s
          limit_mib: 400
          spike_limit_mib: 100

      exporters:
        awsxray:
          region: ${var.aws_region}
        awsemf:
          region: ${var.aws_region}
          namespace: ADP/AgentTelemetry
          log_group_name: ${var.otel_collector_log_group}/metrics
          dimension_rollup_option: NoDimensionRollup
        awscloudwatchlogs:
          region: ${var.aws_region}
          log_group_name: ${var.otel_collector_log_group}/logs
          log_stream_name: otel-agent-logs

      extensions:
        health_check:
          endpoint: 0.0.0.0:13133

      service:
        extensions: [health_check]
        pipelines:
          traces:
            receivers: [otlp]
            processors: [memory_limiter, batch]
            exporters: [awsxray]
          metrics:
            receivers: [otlp]
            processors: [memory_limiter, batch]
            exporters: [awsemf]
          logs:
            receivers: [otlp]
            processors: [memory_limiter, batch]
            exporters: [awscloudwatchlogs]
    YAML
  }
}

# -----------------------------------------------------------------------------
# Collector Service Account (IRSA-annotated)
# -----------------------------------------------------------------------------

resource "kubernetes_service_account" "otel_collector" {
  count = var.enable_agent_otel ? 1 : 0

  metadata {
    name      = "adot-collector-sa"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.otel_collector[0].arn
    }

    labels = {
      "app.kubernetes.io/name"       = "adot-collector"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# -----------------------------------------------------------------------------
# Collector Deployment
# -----------------------------------------------------------------------------

resource "kubernetes_deployment" "otel_collector" {
  count = var.enable_agent_otel ? 1 : 0

  metadata {
    name      = "adot-collector"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    labels = {
      "app.kubernetes.io/name"       = "adot-collector"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "observability"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  spec {
    replicas = 1

    selector {
      match_labels = {
        "app.kubernetes.io/name" = "adot-collector"
      }
    }

    template {
      metadata {
        labels = {
          "app.kubernetes.io/name"      = "adot-collector"
          "app.kubernetes.io/part-of"   = "adp-agent-factory"
          "app.kubernetes.io/component" = "observability"
        }

        annotations = {
          # Prevent Karpenter from evicting the collector during consolidation
          "karpenter.sh/do-not-disrupt" = "true"
        }
      }

      spec {
        service_account_name = kubernetes_service_account.otel_collector[0].metadata[0].name

        container {
          name  = "adot-collector"
          image = var.otel_collector_image

          args = ["--config=/conf/collector-config.yaml"]

          port {
            name           = "otlp-grpc"
            container_port = 4317
            protocol       = "TCP"
          }

          port {
            name           = "otlp-http"
            container_port = 4318
            protocol       = "TCP"
          }

          port {
            name           = "health"
            container_port = 13133
            protocol       = "TCP"
          }

          env {
            name  = "AWS_REGION"
            value = var.aws_region
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

          liveness_probe {
            http_get {
              path = "/"
              port = 13133
            }
            initial_delay_seconds = 10
            period_seconds        = 15
            timeout_seconds       = 5
            failure_threshold     = 3
          }

          readiness_probe {
            http_get {
              path = "/"
              port = 13133
            }
            initial_delay_seconds = 5
            period_seconds        = 10
            timeout_seconds       = 3
          }

          volume_mount {
            name       = "collector-config"
            mount_path = "/conf"
            read_only  = true
          }
        }

        volume {
          name = "collector-config"

          config_map {
            name = kubernetes_config_map.otel_collector_config[0].metadata[0].name
          }
        }
      }
    }
  }
}

# -----------------------------------------------------------------------------
# Collector Service — ClusterIP for pod-to-collector communication
# -----------------------------------------------------------------------------

resource "kubernetes_service" "otel_collector" {
  count = var.enable_agent_otel ? 1 : 0

  metadata {
    name      = "adot-collector"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name

    labels = {
      "app.kubernetes.io/name"       = "adot-collector"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  spec {
    selector = {
      "app.kubernetes.io/name" = "adot-collector"
    }

    port {
      name        = "otlp-grpc"
      port        = 4317
      target_port = 4317
      protocol    = "TCP"
    }

    port {
      name        = "otlp-http"
      port        = 4318
      target_port = 4318
      protocol    = "TCP"
    }

    type = "ClusterIP"
  }
}

# -----------------------------------------------------------------------------
# Collector IAM Role (IRSA) — scoped to CloudWatch + X-Ray write-only
# -----------------------------------------------------------------------------
# Follows the #1204 scoped-policy discipline: no wildcards on sensitive
# actions, resource ARNs scoped where possible.
# -----------------------------------------------------------------------------

resource "aws_iam_role" "otel_collector" {
  count = var.enable_agent_otel ? 1 : 0

  name = "${local.name_prefix}-otel-collector-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = local.oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:adp-agents:adot-collector-sa"
            "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
          }
        }
      }
    ]
  })

  tags = {
    Name      = "${local.name_prefix}-otel-collector-role"
    Component = "observability"
    Issue     = "1630"
  }
}

resource "aws_iam_role_policy" "otel_collector_permissions" {
  count = var.enable_agent_otel ? 1 : 0

  name = "otel-collector-cloudwatch-xray"
  role = aws_iam_role.otel_collector[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = ["ADP/AgentTelemetry", "ADP/KnowledgeLayer"]
          }
        }
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:PutLogEvents",
          "logs:PutRetentionPolicy"
        ]
        Resource = [
          "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:${var.otel_collector_log_group}/*",
          "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:${var.otel_collector_log_group}/*:*",
          "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/adp/*/knowledge-layer/*",
          "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/adp/*/knowledge-layer/*:*"
        ]
      },
      {
        Sid    = "XRayTraceWrite"
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets",
          "xray:GetSamplingStatisticSummaries"
        ]
        Resource = "*"
      }
    ]
  })
}
