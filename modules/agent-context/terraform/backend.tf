terraform {
  backend "s3" {
    # Configured via -backend-config during terraform init
    # See environments/dev/modules/agent-context-backend.tfvars
  }
}
