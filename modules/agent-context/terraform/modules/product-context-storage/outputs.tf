output "session_role_arn" { value = aws_iam_role.product_session.arn }
output "shared_key_arn" { value = aws_kms_key.shared.arn }
output "managed_key_arns" { value = { for label, key in aws_kms_key.product : label => key.arn } }
