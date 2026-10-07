mock_provider "aws" {}

variables {
  environment  = "dev"
  name_prefix  = "adp-dev"
  repositories = ["adp-example"]
}

run "external_registry_keeps_repository_scanning" {
  command = plan
  variables {
    manage_registry_scanning = false
  }
  assert {
    condition     = length(aws_ecr_registry_scanning_configuration.main) == 0
    error_message = "An externally managed registry must not receive an ADP scanning configuration."
  }
  assert {
    condition     = aws_ecr_repository.main["adp-example"].image_scanning_configuration[0].scan_on_push
    error_message = "External registry ownership must not disable per-repository scanning."
  }
}

run "explicit_registry_owner" {
  command = plan
  variables {
    manage_registry_scanning = true
  }
  assert {
    condition     = aws_ecr_registry_scanning_configuration.main[0].scan_type == "BASIC"
    error_message = "Explicit ADP ownership must preserve the configured registry policy."
  }
}
