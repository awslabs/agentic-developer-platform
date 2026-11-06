output "group_namespaces" {
  description = "Dedicated manager and job namespaces indexed by numeric group ID."
  value = {
    for group_id, binding in local.group_bindings : group_id => {
      manager = binding.manager_namespace
      jobs    = binding.job_namespace
    }
  }
}

output "group_runner_resources" {
  description = "Exact per-group resources for the protected IAM and lifecycle handoff; contains no credential values."
  value = {
    for group_id in keys(local.group_bindings) : group_id => {
      manager_role_arn       = aws_iam_role.manager[group_id].arn
      asg_arn                = aws_autoscaling_group.job[group_id].arn
      asg_name               = aws_autoscaling_group.job[group_id].name
      cache_bucket           = aws_s3_bucket.cache.id
      cache_prefix           = "group/${group_id}/"
      host_security_group_id = aws_security_group.job[group_id].id
    }
  }
}
