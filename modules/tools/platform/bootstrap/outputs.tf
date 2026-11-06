output "gateway_role_arn" {
  value = aws_iam_role.gateway.arn
}

output "gateway_kms_key_arn" {
  value = aws_kms_key.gateway.arn
}

output "assertion_signing_key_arn" {
  value = length(aws_kms_key.assertion) > 0 ? aws_kms_key.assertion[0].arn : ""
}

output "reconciler_signing_key_arn" {
  value = length(aws_kms_key.reconciler) > 0 ? aws_kms_key.reconciler[0].arn : ""
}

output "interceptor_function_name" {
  value = var.enable_interceptor ? aws_lambda_function.interceptor[0].function_name : ""
}

output "interceptor_function_arn" {
  value = var.enable_interceptor ? aws_lambda_function.interceptor[0].arn : ""
}

output "interceptor_execution_role_arn" {
  value = var.enable_interceptor ? aws_iam_role.interceptor[0].arn : ""
}
