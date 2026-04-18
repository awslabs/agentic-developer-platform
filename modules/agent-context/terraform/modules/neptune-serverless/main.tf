###############################################################################
# Neptune Serverless Module — graph database for GraphRAG knowledge graph
#
# Creates a Neptune Serverless cluster with IAM authentication, VPC networking,
# and IRSA roles for pod-level access from EKS.
#
# Neptune Serverless scales to zero when idle (~$0.12/NCU-hour when active).
###############################################################################

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id       = data.aws_caller_identity.current.account_id
  partition        = data.aws_partition.current.partition
  oidc_provider_id = replace(var.oidc_provider_url, "https://", "")
}

# ─── Subnet Group ────────────────────────────────────────────────────────────

resource "aws_neptune_subnet_group" "graphrag" {
  name       = "${var.cluster_name}-graphrag"
  subnet_ids = var.subnet_ids

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-graphrag-subnet-group"
  })
}

# ─── Security Group ──────────────────────────────────────────────────────────

resource "aws_security_group" "neptune" {
  name_prefix = "agent-context-neptune-"
  description = "Allow Neptune traffic from EKS nodes"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, {
    Name = "agent-context-neptune"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group_rule" "neptune_ingress" {
  type                     = "ingress"
  from_port                = 8182
  to_port                  = 8182
  protocol                 = "tcp"
  security_group_id        = aws_security_group.neptune.id
  source_security_group_id = var.node_security_group_id
  description              = "Neptune from EKS nodes"
}

resource "aws_security_group_rule" "neptune_egress" {
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  security_group_id = aws_security_group.neptune.id
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "Allow all outbound"
}

# ─── Neptune Serverless Cluster ──────────────────────────────────────────────

resource "aws_neptune_cluster" "graphrag" {
  cluster_identifier                   = "${var.cluster_name}-graphrag"
  engine                               = "neptune"
  engine_version                       = var.neptune_engine_version
  iam_database_authentication_enabled  = true
  neptune_subnet_group_name            = aws_neptune_subnet_group.graphrag.name
  vpc_security_group_ids               = [aws_security_group.neptune.id]
  storage_encrypted                    = true
  skip_final_snapshot                  = var.skip_final_snapshot
  final_snapshot_identifier            = var.skip_final_snapshot ? null : "${var.cluster_name}-graphrag-final"
  apply_immediately                    = true
  deletion_protection                  = var.deletion_protection

  serverless_v2_scaling_configuration {
    min_capacity = var.min_capacity
    max_capacity = var.max_capacity
  }

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-graphrag"
  })
}

# ─── Neptune Serverless Instance ─────────────────────────────────────────────

resource "aws_neptune_cluster_instance" "graphrag" {
  cluster_identifier = aws_neptune_cluster.graphrag.id
  instance_class     = "db.serverless"
  engine             = "neptune"
  identifier         = "${var.cluster_name}-graphrag-01"
  apply_immediately  = true

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-graphrag-01"
  })
}

# ─── IAM Role for Pod Access (IRSA) ─────────────────────────────────────────

data "aws_iam_policy_document" "neptune_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:oidc-provider/${local.oidc_provider_id}"]
    }

    condition {
      test     = "StringLike"
      variable = "${local.oidc_provider_id}:sub"
      values = [
        "system:serviceaccount:${var.namespace}:*",
      ]
    }

    condition {
      test     = "StringLike"
      variable = "${local.oidc_provider_id}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "neptune_access" {
  name               = "${var.cluster_name}-neptune-access"
  assume_role_policy = data.aws_iam_policy_document.neptune_assume.json

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-neptune-access"
  })
}

data "aws_iam_policy_document" "neptune_access" {
  statement {
    effect = "Allow"
    actions = [
      "neptune-db:connect",
      "neptune-db:ReadDataViaQuery",
      "neptune-db:WriteDataViaQuery",
      "neptune-db:DeleteDataViaQuery",
      "neptune-db:GetQueryStatus",
      "neptune-db:CancelQuery",
    ]
    resources = [
      "arn:${local.partition}:neptune-db:${var.aws_region}:${local.account_id}:${aws_neptune_cluster.graphrag.cluster_resource_id}/*",
    ]
  }
}

resource "aws_iam_policy" "neptune_access" {
  name   = "${var.cluster_name}-neptune-access"
  policy = data.aws_iam_policy_document.neptune_access.json

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-neptune-access"
  })
}

resource "aws_iam_role_policy_attachment" "neptune_access" {
  role       = aws_iam_role.neptune_access.name
  policy_arn = aws_iam_policy.neptune_access.arn
}
