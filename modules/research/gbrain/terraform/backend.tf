terraform {
  backend "s3" {
    # Configured via CLI -backend-config flags:
    #   bucket         = "adp-terraform-state-<ACCOUNT_ID>"
    #   key            = "research/gbrain/terraform.tfstate"
    #   region         = "us-east-1"
    #   encrypt        = true
    #   dynamodb_table = "adp-terraform-locks"
  }
}
