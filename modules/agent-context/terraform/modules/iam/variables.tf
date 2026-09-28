variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "oidc_provider_arn" {
  description = "EKS OIDC provider ARN"
  type        = string
}

variable "oidc_issuer" {
  description = "EKS OIDC issuer URL (without https:// prefix)"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace for IRSA binding"
  type        = string
  default     = "agent-context"
}

variable "service_account" {
  description = "Kubernetes service account name for IRSA binding"
  type        = string
  default     = "agent-context-sa"
}

variable "bucket_name" {
  description = "S3 bucket name for platform data"
  type        = string
}

variable "served_s3_prefixes" {
  description = <<-EOT
    Object-key prefixes the ingestion/Door role may read and write (#5658).

    The policy previously granted the whole bucket (`<bucket>/*`). That is wider
    than anything the code asks for: every artifact path is produced by
    scope.compute_s3_prefix, which emits `content/<artifact>`, `sbom`,
    `zoekt-shards`, `tenants/<tenant_id>/<artifact>` or
    `users/<owner_sub>/<artifact>`. Bucket-wide access means a path-construction
    bug — or a traversal in a key built from user input — reads objects IAM should
    have refused, so the bound exists to stop a code-level mistake becoming a
    cross-tenant read.

    Note what this does and does not buy. `tenants/*` still covers every tenant,
    so this does not isolate tenants from each other at the IAM layer; the role
    is shared by all ingestion work, so it cannot. Per-tenant isolation is
    enforced by the ACL checks in door/acl.py and by prefix routing. This narrows
    the blast radius to the served namespace and keeps the rest of the bucket
    (state, logs, anything added later) out of reach.

    The default list was derived from the prefixes the code actually uses:
    images/ingestion/config.py (s3_content_prefix, wiki_s3_prefix,
    code_index_s3_prefix, zoekt_shards_s3_prefix, sbom_s3_prefix),
    door/config.py, door/secure_backend.py::SBOM_S3_PREFIX and the two scope
    routing branches. Adding a new artifact prefix in code without adding it here
    produces AccessDenied at runtime, so keep the two in sync.
  EOT
  type        = list(string)
  default = [
    "content/*",
    "sbom/*",
    "zoekt-shards/*",
    "tenants/*",
    "users/*",
    # The same role serves owner-authorized experience/remember operations.
    # personal_context/backends/s3_backend.py defaults to this namespace.
    "personal-context/*",
    # Legacy top-level prefix, not produced by compute_s3_prefix but still READ by
    # door/structural_backend.py:111 and door/browse_backend.py:511 as a no-DB
    # fallback. Omitting it would turn those fallbacks into AccessDenied.
    "code-indexes/*",
  ]
}

variable "rds_username" {
  description = "RDS username for IAM auth (rds-db:connect)"
  type        = string
  default     = "agent_context_svc"
}

variable "graphrag_enabled" {
  description = "Enable OpenSearch Serverless IAM policy (full GraphRAG). Neptune IAM is gated separately via neptune_enabled."
  type        = bool
  default     = false
}

variable "neptune_enabled" {
  description = "Enable the Neptune IAM policy independently of OpenSearch/GraphRAG."
  type        = bool
  default     = false
}

variable "keda_operator_role_arn" {
  description = "ARN of the KEDA operator role allowed to chain-assume this IRSA role for SQS scaling. Empty disables the trust statement. Issue #2213."
  type        = string
  default     = ""
}
