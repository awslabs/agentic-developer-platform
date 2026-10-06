output "resource_identity" {
  description = "Exact owned resources; worker readiness and Gateway admission are not activated."
  value = {
    queue_name        = aws_sqs_queue.operations.name
    queue_arn         = aws_sqs_queue.operations.arn
    queue_url         = aws_sqs_queue.operations.url
    worker_role_arn   = aws_iam_role.worker.arn
    worker_role_id    = aws_iam_role.worker.unique_id
    observer_role_arn = aws_iam_role.observer.arn
    observer_role_id  = aws_iam_role.observer.unique_id
    worker_ready      = false
    installation_id   = var.installation_id
  }
}
