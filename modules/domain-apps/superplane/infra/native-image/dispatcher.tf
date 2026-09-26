# This policy adds only the dedicated lane to the already trusted dispatcher.
# It does not create a competing dispatcher role or execution engine. Existing
# principal trust/main/environment protection remains an installation prerequisite.
resource "aws_iam_policy" "dispatcher" {
  name = "${var.lane.name}-dispatch"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "OwnProject", Effect = "Allow", Action = ["codebuild:StartBuild", "codebuild:BatchGetProjects", "codebuild:ListBuildsForProject"], Resource = local.build_project_arn },
      { Sid = "OwnBuilds", Effect = "Allow", Action = ["codebuild:BatchGetBuilds", "codebuild:StopBuild"], Resource = "arn:aws:codebuild:${var.lane.region}:${var.lane.account_id}:build/${var.lane.name}:*" },
      { Sid = "NativeSourceWrite", Effect = "Allow", Action = ["s3:PutObject", "s3:AbortMultipartUpload"], Resource = ["${aws_s3_bucket.native["input"].arn}/native-input/*", "${aws_s3_bucket.native["input"].arn}/codebuild/src/${var.lane.name}/*"] },
      { Sid = "DispatchReceiptWrite", Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.native["output"].arn}/dispatch/*" },
      { Sid = "ReviewEvidenceRead", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.native["output"].arn}/*" },
      { Sid = "InputEvidenceKey", Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = var.lane.kms_key_arn, Condition = { StringEquals = { "kms:ViaService" = "s3.${var.lane.region}.amazonaws.com", "kms:CallerAccount" = var.lane.account_id } } }
    ]
  })
  tags = local.tags
}
# Review this output with the dispatcher's existing boundary before installation.
# Deliberately no attachment/mutation to the shared dispatcher or executor role.
output "dispatcher_policy_arn" { value = aws_iam_policy.dispatcher.arn }
output "project_name" { value = aws_codebuild_project.native.name }
output "build_role_arn" { value = aws_iam_role.build.arn }
output "helper_profile_name" { value = aws_iam_instance_profile.helper.name }
output "input_bucket" { value = aws_s3_bucket.native["input"].id }
output "output_bucket" { value = aws_s3_bucket.native["output"].id }
output "required_project_constraints" { value = local.constraints }
# Export, review and hash this deployment contract separately from the plan.
# The CLI compares the observed project to this immutable approval before claiming
# a dispatch ID or invoking the shared runner; a name/buildspec match is insufficient.
output "dispatch_contract" {
  value = {
    version             = 1
    account_id          = var.lane.account_id
    region              = var.lane.region
    dispatcher_role_arn = var.lane.dispatcher_role_arn
    project_name        = aws_codebuild_project.native.name
    project = {
      service_role_arn            = aws_iam_role.build.arn
      environment_image           = var.lane.environment_image
      environment_type            = "LINUX_CONTAINER"
      compute_type                = var.lane.compute_type
      privileged_mode             = true
      image_pull_credentials_type = "CODEBUILD"
      environment_variables       = { for value in aws_codebuild_project.native.environment[0].environment_variable : value.name => value.value }
      vpc_id                      = var.lane.vpc_id
      subnet_ids                  = sort(tolist(var.lane.build_subnet_ids))
      security_group_ids          = [var.lane.build_security_group_id]
      timeout_minutes             = var.lane.timeout_minutes
      queued_timeout_minutes      = 30
      concurrent_build_limit      = 1
      source_type                 = "S3"
      source_location             = "${aws_s3_bucket.native["input"].id}/codebuild/src/${var.lane.name}/dispatch-required.zip"
      buildspec                   = "modules/domain-apps/superplane/releases/buildspecs/native-node-lane.yml"
      artifact_type               = "NO_ARTIFACTS"
    }
  }
}
