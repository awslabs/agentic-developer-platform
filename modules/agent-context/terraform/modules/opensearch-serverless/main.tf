###############################################################################
# OpenSearch Serverless Module — vector store for GraphRAG entity embeddings
#
# Creates an OpenSearch Serverless collection (VECTORSEARCH type) with
# encryption, network, and data access policies. Used by the GraphRAG toolkit
# for entity embedding similarity search.
#
# OpenSearch Serverless: ~$0.24/OCU-hour (minimum 2 OCUs when active).
###############################################################################

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id       = data.aws_caller_identity.current.account_id
  partition        = data.aws_partition.current.partition
  oidc_provider_id = replace(var.oidc_provider_url, "https://", "")
  collection_name  = var.collection_name
}

# ─── Encryption Policy ──────────────────────────────────────────────────────

resource "aws_opensearchserverless_security_policy" "encryption" {
  name = "${local.collection_name}-enc"
  type = "encryption"

  policy = jsonencode({
    Rules = [
      {
        Resource     = ["collection/${local.collection_name}"]
        ResourceType = "collection"
      }
    ]
    AWSOwnedKey = true
  })
}

# ─── Network Policy ─────────────────────────────────────────────────────────

resource "aws_opensearchserverless_security_policy" "network" {
  name = "${local.collection_name}-net"
  type = "network"

  policy = var.allow_public_access ? jsonencode([
    {
      Description = "Public access for GraphRAG collection"
      Rules = [
        {
          Resource     = ["collection/${local.collection_name}"]
          ResourceType = "collection"
        },
        {
          Resource     = ["collection/${local.collection_name}"]
          ResourceType = "dashboard"
        }
      ]
      AllowFromPublic = true
    }
    ]) : jsonencode([
    {
      Description = "VPC access for GraphRAG collection"
      Rules = [
        {
          Resource     = ["collection/${local.collection_name}"]
          ResourceType = "collection"
        },
        {
          Resource     = ["collection/${local.collection_name}"]
          ResourceType = "dashboard"
        }
      ]
      AllowFromPublic = false
      SourceVPCEs     = var.vpc_endpoint_ids
    }
  ])
}

# ─── Data Access Policy ──────────────────────────────────────────────────────

resource "aws_opensearchserverless_access_policy" "data" {
  name = "${local.collection_name}-data"
  type = "data"

  policy = jsonencode([
    {
      Description = "Data access for GraphRAG ingestion and query"
      Rules = [
        {
          Resource     = ["collection/${local.collection_name}"]
          ResourceType = "collection"
          Permission = [
            "aoss:CreateCollectionItems",
            "aoss:DeleteCollectionItems",
            "aoss:UpdateCollectionItems",
            "aoss:DescribeCollectionItems",
          ]
        },
        {
          Resource     = ["index/${local.collection_name}/*"]
          ResourceType = "index"
          Permission = [
            "aoss:CreateIndex",
            "aoss:DeleteIndex",
            "aoss:UpdateIndex",
            "aoss:DescribeIndex",
            "aoss:ReadDocument",
            "aoss:WriteDocument",
          ]
        }
      ]
      Principal = concat(
        [aws_iam_role.opensearch_access.arn],
        var.additional_access_arns,
      )
    }
  ])
}

# ─── Collection ──────────────────────────────────────────────────────────────

resource "aws_opensearchserverless_collection" "graphrag" {
  name = local.collection_name
  type = "VECTORSEARCH"

  depends_on = [
    aws_opensearchserverless_security_policy.encryption,
    aws_opensearchserverless_security_policy.network,
    aws_opensearchserverless_access_policy.data,
  ]

  tags = merge(var.tags, {
    Name = local.collection_name
  })
}

# ─── IAM Role for Pod Access (IRSA) ─────────────────────────────────────────

data "aws_iam_policy_document" "opensearch_assume" {
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

resource "aws_iam_role" "opensearch_access" {
  name               = "${var.cluster_name}-opensearch-access"
  assume_role_policy = data.aws_iam_policy_document.opensearch_assume.json

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-opensearch-access"
  })
}

data "aws_iam_policy_document" "opensearch_access" {
  statement {
    effect = "Allow"
    actions = [
      "aoss:APIAccessAll",
    ]
    resources = [
      aws_opensearchserverless_collection.graphrag.arn,
    ]
  }

  # Allow Bedrock embedding calls (for vector embedding during ingestion)
  statement {
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
    ]
    resources = [
      "arn:${local.partition}:bedrock:*::foundation-model/amazon.titan-embed-text-v2*",
    ]
  }
}

resource "aws_iam_policy" "opensearch_access" {
  name   = "${var.cluster_name}-opensearch-access"
  policy = data.aws_iam_policy_document.opensearch_access.json

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-opensearch-access"
  })
}

resource "aws_iam_role_policy_attachment" "opensearch_access" {
  role       = aws_iam_role.opensearch_access.name
  policy_arn = aws_iam_policy.opensearch_access.arn
}
