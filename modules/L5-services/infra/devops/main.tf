module "runtime" {
  source                     = "../modules/worker-runtime"
  service_id                 = "devops"
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
  service_id                = "devops"
  agent_registry_table_name = var.agent_registry_table_name
  worker_role_arn           = module.runtime.worker_role_arn
  worker_role_id            = module.runtime.worker_role_id
  repair_role_arn           = module.runtime.repair_role_arn
  repair_role_id            = module.runtime.repair_role_id
  work_queue_url            = module.runtime.work_queue_url
}

module "events" {
  agent_registry_table_name = var.agent_registry_table_name
  source                    = "../modules/service-events"
  name_prefix               = "adp-${var.environment}-l5-devops"
  functions = {
    readback = { handler = "l5_services.workers.devops_readback.lambda_handler", schedule = "rate(5 minutes)" }
    native   = { handler = "l5_services.workers.devops_events.lambda_handler", event_pattern = var.native_event_pattern }
  }
  permissions_boundary_arn = var.permissions_boundary_arn
  private_subnet_ids       = var.private_subnet_ids
  security_group_id        = module.runtime.worker_security_group_id
  worker_environment = merge(var.worker_environment, {
    L5_SERVICE_ID     = "devops"
    L5_WORKER_FACTORY = var.worker_factory
    L5_WORK_QUEUE_URL = module.runtime.work_queue_url
  })
  worker_artifact_bucket  = var.worker_artifact_bucket
  worker_artifact_key     = var.worker_artifact_key
  worker_artifact_version = var.worker_artifact_version
  worker_artifact_sha256  = var.worker_artifact_sha256
  gateway_api_arn         = var.gateway_api_arn
  rds_connect_arn         = var.rds_connect_arn
  work_queue_arn          = module.runtime.work_queue_arn
}
