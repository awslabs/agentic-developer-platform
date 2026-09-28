# Plan-only, mocked-provider assertions that this module provisions nothing for tenant
# workloads on the ADP management cluster (#5051, R12 acceptance 5).
#
# SCOPE — read this before trusting a green run. These assertions are over the
# DECLARED CONFIGURATION of this Terraform module. They establish that the management
# cluster's own definition creates no capacity, namespace, service account or AWS
# identity for a tenant workload. They do NOT observe a scheduler, and they cannot:
#
#   - An operator with cluster-admin can still `kubectl apply` a pod. Nothing in
#     Terraform prevents that; access control is what limits who can.
#   - Kubernetes objects applied outside this module are out of view — including the
#     custom Karpenter NodePools in platform/infra/nodepool-*.tf, which are applied
#     via null_resource + kubectl and are not in this module's plan.
#
# So this file's claim is narrow and checkable offline: the management cluster is
# declared with platform compute and platform identity only. Whether a tenant pod can
# in fact be scheduled on a live cluster is not settled here.

mock_provider "aws" {}
mock_provider "kubernetes" {}
mock_provider "tls" {}
mock_provider "time" {}

variables {
  environment             = "dev"
  name_prefix             = "adp-dev"
  vpc_id                  = "vpc-00000000000000000"
  private_subnet_ids      = ["subnet-00000000000000001", "subnet-00000000000000002"]
  eks_security_group_id   = "sg-00000000000000000"
  eks_cluster_role_arn    = "arn:aws:iam::123456789012:role/adp-dev-role-eks-cluster"
  node_group_role_arn     = "arn:aws:iam::123456789012:role/adp-dev-role-eks-node-group"
  eks_public_access_cidrs = ["10.0.0.0/8"]
}

run "management_cluster_declares_no_tenant_compute_or_identity" {
  command = plan

  variables {
    # Worst case for this check: pod identity ON, so the association set is non-empty
    # and its namespace confinement is actually exercised rather than trivially true.
    enable_gateway_pod_identity  = true
    cluster_admin_principal_arns = ["arn:aws:iam::123456789012:role/Admin"]
  }

  override_data {
    target = data.aws_region.current
    values = {
      name = "us-east-1"
    }
  }

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  override_resource {
    target = aws_eks_cluster.main
    values = {
      identity = [{
        oidc = [{
          issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
        }]
      }]
      certificate_authority = [{
        data = "TFNUQVJUQ0VSVElGSUNBVEU="
      }]
    }
  }

  override_data {
    target = data.tls_certificate.cluster
    values = {
      certificates = [{
        sha1_fingerprint = "0123456789abcdef0123456789abcdef01234567"
      }]
    }
  }

  override_resource {
    target = aws_iam_role.gateway_service_irsa
    values = {
      arn = "arn:aws:iam::123456789012:role/adp-dev-role-gateway-service"
    }
  }

  # Compute: Auto Mode's own general-purpose pool and nothing else. A tenant pool
  # added here would be tenant capacity attached to the management cluster, which is
  # the shape acceptance 5 forbids.
  assert {
    condition     = toset(aws_eks_cluster.main.compute_config[0].node_pools) == toset(["general-purpose"])
    error_message = "The management cluster must declare only the general-purpose Auto Mode node pool; an extra pool here would be tenant capacity on the management cluster."
  }

  assert {
    condition     = aws_eks_cluster.main.compute_config[0].node_role_arn == var.node_group_role_arn
    error_message = "Auto Mode nodes must use the platform node role, not a per-tenant role."
  }

  # Namespaces: this module creates exactly one, for the gateway. A tenant namespace
  # created here would be a place for a tenant workload to land with no further step.
  assert {
    condition     = kubernetes_namespace.bedrockgw.metadata[0].name == "bedrockgw"
    error_message = "The only namespace this module creates must be the gateway's."
  }

  # Service accounts: one, the gateway's. A workload needs an identity to do anything
  # useful; this module grants exactly one and it is a platform component's.
  assert {
    condition = (
      kubernetes_service_account.gateway_service.metadata[0].name == "gateway-service" &&
      kubernetes_service_account.gateway_service.metadata[0].namespace == "bedrockgw"
    )
    error_message = "The only service account this module creates must be the gateway's, in the gateway namespace."
  }

  # AWS identity delivered into the cluster is confined to the gateway's namespaces.
  # An association in a tenant namespace would hand a tenant pod an ADP role on the
  # management cluster.
  assert {
    condition     = toset([for _, assoc in aws_eks_pod_identity_association.gateway_service : assoc.namespace]) == toset(["bedrockgw", "adp-gateway"])
    error_message = "Pod-identity associations must not reach outside the gateway's namespaces on the management cluster."
  }

  # Cluster access is only what the caller explicitly supplied. An entry this module
  # synthesised would be an unreviewed principal on the management cluster.
  assert {
    condition     = toset(keys(aws_eks_access_entry.admins)) == toset(var.cluster_admin_principal_arns)
    error_message = "Access entries must come only from cluster_admin_principal_arns; a synthesised entry is an unreviewed principal on the management cluster."
  }

  # Every access entry is a standard IAM principal grant. EC2_LINUX / FARGATE_LINUX
  # entry types register node identities, which is not what an operator grant is.
  assert {
    condition     = alltrue([for _, entry in aws_eks_access_entry.admins : entry.type == "STANDARD"])
    error_message = "Operator access entries must be STANDARD; a node-registration entry type here would be capacity joining the cluster."
  }

  # Secrets encryption stays on. Isolation of the control plane's stored state is part
  # of what makes co-residency on this cluster consequential.
  assert {
    condition     = toset(aws_eks_cluster.main.encryption_config[0].resources) == toset(["secrets"])
    error_message = "Management-cluster secrets encryption must remain enabled."
  }
}
