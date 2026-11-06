# Crew bootstrap modes (#8055): image mode leaves the launch template exactly as
# before (no user data, no extra grant, no extra egress); user-data mode renders a
# pinned, secret-free first-boot script and the minimum the guest needs to fetch it.

mock_provider "aws" {
  mock_resource "aws_iam_instance_profile" {
    defaults = { arn = "arn:aws:iam::000000000000:instance-profile/example-guest" }
  }
  mock_data "aws_prefix_list" {
    defaults = { id = "pl-0123456789abcdef0" }
  }
}

override_resource {
  target = aws_iam_role.workspace_snapshot_dlm
  values = { arn = "arn:aws:iam::000000000000:role/example-snapshot-dlm" }
}

variables {
  account_id                     = "000000000000"
  region                         = "us-east-1"
  environment                    = "example"
  deployment_id                  = "example-deployment"
  vpc_id                         = "vpc-0123456789abcdef0"
  private_subnet_id              = "subnet-0123456789abcdef0"
  gateway_security_group_id      = "sg-11111111111111111"
  ssm_endpoint_security_group_id = "sg-22222222222222222"
  controller_principal_arn       = "arn:aws:iam::000000000000:role/example-controller"
  image_id                       = "ami-0123456789abcdef0"
  source_revision                = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  template_revision              = "example-r1"
  instance_size                  = "t3.small"
  workspace_disk_gb              = 40
  kms_key_arn                    = "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000"
  run_table_arn                  = "arn:aws:dynamodb:us-east-1:000000000000:table/example-runs"
  gateway_execute_arn            = "arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev"
  crew_model_id                  = "example-model"
}

run "image_mode_is_unchanged" {
  command = apply
  variables {
    crew_profile_revision = "crew-r1"
  }

  assert {
    condition     = aws_launch_template.guest.user_data == null
    error_message = "image mode must not add user data to the launch template."
  }
  assert {
    condition     = length(aws_iam_role_policy.guest_crew_bootstrap) == 0 && length(aws_vpc_security_group_egress_rule.crew_bootstrap_s3) == 0 && length(data.aws_prefix_list.s3) == 0
    error_message = "image mode must not grant S3 or open egress."
  }
  assert {
    condition     = aws_ssm_parameter.stage["crew-bootstrap"].value == "image" && !contains(keys(aws_ssm_parameter.stage), "crew-bundle-sha256") && !contains(keys(aws_ssm_parameter.stage), "profile-revision")
    error_message = "SSM publishes the mode only; the bundle digest and profile revision are script-owned keys of the builds."
  }
  assert {
    condition     = output.crew_bootstrap == "image" && output.crew_bundle_sha256 == "" && output.crew_source_revision == "" && output.crew_bootstrap_s3_prefix_list_id == "" && output.release_context.crew_bootstrap == "image" && output.release_context.crew_bundle_s3_uri == ""
    error_message = "The release context describes image mode with no bundle inputs."
  }
}

run "image_mode_ignores_bundle_inputs" {
  command = apply
  variables {
    crew_profile_revision = "crew-r1"
    crew_bundle_s3_uri    = "s3://adp-terraform-state-000000000000/dev-box/image-inputs/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/crew.tar.gz"
    crew_bundle_sha256    = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    crew_source_revision  = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }

  assert {
    condition     = aws_launch_template.guest.user_data == null && length(aws_iam_role_policy.guest_crew_bootstrap) == 0 && output.crew_bundle_sha256 == "" && output.release_context.crew_bootstrap == "image"
    error_message = "Bundle inputs without crew_bootstrap = user-data change nothing."
  }
}

