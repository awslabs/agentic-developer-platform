output "ingest_lambda_arn" { value = aws_lambda_function.ingest.arn }
output "ingest_lambda_name" { value = aws_lambda_function.ingest.function_name }
output "response_lambda_arn" { value = aws_lambda_function.response.arn }
output "response_lambda_name" { value = aws_lambda_function.response.function_name }
