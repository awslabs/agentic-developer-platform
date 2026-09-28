# =============================================================================
# gVisor Node Group — Self-managed AL2023 nodes with runsc runtime
# =============================================================================
# Provides a dedicated EKS managed node group where gVisor (runsc) is installed
# via user-data. Nodes are tainted + labeled so ONLY pods that explicitly request
# the gVisor RuntimeClass and tolerate the taint will schedule here.
#
# Design rationale (from #2373 Phase 0 findings):
# - EKS Auto Mode does NOT expose the standard Karpenter EC2NodeClass CRD, so
#   we cannot use karpenter.k8s.aws/v1 EC2NodeClass + NodePool.
# - Bottlerocket does NOT support `enable-gvisor` in its container-runtime config.
# - Solution: AL2023 EKS-optimized AMI (EKS default for 1.35) + user-data script
#   that installs runsc from the gVisor release channel and registers it with
#   containerd. We do NOT specify a custom AMI — EKS manages the AMI lifecycle
#   and automatically injects the node bootstrap (nodeadm NodeConfig). Our
#   user-data is merged as a pre-bootstrap script.
#
# Zero-impact: min=0 scaling + NoSchedule taint = no nodes provision and no
# existing workloads are affected. The flip story (#2376) will add
# runtimeClassName to agent pods.
#
# Cost controls (from #2326): on-demand, amd64, c/m family gen 6a, capped at
# xlarge. Scaling min=0 means zero cost when no gVisor agent work is running.
#
# References: #2358 (parent), #2374, #2373 (Phase 0), #2326 (cost pattern)
# =============================================================================

# -----------------------------------------------------------------------------
# Launch Template — gVisor install via pre-bootstrap user-data
# -----------------------------------------------------------------------------
# NOTE: We intentionally do NOT set image_id. This lets EKS manage the AMI
# (AL2023 for cluster version 1.35) and automatically inject the node bootstrap
# configuration. Our user-data is merged as a MIME part and runs before kubelet
# starts, ensuring containerd has the runsc handler registered at boot.
# -----------------------------------------------------------------------------

resource "aws_launch_template" "gvisor_nodes" {
  name_prefix = "${local.name_prefix}-gvisor-"
  description = "EKS AL2023 nodes with gVisor (runsc) runtime installed via user-data"

  # Use the EKS-managed cluster security group (Auto Mode pattern)
  vpc_security_group_ids = [module.eks.cluster_security_group_id]

  # Pre-bootstrap user-data: installs gVisor before kubelet starts.
  # EKS merges this with its own NodeConfig for AL2023 managed node groups.
  user_data = base64encode(<<-USERDATA
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="==GVISOR=="

--==GVISOR==
Content-Type: text/x-shellscript; charset="us-ascii"

#!/bin/bash
set -euo pipefail

# =============================================================================
# Install gVisor (runsc) from the official release channel.
# This runs as a pre-bootstrap script — containerd picks up the runsc handler
# before kubelet starts, so pods requesting RuntimeClass "gvisor" work
# immediately without a node restart.
# =============================================================================

ARCH=$(uname -m)
if [ "$ARCH" = "x86_64" ]; then
  URL="https://storage.googleapis.com/gvisor/releases/release/latest/x86_64"
elif [ "$ARCH" = "aarch64" ]; then
  URL="https://storage.googleapis.com/gvisor/releases/release/latest/aarch64"
else
  echo "ERROR: Unsupported architecture: $ARCH" >&2
  exit 1
fi

# Download runsc and containerd-shim-runsc-v1
curl -fsSL --retry 3 "$URL/runsc" -o /usr/local/bin/runsc
curl -fsSL --retry 3 "$URL/containerd-shim-runsc-v1" -o /usr/local/bin/containerd-shim-runsc-v1
chmod +x /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1

# Register runsc as a containerd runtime.
# The containerd config on AL2023 EKS uses /etc/containerd/config.toml.
# We append the runsc runtime definition.
cat >> /etc/containerd/config.toml <<'EOF'

# gVisor (runsc) runtime handler — installed by ADP platform (#2374)
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
EOF

# Restart containerd to pick up the new runtime handler
systemctl restart containerd

--==GVISOR==--
  USERDATA
  )

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name                        = "${local.name_prefix}-gvisor-node"
      "adp.io/runtime"            = "gvisor"
      "app.kubernetes.io/part-of" = "adp-platform"
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(local.common_tags, {
      Name = "${local.name_prefix}-gvisor-node-volume"
    })
  }

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-gvisor-launch-template"
    Purpose = "gvisor-node-group"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# -----------------------------------------------------------------------------
# EKS Managed Node Group — gVisor-capable nodes
# -----------------------------------------------------------------------------

resource "aws_eks_node_group" "gvisor" {
  cluster_name    = module.eks.cluster_name
  node_group_name = "${local.name_prefix}-gvisor"
  node_role_arn   = module.iam.eks_node_group_role_arn
  subnet_ids      = module.networking.private_subnet_ids

  # Instance diversity: compute-optimized + general-purpose, gen 6a (AMD),
  # capped at xlarge (4 vCPU / 8-16 GiB) per #2326 cost principles.
  instance_types = ["c6a.large", "c6a.xlarge", "m6a.large", "m6a.xlarge"]
  capacity_type  = "ON_DEMAND"

  # AMI type: AL2023 x86_64 standard — EKS manages AMI version + node bootstrap.
  ami_type = "AL2023_x86_64_STANDARD"

  scaling_config {
    min_size     = 0
    max_size     = 4
    desired_size = 0
  }

  # Labels for nodeSelector-based scheduling
  labels = {
    "adp.io/runtime" = "gvisor"
  }

  # Taint ensures only pods that tolerate gVisor land here
  taint {
    key    = "adp.io/runtime"
    value  = "gvisor"
    effect = "NO_SCHEDULE"
  }

  launch_template {
    id      = aws_launch_template.gvisor_nodes.id
    version = aws_launch_template.gvisor_nodes.latest_version
  }

  # Allow rolling updates without downtime
  update_config {
    max_unavailable = 1
  }

  tags = merge(local.common_tags, {
    Name                        = "${local.name_prefix}-gvisor-nodegroup"
    Purpose                     = "gvisor-agent-runtime"
    "app.kubernetes.io/part-of" = "adp-platform"
  })

  depends_on = [module.eks]
}
