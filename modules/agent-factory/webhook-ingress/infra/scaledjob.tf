# =============================================================================
# Hosted Agent Worker — KEDA ScaledJob
# =============================================================================
# Spawns agent pods from the adp-<env>-agent-submit.fifo queue. One pod per
# message; FIFO groups serialize per issue (MessageGroupId = tenant#repo#issue).
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
  # GitLab API destination is deployment-owned. Never derive it from the
  # webhook body or SQS envelope; those are untrusted inputs.
  gitlab_env_block = var.gitlab_webhook_enabled ? join("\n", [
    "                  - name: GITLAB_URL",
    "                    value: ${data.aws_ssm_parameter.gitlab_url[0].value}",
  ]) : ""

  # Knowledge Layer env vars for agent-worker container (#3286).
  # Conditionally included in the ScaledJob YAML when knowledge_layer_enabled=true.
  # Uses join() to avoid nested heredoc syntax issues in HCL ternary.
  knowledge_layer_env_block = var.knowledge_layer_enabled ? join("\n", [
    "                  # ── Knowledge Layer (Issue #3286) ───────────────────────────────",
    "                  # Connects hosted agents to the agent-context MCP server for",
    "                  # code intelligence tools (search, understand, impact, etc.).",
    "                  - name: KNOWLEDGE_LAYER_ENABLED",
    "                    value: \"1\"",
    "                  - name: CONTEXT_MCP_SERVER_URL",
    "                    value: http://context-mcp.agent-context.svc.cluster.local:5100",
    "                  # Issue #4073 (finding #8): the Door authenticates every",
    "                  # caller with a shared secret. entrypoint.py reads this",
    "                  # secret into DOOR_API_KEY; lib/doorAuth.ts sends it as",
    "                  # X-Internal-Api-Key. Without it every verb returns 401.",
    "                  # Same SM secret the gateway internal plane uses, already",
    "                  # covered by the scaledjob role's adp/* GetSecretValue.",
    "                  - name: ADP_DOOR_API_KEY_SECRET",
    "                    value: adp/${var.environment}/gateway/internal-api-key",
  ]) : ""

  # OpenTelemetry env vars for agent-worker container (#1630).
  # Conditionally included in the ScaledJob YAML when enable_agent_otel=true.
  # Uses join() to avoid nested heredoc syntax issues in HCL ternary.
  otel_env_block = var.enable_agent_otel ? join("\n", [
    "                  # ── OpenTelemetry (Issue #1630) ──────────────────────────────",
    "                  # Claude Agent SDK telemetry → ADOT Collector → CW/X-Ray.",
    "                  # Fire-and-forget: export failures never block agent runs.",
    "                  - name: CLAUDE_CODE_ENABLE_TELEMETRY",
    "                    value: \"1\"",
    "                  - name: OTEL_TRACES_EXPORTER",
    "                    value: otlp",
    "                  - name: OTEL_METRICS_EXPORTER",
    "                    value: otlp",
    "                  - name: OTEL_LOGS_EXPORTER",
    "                    value: otlp",
    "                  - name: OTEL_EXPORTER_OTLP_PROTOCOL",
    "                    value: grpc",
    "                  - name: OTEL_EXPORTER_OTLP_ENDPOINT",
    "                    value: http://adot-collector.adp-agents.svc.cluster.local:4317",
    "                  - name: OTEL_SERVICE_NAME",
    "                    value: adp-agent-worker",
    "                  - name: OTEL_BSP_SCHEDULE_DELAY",
    "                    value: \"5000\"",
    "                  - name: OTEL_BSP_EXPORT_TIMEOUT",
    "                    value: \"10000\"",
    "                  - name: OTEL_METRIC_EXPORT_INTERVAL",
    "                    value: \"5000\"",
    "                  - name: OTEL_RESOURCE_ATTRIBUTES",
    "                    value: service.namespace=adp-agents,deployment.environment=${var.environment}",
    "                  - name: ENABLE_AGENT_OTEL",
    "                    value: \"1\"",
  ]) : ""

  # Live-control env vars for the agent-worker container (Issue #3960).
  #
  # POD_IP is unconditional and the rest are flag-gated, which is deliberate. The
  # worker binds its control listener to POD_IP explicitly and REFUSES to start
  # the listener if POD_IP is absent — it never falls back to 0.0.0.0, because a
  # wildcard bind would make the port reachable on every interface the pod has,
  # which is precisely what the ingress NetworkPolicy exists to prevent. Supplying
  # the address always (rather than only with the flag) means enabling the feature
  # is one variable, not two, and there is no configuration in which the flag is on
  # and the bind address is missing.
  #
  # There is no way to inject the control TOKEN here, and that is the point: it is
  # minted inside the pod by `secrets.token_urlsafe` per run (entrypoint.py) and
  # written straight to the invocation row. A token in a manifest would be one
  # value shared by every run, visible in `kubectl describe`, and unrotatable.
  agent_control_env_block = var.agent_control_enabled ? join("\n", [
    "                  # ── Live run control (Issue #3960) ───────────────────────────",
    "                  # Strict flag: the worker starts a listener ONLY on the exact",
    "                  # string \"true\". Read independently of the gateway's own flag —",
    "                  # neither side can activate the other.",
    "                  - name: FEATURE_AGENT_CONTROL_ENABLED",
    "                    value: \"true\"",
    "                  - name: ADP_CONTROL_PORT",
    "                    value: \"${var.agent_control_port}\"",
    "                  # Duration cap only; the gateway supplies the absolute Job",
    "                  # deadline from Kubernetes during protected bootstrap.",
    "                  # Registration time is not the beginning of Job lifetime.",
    "                  - name: ADP_POD_DEADLINE_SECONDS",
    "                    value: \"${var.agent_pod_deadline_seconds}\"",
  ]) : ""

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
${local.agent_worker_pause_annotation}
      labels:
        app.kubernetes.io/name: agent-scaledjob
        app.kubernetes.io/part-of: adp-agent-factory
    spec:
      pollingInterval: 5
      minReplicaCount: 0
      maxReplicaCount: 50
      rollout:
        strategy: gradual
      # Issue #4031: keep at most ONE Completed job visible. FIFO group
      # serialization makes KEDA spawn speculative pods that receive nothing
      # and exit 0 (entrypoint.py: "No message available after long-poll") —
      # see the triggers block below for why that is accepted and must not be
      # "fixed" via trigger metadata. At 5, those clean no-ops accumulated in
      # `kubectl get pods -n adp-agents`, and the SOPHOS PoV could not tell
      # them apart from genuinely crash-looping workers (#4030) — churn read
      # as breakage and materially slowed diagnosis of a real bug.
      # failedJobsHistoryLimit intentionally stays at 5: failed pods are the
      # diagnostic surface an operator needs to keep.
      successfulJobsHistoryLimit: 1
      failedJobsHistoryLimit: 5
      # Keep existing Jobs running when the worker template changes.
      rollout:
        strategy: gradual
      jobTargetRef:
        parallelism: 1
        completions: 1
        backoffLimit: 2
        activeDeadlineSeconds: ${var.agent_pod_deadline_seconds}
        template:
          metadata:
            # Protect in-flight agent runs from Karpenter/Auto Mode consolidation.
            # These are long-running, restartPolicy: Never jobs — a consolidation
            # eviction kills the run silently (no resume) and orphans its SQS
            # message. Unlike the prepull DaemonSet (#1807), this is on the WORK
            # pod, not per-node: it only blocks scale-in of a node WHILE an agent
            # is actively running on it; idle nodes still consolidate, so it does
            # NOT re-introduce the always-on cost bug.
            annotations:
              karpenter.sh/do-not-disrupt: "true"
            labels:
              app.kubernetes.io/name: agent-scaledjob
              app.kubernetes.io/part-of: adp-agent-factory
          spec:
            serviceAccountName: ${local.agent_worker_sa_name}
            restartPolicy: Never
            securityContext:
              runAsNonRoot: true
              runAsUser: 1001
              runAsGroup: 1001
              fsGroup: 1001
              seccompProfile:
                type: RuntimeDefault
            containers:
              - name: agent-worker
                image: ${local.agent_image}
                env:
                  - name: AWS_REGION
                    value: ${var.aws_region}
                  # Issue #4028: without this the worker falls back to "dev" in
                  # EVERY environment (entrypoint.py: os.environ.get("ENVIRONMENT",
                  # os.environ.get("ENV", "dev")); no Dockerfile default), so
                  # staging/prod bootstrap logs were written to the dev log group
                  # /adp/dev/agent-factory/bootstrap — cross-environment
                  # commingling, and env-scoped IAM/log-group resources would
                  # silently deny outside dev.
                  - name: ENVIRONMENT
                    value: ${var.environment}
                  - name: QUEUE_URL
                    value: ${aws_sqs_queue.agent_submit.url}
                  # Persona-name routing keeps Codex on this same queue/image.
                  - name: CODEX_REVIEWER_APPLY_FIXES
                    value: "${var.codex_reviewer_apply_fixes}"
                  - name: CODEX_REVIEWER_MERGE_ENABLED
                    value: "${var.codex_reviewer_merge_enabled}"
                  - name: CODEX_REVIEWER_MODEL
                    value: "${var.codex_reviewer_model}"
                  - name: URL_ANALYSIS_EVIDENCE_BUCKET
                    value: adp-${var.environment}-url-analysis-evidence-v2-${local.account_id}
                  - name: AGENT_RUN_LOGS_BUCKET
                    value: adp-${var.environment}-agent-run-logs-${local.account_id}
                  # Issue #4184: AGENT_FALLBACK_BUCKET had six readers in the
                  # agent TypeScript (agent-worker.ts, ghPost.ts, S3Fallback.ts)
                  # and NO writer anywhere in the repo, so every fallback write
                  # took a `|| 'adp-agent-state'` branch. That bucket lives in a
                  # foreign AWS account (HeadBucket → 403 while a control name
                  # → 404) and matches no prefix in scaledjob-iam.tf's S3Combined
                  # statement — verified `implicitDeny` via simulate-custom-policy
                  # against the inline policy document alone. Every fallback write
                  # was therefore silently AccessDenied, at exactly the moment the
                  # primary path had already failed. The git-changes backup site
                  # preserves uncommitted work after a failed push, so the denial
                  # was discarding whole runs of irreplaceable agent output.
                  #
                  # Deliberately EQUAL to AGENT_RUN_LOGS_BUCKET above: run output
                  # that could not reach GitHub is a run log, and this is the same
                  # bucket + credentials as the transcript upload that demonstrably
                  # works (entrypoint.py). Keep both names — they are read by two
                  # different runtimes (Python entrypoint vs Node worker); do not
                  # "simplify" them into one, that couples independent consumers.
                  - name: AGENT_FALLBACK_BUCKET
                    value: adp-${var.environment}-agent-run-logs-${local.account_id}
                  # Beads (agent task state). Latent on this path today: `bd` is
                  # not installed in the agent-runtime image, so isBeadsAvailable()
                  # returns false and no beads S3 op runs here. Set now so the path
                  # is correct if/when bd is added, rather than inheriting the same
                  # foreign-bucket default. Region comes from AWS_REGION above.
                  - name: BEADS_S3_BUCKET
                    value: adp-${var.environment}-agent-beads-state-${local.account_id}
                  - name: ADP_GATEWAY_ENDPOINT
                    value: ${data.aws_ssm_parameter.gateway_apigw_invoke_url.value}
                  # Reconciled from live cluster drift (was kubectl-applied during
                  # the tenant credential migration; now codified here so future
                  # apply does not wipe these). See scripts/migrate-tenant-aws-creds-to-user.py.
                  - name: ENABLE_USER_CREDENTIALS
                    value: "1"
                  # Phase 3 cutover: route Bedrock through the platform gateway.
                  # Bedrock calls go via sigv4-proxy → API GW /agent/* → gateway pod
                  # → Bedrock with platform IRSA (platform billing, gateway-mediated
                  # tenant isolation + budgets + audit). When the operations persona
                  # also assumes a customer AWS role, the entrypoint composes both:
                  # Bedrock via gateway, customer creds for shell `aws ...` commands.
                  # See entrypoint.py:407-471 (ADP_BEDROCK_VIA composition logic).
                  - name: ADP_BEDROCK_VIA
                    value: gateway
                  - name: SIGV4_PROXY_TARGET
                    value: ${data.aws_ssm_parameter.gateway_apigw_invoke_url.value}/agent
                  - name: SIGV4_PROXY_PORT
                    value: "9090"
                  - name: WEBHOOK_EVENTS_TABLE
                    value: ${aws_dynamodb_table.webhook_events.name}
                  # Issue #1679/#1460: without this the worker's write_pointer()
                  # silently no-ops (correlation_store.py guards on this env var),
                  # so triggering_invocation_id / parent_invocation_id never persist
                  # and agent-to-agent lineage is null. The IRSA role has
                  # UpdateItem on this table (scaledjob-iam.tf) — UpdateItem, not
                  # PutItem, per #1716/#4028.
                  - name: CORRELATION_POINTERS_TABLE
                    value: ${aws_dynamodb_table.correlation_pointers.name}
                  # Issue #3178: correlation marker HMAC signing key (cred-binding S4).
                  # Worker reads this secret from SM to sign outbound markers.
                  - name: ADP_MARKER_SIGNING_KEY_SECRET
                    value: adp/${var.environment}/webhook-ingress/marker-signing-key
                  # Issue #2153: adp-trigger CLI needs the webhook-ingress API
                  # endpoint to POST /agent/trigger (SigV4-signed).
                  - name: ADP_TRIGGER_ENDPOINT
                    value: ${aws_api_gateway_stage.dev.invoke_url}/agent/trigger
                  # Issue #4272: GitHub-token gatekeeper kill-switch. Off by
                  # default = today's behavior (in-pod mint from the platform App
                  # private key). On = the key is never read in this pod and the
                  # gateway mints a repo-scoped token instead. See the
                  # gh_token_broker_enabled variable for the rollback caveat.
                  - name: ADP_GH_TOKEN_BROKER_ENABLED
                    value: "${var.gh_token_broker_enabled || var.agent_authority_enabled ? "1" : "0"}"
                  # Issue #3960: the pod's own IP, from the downward API. The
                  # control listener binds to THIS address specifically; with the
                  # variable absent the worker logs an error and starts no
                  # listener rather than binding every interface. Unconditional
                  # (not flag-gated) so the flag alone turns the feature on.
                  - name: POD_IP
                    valueFrom:
                      fieldRef:
                        fieldPath: status.podIP
