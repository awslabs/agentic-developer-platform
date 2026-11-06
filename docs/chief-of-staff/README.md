# Chief of Staff / ADP Assistant

[**Architecture**](architecture.md) is the single canonical design for epic
[#7197](https://github.com/aws-e/adp/issues/7197): product, foundation/runtime,
memory, managed Dev Box, delivery order, deployment policy and verification.
The selected memory design uses **AgentCore through the ADP gateway** for shared
user memory. **Crew runs on block storage**, with an external helper creating
consistent backup archives uploaded **directly to S3** from EKS or EC2. A small
checkpoint table in ADP tracks status, exact object versions and integrity; an
atomic latest-verified pointer selects the recovery copy. Restore happens before
Crew starts; live databases are not synchronized in two directions. No S3 Files
mount or Kiro memory changes/plugins are required. Implementation, migration and qualification
remain pending; unresolved decisions and their owners are recorded there.

[Manual Dev Box setup](devbox-setup.md) is the historical Kiro installation
runbook. It does not establish managed EC2 service readiness.

The [`chat-agent-deploy.yml` deployment pipeline](architecture.md#deployment-boundary)
builds distinct digest-pinned supervisor and restricted crew images and publishes
release evidence for engine verification. Engine dispatch checks its source and
workflow definition before deployment; manual dev dispatch and existing environment
protections remain. The registered chat deployment target is unresolved until an
operator verifies the dev connection and reviewed revisions. A live receipt still
requires a post-merge engine deployment and a NetworkPolicy enforcement probe;
the local fixtures alone do not establish that outcome.

[Crew dashboard edge](crew-dashboard-edge.md) documents its opt-in hostname, activation gate and operator-owned live checks.
