module "runtime" {
  source                     = "../modules/worker-runtime"
  service_id                 = "transform"
  environment                = var.environment
  vpc_id                     = var.vpc_id
  private_subnet_ids         = var.private_subnet_ids
  db_security_group_id       = var.db_security_group_id
  endpoint_security_group_id = var.endpoint_security_group_id
  db_port                    = var.db_port
  permissions_boundary_arn   = var.permissions_boundary_arn
  worker_artifact_bucket     = var.worker_artifact_bucket
  worker_artifact_key        = var.worker_artifact_key
  worker_artifact_version    = var.worker_artifact_version
  worker_artifact_sha256     = var.worker_artifact_sha256
  worker_factory             = var.worker_factory
  worker_environment         = var.worker_environment
  gateway_api_arn            = var.gateway_api_arn
  rds_connect_arn            = var.rds_connect_arn
}

module "registration" {
  source                    = "../modules/service-registration"
  environment               = var.environment
  service_id                = "transform"
  agent_registry_table_name = var.agent_registry_table_name
  worker_role_arn           = module.runtime.worker_role_arn
  worker_role_id            = module.runtime.worker_role_id
  repair_role_arn           = module.runtime.repair_role_arn
  repair_role_id            = module.runtime.repair_role_id
  work_queue_url            = module.runtime.work_queue_url
}