${local.agent_control_env_block}
${local.agent_authority_env_block}
${local.gitlab_env_block}
${local.otel_env_block}
${local.knowledge_layer_env_block}
${local.agent_authority_mount_block}
                # Issue #3960: declared so the port is visible in `kubectl
                # describe pod` and to `kubectl port-forward`. containerPort is
                # documentation to the API server, NOT a control — it neither
                # opens nor closes anything, which is why the ingress
                # NetworkPolicy (scaledjob-netpol.tf) is the actual boundary.
                # Declared unconditionally so the manifest does not change shape
                # when the flag flips.
                ports:
                  - name: agent-control
                    containerPort: ${var.agent_control_port}
                    protocol: TCP
                resources:
                  requests:
                    cpu: "1"
                    memory: 4Gi
                    ephemeral-storage: 50Gi
                  limits:
                    cpu: "4"
                    memory: 8Gi
                    ephemeral-storage: 50Gi
                securityContext:
                  allowPrivilegeEscalation: false
                  capabilities:
                    drop:
                      - ALL
${local.agent_authority_volume_block}
      # ── Scaling trigger — DO NOT add scaleOnInFlight or scalingStrategy ─────
      # Issue #4031. This bare trigger is deliberate, not an oversight. KEDA's
      # scaleOnInFlight (scaler: what is COUNTED) and scalingStrategy (executor:
      # what is SUBTRACTED) are a matched pair, and the default pairing is the
      # only internally consistent one for a delete-at-end consumer like ours
      # (the worker holds its message in flight for the whole run, extending
      # visibility via a 120s heartbeat, and deletes only at the end).
      #
      # Default strategy computes: eff = ceil(visible + notVisible) - running.
      # Because each running worker holds exactly one message in flight,
      # notVisible ~= running, so the two cancel and eff ~= visible. Correct.
      #
      #   * scaleOnInFlight: "false" (default strategy) -> eff = visible -
      #     running, double-subtracting live runs. A dispatch arriving in a
      #     DIFFERENT FIFO group during a long run gets no pod until that run
      #     ends — up to activeDeadlineSeconds (6h) — and does not self-heal.
      #   * scalingStrategy: accurate/eager -> subtracts pending, never
      #     running: a visible-but-undeliverable FIFO message spawns a no-op
      #     pod that exits in ~20s, then respawns. A ~25-30s churn loop for the
      #     whole run, amplifying the noise #4031 set out to reduce.
      #
      # ACCEPTED residual: messages queued behind an in-flight member of the
      # same FIFO group are visible but undeliverable, so KEDA still spawns the
      # occasional speculative pod that exits 0. SQS exposes no
      # "deliverable visible" attribute, so NO queue-depth scaler config can
      # see the difference — this is inherent to FIFO, not a misconfiguration.
      # It is bounded and harmless; the clean-exit path handles it, and
      # successfulJobsHistoryLimit: 1 above keeps it out of the pod list.
      #
      # activationQueueLength is intentionally left at its default of 0, which
      # is what makes scale-from-zero work (isActive = metric > 0); setting it
      # would silently break first-message dispatch.
      #
      # Derived from KEDA v2.16.0 source (the version pinned in keda.tf), not
      # the docs — the docs describe scaleOnInFlight without mentioning the
      # strategy's runningJobCount subtraction, which is where the trap hides:
      # pkg/scalers/aws_sqs_queue_scaler.go, pkg/scaling/executor/scale_jobs.go.
      # Full arithmetic: issue #4031 comment 5376105252.
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
    cluster_name   = local.eks_cluster_name
    cluster_region = var.aws_region
  }

  provisioner "local-exec" {
    command = <<-CMD
      set -e
      export KUBECONFIG="$${KUBECONFIG:-$(mktemp)}"
      aws eks update-kubeconfig --name ${local.eks_cluster_name} --region ${var.aws_region} --kubeconfig "$KUBECONFIG" >/dev/null
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
    command    = <<-CMD
      set -e
      export KUBECONFIG="$${KUBECONFIG:-$(mktemp)}"
      aws eks update-kubeconfig --name ${self.triggers.cluster_name} --region ${self.triggers.cluster_region} --kubeconfig "$KUBECONFIG" >/dev/null
      kubectl delete triggerauthentication agent-scaledjob-aws-auth -n ${self.triggers.namespace} --ignore-not-found || true
    CMD
  }

  # RBAC must exist before kubectl apply runs as the runner SA.
  # KEDA CRDs must be installed (helm_release.keda) before applying CRs.
  depends_on = [
    kubernetes_service_account.agent_scaledjob_sa,
    kubernetes_role_binding.runner_keda_manage,
    helm_release.keda,
  ]
}

