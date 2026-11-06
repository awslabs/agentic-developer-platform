bucket         = "adp-terraform-state-ACCOUNT_ID"
key            = "dev/modules/dev-box-platform/terraform.tfstate"
region         = "us-east-1"
encrypt        = true
dynamodb_table = "adp-terraform-locks"
