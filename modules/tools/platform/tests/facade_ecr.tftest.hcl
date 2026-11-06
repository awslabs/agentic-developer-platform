mock_provider "aws" {}

run "facade_repository_is_safe_for_digest_rollbacks" {
  command = plan

  assert {
    condition     = aws_ecr_repository.tools_facade.name == "adp-tools-facade"
    error_message = "The deployment pipeline must use the façade's own repository."
  }

  assert {
    condition     = aws_ecr_repository.tools_facade.image_tag_mutability == "IMMUTABLE" && !aws_ecr_repository.tools_facade.force_delete
    error_message = "Image tags must be immutable and Terraform cannot force-delete releases."
  }

  assert {
    condition     = aws_ecr_repository.tools_facade.image_scanning_configuration[0].scan_on_push && aws_ecr_repository.tools_facade.encryption_configuration[0].encryption_type == "AES256"
    error_message = "Scan every pushed image and encrypt the repository."
  }

  assert {
    condition     = jsondecode(aws_ecr_lifecycle_policy.tools_facade.policy).rules[0].selection.tagStatus == "untagged"
    error_message = "Tagged release digests must remain available for rollback."
  }
}
