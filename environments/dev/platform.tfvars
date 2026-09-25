environment = "dev"
aws_region  = "us-east-1"

vpc_cidr           = "10.0.0.0/16"
az_count           = 2
single_nat_gateway = true

eks_cluster_version     = "1.35"
eks_node_instance_types = ["m5.large", "m5.xlarge"]
eks_node_desired_size   = 2
eks_node_min_size       = 1
eks_node_max_size       = 10

# Enable CloudWatch Container Insights (amazon-cloudwatch-observability addon).
# Ships EKS node/pod metrics (node_count, pod CPU/mem, restarts, pods-pending)
# to the ContainerInsights namespace — the data source the platform health
# alarms (#920) require. Without this the platform layer has zero pod/node
# visibility (the blind spot that hid the subnet-IP-exhaustion incident).
# Adds CloudWatch metric + log ingestion cost (bounded by cluster size).
enable_container_insights = true

# Enforce NetworkPolicy (#4999). Until this was set, the cluster ran no
# network-policy enforcement agent, so all four existing policies were inert:
# worker egress was not actually restricted, and the agent control-listener
# ingress boundary that evaluation #3967 must prove (check W1-04) could not be
# proven because a deny-all policy let traffic through.
#
# ORDERING IS LOAD-BEARING. This must not be applied until the ADOT collector
# egress policy has already been applied by the webhook-ingress module
# (modules/agent-factory/webhook-ingress/infra/scaledjob-netpol.tf). The
# namespace's default-deny-egress selects all pods; before that policy existed
# the collector matched no allow rule, so enabling enforcement first stops all
# agent traces/metrics/logs with no error visible anywhere. See
# docs/runbooks/network-policy-enforcement.md for the ordered procedure,
# post-apply verification and rollback.
enable_network_policy_controller = true

# Human operator role(s) that need EKS cluster-admin, beyond the deploying
# caller and the CI runner (those two are added automatically in main.tf).
# Without an entry here, a CI apply (running as agent-runner-role) can destroy a
# human operator's access entry and lock them out of kubectl.
#
# Intentionally EMPTY in the shipped repo (issue #4027). This previously
# hardcoded ADP's own dev-account role ARN, which broke every self-managed
# deploy: applied as-is it grants a *foreign* account's role cluster-admin on
# the customer's cluster, and rewritten to the local account by deploy.sh it
# names a role that doesn't exist in an IAM Identity Center account — the same
# `InvalidParameterException: invalid principal` this issue fixes for the
# deployer. Account-specific by nature, so it can't be derived or shipped.
#
# Operators: set your own durable operator ARN(s) per-invocation via
#   export TF_VAR_extra_cluster_admin_principal_arns='["arn:aws:iam::<acct>:role/<Role>"]'
# Prefer a stable IAM role over an Identity Center permission-set role: the
# AWSReservedSSO_<PermissionSet>_<suffix> name changes if the permission set is
# re-provisioned, and it differs per permission set — so an access entry derived
# from an SSO session is not durable. principal_arn is ForceNew, so a changed
# name means destroy+create of the access entry on the next apply.
# distinct() in main.tf dedupes if the deployer is already listed here.
#
# NOT assigned here, deliberately — same reason as eks_public_access_cidrs
# below. A `-var-file` assignment OVERRIDES TF_VAR_ environment variables, so
# an explicit `= []` on this line would silently defeat every TF_VAR_ override:
# CI's passthrough (platform-infra-apply.yml reads the EXTRA_CLUSTER_ADMIN_ARNS
# repository variable) and the operator export above would both be ignored, and
# the apply would destroy the operator's access entry anyway. The variable's
# declared default in platform/infra/variables.tf is already [], so leaving it
# unassigned keeps the shipped repo portable AND keeps the override working.
# Set EXTRA_CLUSTER_ADMIN_ARNS (repo variable) to this account's operator ARNs.

# `eks_public_access_cidrs` is intentionally NOT set here so the repo stays
# portable. Set it per-invocation via:
#   export TF_VAR_eks_public_access_cidrs='["<your.public.ip>/32"]'
# The deploy-all.sh and preflight-check.sh scripts autodetect the operator's IP
# when this variable is unset.

# The legacy customer-source role must not regain platform Kubernetes access.
agent_legacy_worker_admin_retired      = true
agent_authority_legacy_workers_drained = true
