# Managed EKS add-on candidates

Target: account 879318057152, cluster adp-dev-eks-cluster, us-east-1, Kubernetes 1.35.

| Add-on | Installed | Candidate | Candidate raw Critical / High |
|---|---|---|---|
| metrics-server | v0.9.0-eksbuild.7 | v0.9.0-eksbuild.11 | 0 / 0 |
| CoreDNS | v1.13.2-eksbuild.31 | v1.14.6-eksbuild.4 | 0 / 0 |

AWS compatibility responses and exact image/config digests are retained alongside
this document. Scans use the frozen 2026-09-26 Grype database without suppressions.
Metrics-server binary startup/version passed. Terraform validate and all 25 existing module tests passed.
Metrics-server is the same upstream 0.9 release with an AWS packaging rebuild;
Terraform now pins that reviewed rebuild. CoreDNS is a minor-version candidate,
not yet a completed upgrade review or Terraform-managed release.

CoreDNS passed real UDP and TCP DNS queries, health and readiness in an isolated
network namespace using the included fixture. Its executable requires
NET_BIND_SERVICE (file capabilities); dropping every capability prevents exec.
Production Corefile uses the standard kubernetes, forward, health/ready, cache,
loop, reload, loadbalance and prometheus plugins. Kubernetes discovery and real
service/external DNS still require live acceptance. Fixture DNS is not that check.

No live change was made. The current dev-box identity cannot read metrics API
resources or patch deployments. Both installed add-ons report ACTIVE with no
health issues; metrics-server has two ready replicas. No custom configuration
values or service-account/pod-identity associations were reported by DescribeAddon.
Private deployment/config backups remain in the task evidence directory.

Before an authorized rollout, verify the existing deployment identity can read
APIService availability, node/pod metrics and HPA conditions. Preserve current
configuration and use the EKS managed update API (resolve conflicts PRESERVE),
not a Kubernetes image patch. Roll one add-on at a time. Require update success,
ready replicas, actual pod imageIDs and functional checks before closure.
Rollback to the recorded installed version with configuration preserved if
acceptance fails; document that rollback restores its previous CVE exposure.
CoreDNS additionally requires review of 1.13-to-1.14 upstream release notes,
configuration/plugin compatibility, DNS canary and Kubernetes service discovery.