run "user_data_mode_renders_pinned_secret_free_bootstrap" {
  command = apply
  variables {
    crew_bootstrap        = "user-data"
    crew_profile_revision = "crew-r1"
    crew_bundle_s3_uri    = "s3://adp-terraform-state-000000000000/dev-box/image-inputs/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/crew.tar.gz"
    crew_bundle_sha256    = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    crew_source_revision  = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }

  assert {
    condition     = aws_launch_template.guest.user_data != null && startswith(base64decode(aws_launch_template.guest.user_data), "#!/bin/bash\n")
    error_message = "user-data mode renders a shell bootstrap into the launch template."
  }
  assert {
    condition = alltrue([for needle in [
      "BUNDLE_URI='s3://adp-terraform-state-000000000000/dev-box/image-inputs/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/crew.tar.gz'",
      "MANIFEST_URI='s3://adp-terraform-state-000000000000/dev-box/image-inputs/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/profile.json'",
      "HELPER_PREFIX='s3://adp-terraform-state-000000000000/dev-box/image-inputs/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/adp'",
      "BUNDLE_SHA256='cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'",
      "PROFILE_REVISION='crew-r1'",
      "SOURCE_REVISION='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'",
      "sha256sum -c --quiet -",
      "install_crew.py\" --manifest",
      "--expected-source-revision \"$SOURCE_REVISION\" --expected-profile-revision \"$PROFILE_REVISION\"",
      "install -m 0700 -o root -g root \"$WORK/adp/write_enrollment.py\" /opt/adp/crew/write_enrollment.py",
      "install -m 0700 -o root -g root \"$WORK/adp/write_host_certificate.py\" /opt/adp/crew/write_host_certificate.py",
      "install -m 0700 -o root -g root \"$WORK/adp/verify_crew_ready.py\" /opt/adp/crew/verify_crew_ready.py",
      "install -m 0700 -o root -g root \"$WORK/adp/restore_crew_host.py\" /opt/adp/crew/restore_crew_host.py",
      "install -m 0700 -o root -g root \"$WORK/adp/checkpoint_crew_host.py\" /opt/adp/crew/checkpoint_crew_host.py",
      "checkpoint_crew_host.py=${filesha256("${path.module}/../image/checkpoint_crew_host.py")}",
      "install -m 0600 -o root -g root \"$WORK/adp/crew_checkpoint.py\" /opt/adp/crew/crew_checkpoint.py",
      "/run/adp/crew-bootstrap.json",
      "install_crew.py=${filesha256("${path.module}/../image/install_crew.py")}",
      "crew_checkpoint.py=${filesha256("${path.module}/../../../agent-factory/agent/scripts/crew_checkpoint.py")}",
    ] : strcontains(base64decode(aws_launch_template.guest.user_data), needle)])
    error_message = "The bootstrap must pin the bundle, the manifest, every helper and both expected revisions, verify digests and install the same root-only helpers as crew.pkr.hcl."
  }
  assert {
    condition = alltrue([for forbidden in [
      "AKIA", "aws_secret_access_key", "session_token", "PRIVATE KEY", "curl ", "wget ", "latest",
      var.kms_key_arn, var.controller_principal_arn, var.run_table_arn,
    ] : !strcontains(base64decode(aws_launch_template.guest.user_data), forbidden)])
    error_message = "User data may carry only object paths, digests and revisions."
  }
  assert {
    condition     = strcontains(base64decode(aws_launch_template.guest.user_data), "name=$${pin%%=*}") && !strcontains(base64decode(aws_launch_template.guest.user_data), "$$$${")
    error_message = "Escaped shell parameter expansions render as bash, and no template escape leaks into the script."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.guest_crew_bootstrap[0].policy).Statement == [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["arn:aws:s3:::adp-terraform-state-000000000000/dev-box/image-inputs/*"]
      }
    ]
    error_message = "The guest may only read the image-inputs prefix of the selected bucket."
  }
  assert {
    condition     = length(aws_iam_role_policy.guest_gateway) == 1 && aws_iam_role_policy.guest_crew_bootstrap[0].name == "crew-bootstrap-inputs-read"
    error_message = "The bootstrap grant is a separate, named inline policy next to the model route."
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.crew_bootstrap_s3[0].prefix_list_id == "pl-0123456789abcdef0" && aws_vpc_security_group_egress_rule.crew_bootstrap_s3[0].from_port == 443 && aws_vpc_security_group_egress_rule.crew_bootstrap_s3[0].to_port == 443 && aws_vpc_security_group_egress_rule.crew_bootstrap_s3[0].ip_protocol == "tcp"
    error_message = "The only new egress is HTTPS to the S3 gateway endpoint prefix list."
  }
  assert {
    condition     = aws_ssm_parameter.stage["crew-bootstrap"].value == "user-data" && !contains(keys(aws_ssm_parameter.stage), "crew-bundle-sha256")
    error_message = "SSM publishes the mode for the stage and the gateway renderers; the bundle build owns crew-bundle-sha256."
  }
  assert {
    condition     = output.release_context.crew_bootstrap == "user-data" && output.release_context.crew_bundle_sha256 == "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc" && output.release_context.crew_source_revision == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" && output.release_context.crew_bundle_s3_uri == var.crew_bundle_s3_uri && output.crew_bootstrap_s3_prefix_list_id == "pl-0123456789abcdef0"
    error_message = "The release context carries the bootstrap pins so the manifest and runtime.py describe what is live."
  }
  assert {
    condition     = length(aws_launch_template.guest.block_device_mappings) == 1 && aws_launch_template.guest.metadata_options[0].http_tokens == "required" && aws_launch_template.guest.network_interfaces[0].associate_public_ip_address == "false"
    error_message = "The rest of the launch template is unchanged by the bootstrap mode."
  }
}

run "reject_user_data_without_bundle_inputs" {
  command = plan
  variables {
    crew_bootstrap        = "user-data"
    crew_profile_revision = "crew-r1"
  }
  expect_failures = [aws_launch_template.guest]
}

run "reject_user_data_without_profile_pin" {
  command = plan
  variables {
    crew_bootstrap       = "user-data"
    crew_bundle_s3_uri   = "s3://adp-terraform-state-000000000000/dev-box/image-inputs/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/crew.tar.gz"
    crew_bundle_sha256   = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    crew_source_revision = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
  expect_failures = [aws_launch_template.guest]
}

run "reject_bundle_not_addressed_by_its_digest" {
  command = plan
  variables {
    crew_bootstrap        = "user-data"
    crew_profile_revision = "crew-r1"
    crew_bundle_s3_uri    = "s3://adp-terraform-state-000000000000/dev-box/image-inputs/dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd/crew.tar.gz"
    crew_bundle_sha256    = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    crew_source_revision  = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
  expect_failures = [aws_launch_template.guest]
}

run "reject_unpinned_bundle_location" {
  command = plan
  variables {
    crew_bootstrap        = "user-data"
    crew_profile_revision = "crew-r1"
    crew_bundle_s3_uri    = "s3://adp-terraform-state-000000000000/dev-box/image-inputs/latest/crew.tar.gz"
    crew_bundle_sha256    = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    crew_source_revision  = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
  expect_failures = [var.crew_bundle_s3_uri]
}

run "reject_unknown_mode" {
  command = plan
  variables {
    crew_bootstrap = "ssm"
  }
  expect_failures = [var.crew_bootstrap]
}
