# Critical remediation coverage

59 baseline advisories. 25 absent from the current conservative image/package register; 34 remain open. Candidate scans are not automatically deducted.

| Advisory | State | Remaining workloads |
|---|---|---|
| CVE-2022-32511 | open Critical | superplane/Deployment/superplane-controller |
| CVE-2022-48174 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2024-41110 | open Critical | adp-agents/Deployment/adot-collector<br>superplane/Deployment/superplane-controller |
| CVE-2024-45337 | open Critical | adp-agents/Deployment/adot-collector<br>keda/Deployment/keda-admission-webhooks<br>keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver<br>superplane/Deployment/superplane-controller |
| CVE-2025-22871 | open Critical | adp-agents/Deployment/adot-collector<br>keda/Deployment/keda-admission-webhooks<br>keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver<br>kube-system/DaemonSet/s3-csi-node<br>superplane/Deployment/superplane-controller |
| CVE-2025-3277 | open Critical | superplane/Deployment/superplane-controller |
| CVE-2025-68121 | open Critical | adp-agents/Deployment/adot-collector<br>arc-systems/AutoscalingListener/arc-runner-agent-5f66b876-listener<br>arc-systems/AutoscalingListener/arc-runner-cip-744d6f9f-listener<br>arc-systems/AutoscalingListener/arc-runner-org-5f66b876-listener<br>arc-systems/Deployment/arc-gha-rs-controller<br>keda/Deployment/keda-admission-webhooks<br>keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver<br>kube-system/DaemonSet/s3-csi-node<br>superplane/Deployment/superplane-controller<br>superplane/Deployment/superplane-platform-monitor |
| CVE-2026-10536 | open Critical | adp-agents/DaemonSet/agent-image-prepull<br>adp-agents/ScaledJob/agent-scaledjob<br>adp-gateway/Deployment/authority-probe-gateway-20260920<br>superplane/Deployment/superplane-api<br>superplane/Deployment/superplane-controller |
| CVE-2026-11856 | open Critical | adp-agents/DaemonSet/agent-image-prepull<br>adp-agents/ScaledJob/agent-scaledjob<br>adp-gateway/Deployment/authority-probe-gateway-20260920<br>superplane/Deployment/superplane-api<br>superplane/Deployment/superplane-controller |
| CVE-2026-12087 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-13221 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-18924 | open Critical | adp-agents/DaemonSet/agent-image-prepull<br>adp-agents/ScaledJob/agent-scaledjob<br>adp-gateway/Deployment/authority-probe-gateway-20260920<br>superplane/Deployment/superplane-api<br>superplane/Deployment/superplane-controller |
| CVE-2026-19931 | open Critical | adp-agents/DaemonSet/agent-image-prepull<br>adp-agents/ScaledJob/agent-scaledjob<br>adp-gateway/Deployment/authority-probe-gateway-20260920<br>superplane/Deployment/superplane-api<br>superplane/Deployment/superplane-controller |
| CVE-2026-31789 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-33186 | open Critical | adp-agents/Deployment/adot-collector<br>keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver<br>kube-system/DaemonSet/s3-csi-node<br>superplane/Deployment/superplane-controller |
| CVE-2026-33815 | open Critical | keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver |
| CVE-2026-33816 | open Critical | keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver |
| CVE-2026-33845 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-34182 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-39830 | open Critical | adp-agents/Deployment/adot-collector<br>arc-systems/AutoscalingListener/arc-runner-agent-5f66b876-listener<br>arc-systems/AutoscalingListener/arc-runner-cip-744d6f9f-listener<br>arc-systems/AutoscalingListener/arc-runner-org-5f66b876-listener<br>arc-systems/Deployment/arc-gha-rs-controller<br>keda/Deployment/keda-admission-webhooks<br>keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver<br>superplane/Deployment/superplane-controller |
| CVE-2026-39831 | open Critical | adp-agents/Deployment/adot-collector<br>arc-systems/AutoscalingListener/arc-runner-agent-5f66b876-listener<br>arc-systems/AutoscalingListener/arc-runner-cip-744d6f9f-listener<br>arc-systems/AutoscalingListener/arc-runner-org-5f66b876-listener<br>arc-systems/Deployment/arc-gha-rs-controller<br>keda/Deployment/keda-admission-webhooks<br>keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver<br>superplane/Deployment/superplane-controller |
| CVE-2026-39832 | open Critical | adp-agents/Deployment/adot-collector<br>arc-systems/AutoscalingListener/arc-runner-agent-5f66b876-listener<br>arc-systems/AutoscalingListener/arc-runner-cip-744d6f9f-listener<br>arc-systems/AutoscalingListener/arc-runner-org-5f66b876-listener<br>arc-systems/Deployment/arc-gha-rs-controller<br>keda/Deployment/keda-admission-webhooks<br>keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver<br>superplane/Deployment/superplane-controller |
| CVE-2026-39833 | open Critical | adp-agents/Deployment/adot-collector<br>arc-systems/AutoscalingListener/arc-runner-agent-5f66b876-listener<br>arc-systems/AutoscalingListener/arc-runner-cip-744d6f9f-listener<br>arc-systems/AutoscalingListener/arc-runner-org-5f66b876-listener<br>arc-systems/Deployment/arc-gha-rs-controller<br>keda/Deployment/keda-admission-webhooks<br>keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver<br>superplane/Deployment/superplane-controller |
| CVE-2026-39834 | open Critical | adp-agents/Deployment/adot-collector<br>arc-systems/AutoscalingListener/arc-runner-agent-5f66b876-listener<br>arc-systems/AutoscalingListener/arc-runner-cip-744d6f9f-listener<br>arc-systems/AutoscalingListener/arc-runner-org-5f66b876-listener<br>arc-systems/Deployment/arc-gha-rs-controller<br>keda/Deployment/keda-admission-webhooks<br>keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver<br>superplane/Deployment/superplane-controller |
| CVE-2026-42010 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-42496 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-42508 | open Critical | adp-agents/Deployment/adot-collector<br>arc-systems/AutoscalingListener/arc-runner-agent-5f66b876-listener<br>arc-systems/AutoscalingListener/arc-runner-cip-744d6f9f-listener<br>arc-systems/AutoscalingListener/arc-runner-org-5f66b876-listener<br>arc-systems/Deployment/arc-gha-rs-controller<br>keda/Deployment/keda-admission-webhooks<br>keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver<br>superplane/Deployment/superplane-controller |
| CVE-2026-46595 | open Critical | adp-agents/Deployment/adot-collector<br>arc-systems/AutoscalingListener/arc-runner-agent-5f66b876-listener<br>arc-systems/AutoscalingListener/arc-runner-cip-744d6f9f-listener<br>arc-systems/AutoscalingListener/arc-runner-org-5f66b876-listener<br>arc-systems/Deployment/arc-gha-rs-controller<br>keda/Deployment/keda-admission-webhooks<br>keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver<br>superplane/Deployment/superplane-controller |
| CVE-2026-48930 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-50195 | open Critical | superplane/Deployment/superplane-controller |
| CVE-2026-53492 | open Critical | superplane/Deployment/superplane-controller |
| CVE-2026-53790 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-53791 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-53793 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-54133 | open Critical | superplane/Deployment/superplane-controller |
| CVE-2026-5450 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-56123 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-57433 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-58016 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-59873 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-60002 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-63073 | open High; Critical occurrences removed | amazon-cloudwatch/DaemonSet/fluent-bit |
| CVE-2026-63374 | open Critical | adp-gateway/Deployment/authority-probe-gateway-20260920 |
| CVE-2026-70452 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-70460 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-75595 | open Critical | adp-agents/Deployment/adot-collector |
| CVE-2026-75604 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-75803 | open High; Critical occurrences removed | amazon-cloudwatch/DaemonSet/fluent-bit |
| CVE-2026-7598 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-77405 | open Critical | keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver |
| CVE-2026-77408 | open Critical | keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver |
| CVE-2026-77411 | open Critical | keda/Deployment/keda-operator<br>keda/Deployment/keda-operator-metrics-apiserver |
| CVE-2026-78676 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-8376 | closed in reconciled image/package scope | No remaining matched occurrence |
| CVE-2026-8924 | open Critical | adp-agents/DaemonSet/agent-image-prepull<br>adp-agents/ScaledJob/agent-scaledjob<br>adp-gateway/Deployment/authority-probe-gateway-20260920<br>superplane/Deployment/superplane-api<br>superplane/Deployment/superplane-controller |
| CVE-2026-8926 | open Critical | adp-agents/DaemonSet/agent-image-prepull<br>adp-agents/ScaledJob/agent-scaledjob<br>adp-gateway/Deployment/authority-probe-gateway-20260920<br>superplane/Deployment/superplane-api<br>superplane/Deployment/superplane-controller |
| CVE-2026-8927 | open Critical | adp-agents/DaemonSet/agent-image-prepull<br>adp-agents/ScaledJob/agent-scaledjob<br>adp-gateway/Deployment/authority-probe-gateway-20260920<br>superplane/Deployment/superplane-api<br>superplane/Deployment/superplane-controller |
| CVE-2026-9079 | open Critical | adp-agents/DaemonSet/agent-image-prepull<br>adp-agents/ScaledJob/agent-scaledjob<br>adp-gateway/Deployment/authority-probe-gateway-20260920<br>superplane/Deployment/superplane-api<br>superplane/Deployment/superplane-controller |
| GHSA-2xp9-vwfh-vxw4 | closed in reconciled image/package scope | No remaining matched occurrence |
