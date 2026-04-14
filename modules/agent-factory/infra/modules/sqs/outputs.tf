output "input_queue_url" { value = aws_sqs_queue.input.url }
output "input_queue_arn" { value = aws_sqs_queue.input.arn }
output "response_queue_url" { value = aws_sqs_queue.response.url }
output "response_queue_arn" { value = aws_sqs_queue.response.arn }
output "dlq_url" { value = aws_sqs_queue.dlq.url }
output "dlq_arn" { value = aws_sqs_queue.dlq.arn }