resource "null_resource" "keda_scaledjob" {
  # A manifest update replaces this Terraform carrier. Deleting the ScaledJob
  # first would cascade into live Jobs. Apply the new template in place instead;
  # full teardown removes the Terraform-owned namespace and its children.
  lifecycle {
    create_before_destroy = true
  }
  triggers = {
    manifest_sha   = sha256(local.keda_scaledjob_yaml)
    namespace      = kubernetes_namespace.adp_agents.metadata[0].name
    cluster_name   = local.eks_cluster_name
    cluster_region = var.aws_region
  }

  provisioner "local-exec" {
    command = <<-CMD
      set -e
      export KUBECONFIG="$${KUBECONFIG:-$(mktemp)}"
      aws eks update-kubeconfig --name ${local.eks_cluster_name} --region ${var.aws_region} --kubeconfig "$KUBECONFIG" >/dev/null
      cat <<'EOF' | kubectl apply -f -
${local.keda_scaledjob_yaml}
EOF
    CMD
  }

  # Destroy provisioner is best-effort — see the keda_trigger_auth comment.
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = <<-CMD
      set -e
      export KUBECONFIG="$${KUBECONFIG:-$(mktemp)}"
      aws eks update-kubeconfig --name ${self.triggers.cluster_name} --region ${self.triggers.cluster_region} --kubeconfig "$KUBECONFIG" >/dev/null
      kubectl delete scaledjob agent-scaledjob -n ${self.triggers.namespace} --cascade=orphan --ignore-not-found || true
    CMD
  }

  # RBAC must exist before kubectl apply runs as the runner SA.
  # KEDA CRDs must be installed (helm_release.keda) before applying CRs.
  depends_on = [
    null_resource.keda_trigger_auth,
    aws_dynamodb_table_item.agent_authority_worker,
    aws_iam_role_policy.agent_authority_worker,
    kubernetes_config_map.agent_control_verification_keys,
    kubernetes_role_binding.runner_keda_manage,
    helm_release.keda,
    terraform_data.worker_security_rollout,
    terraform_data.worker_gateway_rollout,
  ]
}
