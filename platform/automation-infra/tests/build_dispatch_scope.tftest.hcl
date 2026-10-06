mock_provider "external" {}
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}
override_data {
  target = data.aws_ssm_parameter.frontend_bucket
  values = { value = "adp-test-frontend" }
}
override_data {
  target = data.aws_ssm_parameter.frontend_cloudfront_id
  values = { value = "E1234567890" }
}
variables {
  name_prefix         = "adp-test"
  environment         = "test"
  aws_region          = "us-east-1"
  cluster_name        = "adp-test-eks-cluster"
  repository          = "aws-e/adp"
  build_project_names = ["adp-test-superplane-executor"]
}

run "executor_has_exact_image_reads_and_no_worker_pointer_write" {
  command = plan
  variables {
    build_ecr_repository_names     = ["adp-superplane-executor"]
    build_publish_worker_image_tag = false
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "ReadPublishedImages"
    ]).Resource) == toset(["arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-executor"])
    error_message = "Executor dispatch may read only its explicitly selected repository."
  }
  assert {
    condition     = !can(regex("ssm:|PublishWorkerBuildTag", aws_iam_role_policy.build_dispatch.policy))
    error_message = "Executor-only dispatch must have no SSM publication capability."
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "DispatchKnownProjects"
      ]).Resource) == toset([
      "arn:aws:codebuild:us-east-1:123456789012:project/adp-test-superplane-executor",
      "arn:aws:codebuild:us-east-1:123456789012:build/adp-test-superplane-executor:*",
    ])
    error_message = "Repository scoping must not widen the reviewed build-project inventory."
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "StageReviewedSource"
    ]).Resource) == toset(["arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-test-superplane-executor/*"])
    error_message = "Only the selected project's source prefix may be staged."
  }
}

run "omitted_inputs_preserve_existing_dispatcher_capabilities" {
  command = plan
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "ReadPublishedImages"
    ]).Resource) == toset(["arn:aws:ecr:us-east-1:123456789012:repository/adp-*"])
    error_message = "Existing multi-image dispatchers retain their previous read scope until explicitly configured."
  }
  assert {
    condition = one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "PublishWorkerBuildTag"
    ]).Resource == "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/cyber/worker-image-tag"
    error_message = "Existing worker build consumers retain their exact publication pointer."
  }
}

run "multiple_explicit_repositories_remain_exact" {
  command = plan
  variables {
    build_ecr_repository_names = ["adp-superplane-executor", "adp-superplane-api"]
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "ReadPublishedImages"
      ]).Resource) == toset([
      "arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-executor",
      "arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-api",
    ])
    error_message = "Explicit repository names must never collapse to a prefix grant."
  }
}

run "explicit_wildcard_is_refused" {
  command = plan
  variables { build_ecr_repository_names = ["adp-*"] }
  expect_failures = [var.build_ecr_repository_names]
}
run "explicit_empty_inventory_is_refused" {
  command = plan
  variables { build_ecr_repository_names = [] }
  expect_failures = [var.build_ecr_repository_names]
}
run "repository_arn_instead_of_name_is_refused" {
  command = plan
  variables { build_ecr_repository_names = ["arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-executor"] }
  expect_failures = [var.build_ecr_repository_names]
}

run "layer_checks_read_only_the_selected_artifact" {
  command = plan
  variables {
    build_project_names = ["adp-test-pyjwt-layer"]
  }
  assert {
    condition = one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "VerifyPublishedLayerArtifacts"
    ]).Resource == ["arn:aws:s3:::adp-terraform-state-123456789012/lambda-layers/pyjwt-py313.zip"]
    error_message = "Layer verification may read only the selected exact output object."
  }
  assert {
    condition     = !can(regex("s3:ListBucket", aws_iam_role_policy.build_dispatch.policy))
    error_message = "Artifact existence checks must not gain bucket listing authority."
  }
}

run "superplane_disabled_adds_no_authority" {
  command = plan
  assert {
    condition     = !can(regex("Superplane|superplane/releases|superplane-paid-worker", aws_iam_role_policy.build_dispatch.policy))
    error_message = "Existing dispatchers must receive no new Superplane capability by default."
  }
}

