# Observability Critical repairs

Pin the CloudWatch EKS add-on to v6.7.0-eksbuild.1 and the independent ADOT
collector to the scanned v0.50.0 digest. Frozen raw candidate scans report zero
Critical matches for the operator, agent, Fluent Bit and collector. The EKS
upgrade uses PRESERVE and retains the existing IRSA role; live acceptance is
recorded separately.

The CloudWatch auto-annotator injected Java agents into the Python/Node DeepWiki
service, Python gateway/MCP/LiteLLM services and Go Zoekt/ADOT binaries. Those
workloads do not run JVMs. Explicitly disable Java auto-annotation and injection
on their pod templates so unused vulnerable JARs are no longer copied by init
containers. Python and Node instrumentation settings are preserved. Existing
pods must roll before these image occurrences can close.

Java v2.31.0 also scans 0/0 and is available for actual JVM workloads; the
CloudWatch chart's bundled v2.30.0 still matches the Critical Netty advisory.
No claim that the bundled agent is fixed is made.

The ADOT wrapper does not implement the upstream validate subcommand. A real
isolated collector started with the production-shaped config and all AWS
exporter types, returned healthy, and accepted empty JSON OTLP traces, metrics
and logs with HTTP200. Synthetic credentials and a network-none namespace kept
this fixture offline. AWS export acceptance still requires a live canary.

Five deployment YAML templates and the changed HCL files parsed successfully;
Terraform formatting passed. Candidate and live totals are separate.
