terraform {
  backend "s3" {
    # Bucket, key, region and encrypt are supplied via -backend-config at init time.
    # See environments/dev/modules/agent-context-backend.tfvars
    #
    # State locking is required for safe concurrent operations.
    # The table is provisioned by the bootstrap process (platform/scripts/bootstrap.sh).
    dynamodb_table = "adp-terraform-locks"
  }
}