run "source_export_has_only_exact_transport_and_bucket_checks" {
  command = plan
  variables {
    enable_superplane_operator_source = true
    build_ecr_repository_names        = ["adp-superplane-executor"]
    build_publish_worker_image_tag    = false
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "SuperplaneReviewedSource"
      ]).Resource) == toset([
      "arn:aws:s3:::adp-terraform-state-123456789012/superplane/releases/operator-source/test/*/bundles/*",
      "arn:aws:s3:::adp-terraform-state-123456789012/superplane/releases/operator-source/test/*/consumers/*",
      "arn:aws:s3:::adp-terraform-state-123456789012/superplane/releases/operator-source/test/*/manifests/*",
    ])
    error_message = "Source publication must be limited to its environment's three transport paths."
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "SuperplaneReviewedSource"
    ]).Action) == toset(["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"])
    error_message = "Source delivery requires conditional upload and versioned readback only."
  }
  assert {
    condition = one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "SuperplaneSourceBucketChecks"
      ]).Resource == "arn:aws:s3:::adp-terraform-state-123456789012" && toset(one([
        for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "SuperplaneSourceBucketChecks"
    ]).Action) == toset(["s3:GetBucketVersioning", "s3:GetBucketPublicAccessBlock", "s3:GetBucketOwnershipControls", "s3:GetBucketLocation"])
    error_message = "Bucket inspection must name one account-owned bucket and only transport safety reads."
  }
  assert {
    condition     = !can(regex("s3:Delete|s3:List|s3:PutBucket|s3:PutObjectAcl|iam:|kms:|lambda:|eks:|secretsmanager:|superplane-paid-worker|paid-worker/dispatch", aws_iam_role_policy.build_dispatch.policy))
    error_message = "Source delivery must not gain paid dispatch, deletion, bucket mutation or deployment authority."
  }
}

run "paid_release_has_exact_builder_and_durable_claim_scope" {
  command = plan
  variables {
    enable_superplane_paid_release = true
    build_ecr_repository_names     = ["adp-superplane-executor"]
    build_publish_worker_image_tag = false
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "DispatchKnownProjects"
      ]).Resource) == toset([
      "arn:aws:codebuild:us-east-1:123456789012:project/adp-test-superplane-executor",
      "arn:aws:codebuild:us-east-1:123456789012:build/adp-test-superplane-executor:*",
      "arn:aws:codebuild:us-east-1:123456789012:project/adp-test-superplane-paid-worker",
      "arn:aws:codebuild:us-east-1:123456789012:build/adp-test-superplane-paid-worker:*",
    ])
    error_message = "Paid enrollment may add exactly the prepared environment project."
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "StageReviewedSource"
      ]).Resource) == toset([
      "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-test-superplane-executor/*",
      "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-test-superplane-paid-worker/*",
      ]) && toset(one([
        for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "ReadPublishedImages"
      ]).Resource) == toset([
      "arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-executor",
      "arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-paid-worker",
    ])
    error_message = "Paid source staging and image reads must retain exact existing inventories."
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "SuperplanePaidDispatchEvidence"
      ]).Resource) == toset([
      "arn:aws:s3:::adp-terraform-state-123456789012/superplane/releases/paid-worker/dispatch/*/claim.json",
      "arn:aws:s3:::adp-terraform-state-123456789012/superplane/releases/paid-worker/dispatch/*/child.json",
      ]) && one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "SuperplanePaidDispatchEvidence"
    ]).Action == ["s3:PutObject"]
    error_message = "Durable claims need only conditional writes to the two evidence files."
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "SuperplanePaidRetentionChecks"
      ]).Action) == toset(["s3:GetBucketVersioning", "s3:GetLifecycleConfiguration"]) && one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "SuperplanePaidRetentionChecks"
    ]).Resource == "arn:aws:s3:::adp-terraform-state-123456789012"
    error_message = "Paid evidence retention requires only the one bucket's versioning and lifecycle reads."
  }
  assert {
    condition     = !can(regex("s3:Delete|s3:List|s3:PutBucket|s3:GetObject|iam:|kms:|lambda:|eks:|secretsmanager:|operator-source|codebuild:UpdateProject|ecr:PutImage", aws_iam_role_policy.build_dispatch.policy))
    error_message = "Paid dispatch cannot grant source export, claim reset, role/project mutation or deployment."
  }
}

run "source_wrong_repository_is_refused" {
  command = plan
  variables {
    enable_superplane_operator_source = true
    repository                        = "another/repo"
  }
  expect_failures = [aws_iam_role_policy.build_dispatch]
}

run "source_noncanonical_environment_is_refused" {
  command = plan
  variables {
    enable_superplane_operator_source = true
    environment                       = "test--bad"
    name_prefix                       = "adp-test--bad"
  }
  expect_failures = [aws_iam_role_policy.build_dispatch]
}
