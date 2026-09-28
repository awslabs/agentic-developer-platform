mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/mock-build" }
  }
}

variables {
  domain_app = "cyber"
  projects = {
    cyber-worker = {
      buildspec       = "modules/domain-apps/cyber/codebuild/bs-cyber-worker.yml"
      ecr_repos       = ["adp-cyber-worker"]
      privileged      = true
      artifact_writes = [{ bucket_suffix = "cape-assets", prefix = "worker-manifests" }]
    }
  }
  name_prefix                = "adp-dev"
  account_id                 = "111122223333"
  aws_region                 = "us-east-1"
  state_bucket               = "adp-terraform-state-111122223333"
  security_scans_bucket_name = "adp-dev-security-scans"
  permissions_boundary_arn   = "arn:aws:iam::111122223333:policy/adp-dev-codebuild-boundary"
  common_tags                = { DomainApp = "cyber" }
  allowed_artifact_writes    = ["cape-assets/worker-manifests"]
}

run "cyber_jobs_are_domain_scoped" {
  command = plan
  assert {
    condition     = toset(keys(aws_codebuild_project.main)) == toset(["cyber-worker"])
    error_message = "The domain build module must create only the supplied app jobs."
  }
  assert {
    condition = alltrue([for name, role in aws_iam_role.project :
      role.name == "adp-dev-codebuild-${name}" &&
      role.permissions_boundary == var.permissions_boundary_arn
    ])
    error_message = "Every Cyber build needs its own bounded role."
  }
  assert {
    condition = toset([for statement in jsondecode(aws_iam_role_policy.project["cyber-worker"].policy).Statement : statement.Resource if statement.Sid == "OwnEcrRepositories"][0]) == toset([
      "arn:aws:ecr:us-east-1:111122223333:repository/adp-cyber-worker"
    ])
    error_message = "Cyber worker image publication must be scoped to its repository."
  }
  assert {
    condition = toset([for statement in jsondecode(aws_iam_role_policy.project["cyber-worker"].policy).Statement : statement.Resource if startswith(statement.Sid, "AppArtifactPublish")]) == toset([
      "arn:aws:s3:::adp-dev-cape-assets/worker-manifests/*"
    ])
    error_message = "Cyber's artifact write must stay in its own manifest prefix."
  }
}

run "foreign_manifest_is_rejected" {
  command = plan
  variables {
    domain_app = "cyber"
    projects = {
      superplane-api = {
        buildspec  = "modules/domain-apps/superplane/releases/buildspecs/api.yml"
        ecr_repos  = ["adp-superplane-api"]
        privileged = true
      }
    }
  }
  expect_failures = [terraform_data.manifest_guard]
}

run "unreviewed_artifact_destination_is_rejected" {
  command = plan
  variables {
    allowed_artifact_writes = []
  }
  expect_failures = [terraform_data.manifest_guard]
}
