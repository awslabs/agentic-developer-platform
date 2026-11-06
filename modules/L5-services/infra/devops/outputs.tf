output "work_queue_url" { value = module.runtime.work_queue_url }
output "work_queue_arn" { value = module.runtime.work_queue_arn }
output "worker_function_name" { value = module.runtime.worker_function_name }
output "worker_role_arn" { value = module.runtime.worker_role_arn }
output "repair_function_name" { value = module.runtime.repair_function_name }
output "event_source_mapping_id" { value = module.runtime.event_source_mapping_id }
output "event_function_names" { value = module.events.function_names }
