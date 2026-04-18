# Terraform state stored in S3 for team access.
# Uncomment and configure for your environment.
# For first-time setup, you may run with local state and migrate later.

# terraform {
#   backend "s3" {
#     bucket         = "agent-context-terraform-state"
#     key            = "s3-files/terraform.tfstate"
#     region         = "us-east-1"
#     encrypt        = true
#     dynamodb_table = "terraform-locks"
#   }
# }
