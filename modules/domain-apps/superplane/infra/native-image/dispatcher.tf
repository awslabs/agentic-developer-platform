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
