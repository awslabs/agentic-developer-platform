# Athena discovery plus selected archived page reads. Content is saved as inert
# case evidence and never executed or loaded through the live browser.
variable "common_crawl_partitions" {
  type        = list(string)
  default     = []
  description = "One to twelve existing CC-MAIN-YYYY-WW index partitions. Empty disables Athena discovery until configured."
  validation {
    condition     = length(var.common_crawl_partitions) <= 12 && alltrue([for p in var.common_crawl_partitions : can(regex("^CC-MAIN-20[0-9]{2}-[0-9]{2}$", p))])
    error_message = "Supply at most twelve explicit Common Crawl partition names."
  }
}

locals {
  cc_enabled  = length(var.common_crawl_partitions) > 0
  cc_database = replace("${var.name_prefix}_cyber_common_crawl", "-", "_")
  cc_columns = {
    url_host_tld      = "string", url_host_registered_domain = "string",
    url_host_name     = "string", url = "string", fetch_time = "timestamp",
    fetch_status      = "smallint", content_mime_type = "string",
    content_languages = "string", content_digest = "string",
    warc_filename     = "string", warc_record_offset = "int", warc_record_length = "int"
  }
}

resource "aws_s3_bucket" "common_crawl_results" {
  count  = local.cc_enabled ? 1 : 0
  bucket = "${var.name_prefix}-cyber-common-crawl-${var.account_id}"
}

resource "aws_s3_bucket_public_access_block" "common_crawl_results" {
  count                   = local.cc_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.common_crawl_results[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "common_crawl_results" {
  count  = local.cc_enabled ? 1 : 0
  bucket = aws_s3_bucket.common_crawl_results[0].id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "common_crawl_results" {
  count  = local.cc_enabled ? 1 : 0
  bucket = aws_s3_bucket.common_crawl_results[0].id
  rule {
    id     = "expire-query-results"
    status = "Enabled"
    filter { prefix = "queries/" }
    expiration { days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

resource "aws_s3_bucket_policy" "common_crawl_results" {
  count  = local.cc_enabled ? 1 : 0
  bucket = aws_s3_bucket.common_crawl_results[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "RequireTLS", Effect = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [aws_s3_bucket.common_crawl_results[0].arn, "${aws_s3_bucket.common_crawl_results[0].arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_athena_workgroup" "common_crawl" {
  count = local.cc_enabled ? 1 : 0
  name  = "${var.name_prefix}-cyber-common-crawl"
  configuration {
    enforce_workgroup_configuration    = true
    bytes_scanned_cutoff_per_query     = 1073741824
    publish_cloudwatch_metrics_enabled = true
    engine_version { selected_engine_version = "Athena engine version 3" }
    result_configuration {
      output_location       = "s3://${aws_s3_bucket.common_crawl_results[0].id}/queries/"
      expected_bucket_owner = var.account_id
      encryption_configuration { encryption_option = "SSE_S3" }
    }
  }
}

resource "aws_glue_catalog_database" "common_crawl" {
  count = local.cc_enabled ? 1 : 0
  name  = local.cc_database
}

resource "aws_glue_catalog_table" "common_crawl" {
  count         = local.cc_enabled ? 1 : 0
  name          = "ccindex"
  database_name = aws_glue_catalog_database.common_crawl[0].name
  table_type    = "EXTERNAL_TABLE"
  parameters = {
    EXTERNAL                    = "TRUE"
    classification              = "parquet"
    "projection.enabled"        = "true"
    "projection.crawl.type"     = "injected"
    "projection.subset.type"    = "enum"
    "projection.subset.values"  = "warc"
    "storage.location.template" = "s3://commoncrawl/cc-index/table/cc-main/warc/crawl=$${crawl}/subset=$${subset}/"
  }
  partition_keys {
    name = "crawl"
    type = "string"
  }
  partition_keys {
    name = "subset"
    type = "string"
  }
  storage_descriptor {
    location      = "s3://commoncrawl/cc-index/table/cc-main/warc/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"
    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters            = { "serialization.format" = "1", "parquet.column.index.access" = "false" }
    }
    dynamic "columns" {
      for_each = local.cc_columns
      content {
        name = columns.key
        type = columns.value
      }
    }
  }
}

resource "aws_iam_role_policy" "worker_common_crawl" {
  count = local.cc_enabled ? 1 : 0
  name  = "common-crawl-index-discovery"
  role  = var.worker_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["athena:GetWorkGroup", "athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:StopQueryExecution"]
        Resource = aws_athena_workgroup.common_crawl[0].arn
      },
      {
        Effect   = "Allow"
        Action   = ["athena:GetDataCatalog"]
        Resource = "arn:aws:athena:${var.aws_region}:${var.account_id}:datacatalog/AwsDataCatalog"
      },
      {
        Effect   = "Allow", Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions", "glue:BatchGetPartition"]
        Resource = ["arn:aws:glue:${var.aws_region}:${var.account_id}:catalog", aws_glue_catalog_database.common_crawl[0].arn, aws_glue_catalog_table.common_crawl[0].arn]
      },
      {
        Effect   = "Allow", Action = ["s3:GetObject"]
        Resource = ["arn:aws:s3:::commoncrawl/cc-index/table/cc-main/warc/*"]
      },
      {
        Sid      = "ReadSelectedArchivePages"
        Effect   = "Allow", Action = ["s3:GetObject"]
        Resource = [for crawl in var.common_crawl_partitions : "arn:aws:s3:::commoncrawl/crawl-data/${crawl}/segments/*/warc/*.warc.gz"]
      },
      {
        Effect    = "Allow", Action = ["s3:ListBucket"]
        Resource  = "arn:aws:s3:::commoncrawl"
        Condition = { StringLike = { "s3:prefix" = ["cc-index/table/cc-main/warc/*"] } }
      },
      {
        Effect   = "Allow", Action = ["s3:GetBucketLocation"]
        Resource = ["arn:aws:s3:::commoncrawl", aws_s3_bucket.common_crawl_results[0].arn]
      },
      {
        Effect   = "Allow", Action = ["s3:ListBucket", "s3:ListBucketMultipartUploads"]
        Resource = aws_s3_bucket.common_crawl_results[0].arn
      },
      {
        Effect   = "Allow", Action = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${aws_s3_bucket.common_crawl_results[0].arn}/queries/*"
      }
    ]
  })
}
