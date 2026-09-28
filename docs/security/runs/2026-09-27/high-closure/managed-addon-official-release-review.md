# Managed CloudWatch official release review — 2026-09-27

Read-only; no IAM/RBAC/namespace changes. Current EKS CloudWatch add-on is v6.7.0-eksbuild.1, also newest compatible release returned by EKS. Its schema has no agent/manager image override and no manager.autoInstrumentationImage override. The official Helm chart does expose those fields, but Helm configuration is not automatically valid EKS add-on configuration.

Current agent1.300073.0b1828 and operator3.7.0 match upstream Helm main. The latest GitHub agent release1.300072.0 is older than the managed image; operator3.7.0 is current. Do not downgrade agent based on GitHub latest-tag ordering.

Official releases newer than chart defaults:

- Python0.20.0 (Sep22), https://github.com/aws-observability/aws-otel-python-instrumentation/releases/tag/v0.20.0. Public amd64 image `public.ecr.aws/aws-observability/adot-autoinstrumentation-python@sha256:3365893394dfea43151cef5ecfb6ad9e85391d00370c74458c83a12721119457`. Frozen scan76 packages, zero native matches. No runtime injection acceptance performed.
- Node0.13.0 (Sep22), https://github.com/aws-observability/aws-otel-js-instrumentation/releases/tag/v0.13.0. Public amd64 image `public.ecr.aws/aws-observability/adot-autoinstrumentation-node@sha256:edaa05eca4acdaa7775bbe61e9c3098ac0b80f07a9df738f7c967a5222a6eb73`. Frozen scan270 packages, zero Critical/one High: @opentelemetry/propagator-jaeger2.8.0 GHSA-45rx-2jwx-cxfr, fixed2.9.0. Newer version alone does not close Node High.
- Java2.31.0 newer than chart2.30.0, https://github.com/aws-observability/aws-otel-java-instrumentation/releases/tag/v2.31.0. Not scanned; Java instrumentation is disabled in current closure scope.
- .NET1.15.0 matches chart/default, https://github.com/aws-observability/aws-otel-dotnet-instrumentation/releases/tag/v1.15.0. No newer vendor candidate found.

The installed `cloudwatch.aws.amazon.com/v1alpha1` Instrumentation CRD supports `spec.python.image` (kubectl explain verified), and the official operator README documents creating an Instrumentation in the workload namespace. No Instrumentation CR exists in authorized adp-agents/adp-gateway namespaces. Cluster-wide listing is Forbidden under the existing role; no access changes attempted. A per-workload CR can select a supported official image for that workload after preserving its injection/exporter configuration, but it does not replace the operator's enabled global/default image references; it cannot on its own prove conservative desired-image closure.

Supported vendor configuration alternatives are a newer EKS add-on release when available, or a separately reviewed move to the official Helm chart that exposes image inputs while preserving observability configuration and identity. A direct patch to an EKS-managed DaemonSet/operator can be reconciled away and is not a durable vendor-configured path. No migration was prepared or executed. Current upstream images do not offer zero-High agent/operator/.NET/Node closure.

Evidence: cloudwatch-supported-versions.json, cloudwatch-addon-current.private.json, cloudwatch-addon-schema.json, cloudwatch-upstream-values.yaml, cloudwatch-operator-readme.md, cloudwatch-operator-api.md, adot-{python,node}-latest-scan/. Candidate scan symlinks added to security27-high-closure; these are not live promotion claims.
