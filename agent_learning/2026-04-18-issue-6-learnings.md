# Learnings: Issue #6 — Refactor gateway/infra to layer on shared platform

## Date: 2026-04-18
## Agent: @agent-architect
## Status: Complete

## Key Technical Decisions

### 1. IAM Strategy: Option B (Incremental Policies)
- The platform EKS module creates the base IRSA role (`gateway_service_irsa`) with Bedrock, STS, logs, and DynamoDB permissions
- The gateway module attaches **incremental** policies to that same role using `aws_iam_role_policy` resources
- This avoids circular dependencies: Cognito pool ID and Redis replication group ID are only known after gateway applies
- Policy resources: `gateway_rds_iam_auth`, `gateway_elasticache_iam_auth`, `gateway_cognito_read`, `gateway_chat_logs_s3`, `gateway_comprehend_pii`, `gateway_xray_tracing`, `gateway_cross_account`

### 2. Naming: Kept `bedrockgw-${env}` Prefix
- Did NOT rename to `adp-${env}-gateway-*` to avoid churn in k8s manifests, deploy scripts, and existing AWS resources
- The `local.name_prefix = "bedrockgw-${var.environment}"` is preserved

### 3. Output Rename: `authorizer_lambda_invoke_arn`
- The old output was `lambda_authorizer_arn` which pointed to the Lambda ARN (not invoke ARN)
- Renamed to `authorizer_lambda_invoke_arn` to match what `agent-factory/infra/gateway-main.tf` expects
- Value now uses `invoke_arn` from the lambda-authorizer submodule (correct for API Gateway REQUEST authorizers)
- Kept `lambda_authorizer_arn` as a separate output for the plain ARN (backward compat)

## What Changed

### Files Modified
1. `platform/infra/outputs.tf` — Added 7 new outputs: `eks_cluster_security_group_id`, `eks_security_group_id`, `rds_security_group_id`, `redis_security_group_id`, `alb_security_group_id`, `gateway_service_irsa_role_arn`, `gateway_service_irsa_role_name`
2. `modules/gateway/infra/main.tf` — Full rewrite: removed 5 platform modules, added `data.terraform_remote_state.platform`, added locals, rewired all modules to `local.*`, added incremental IAM policies
3. `modules/gateway/infra/variables.tf` — Added `account_id`, `enable_rds_iam_auth`, `enable_elasticache_iam_auth`; removed VPC/EKS/ECR/IAM vars
4. `modules/gateway/infra/outputs.tf` — Removed platform-owned outputs (vpc_id, cluster_*, ecr_*, iam role outputs); added `authorizer_lambda_invoke_arn`
5. `environments/dev/modules/gateway.tfvars` — Added `cost_center`; removed `cognito_domain_prefix`

### Files NOT Modified (no changes needed)
- `modules/gateway/infra/versions.tf` — Already has S3 backend block
- `environments/dev/modules/gateway-backend.tfvars` — Already points at correct S3 key
- `modules/agent-factory/infra/gateway-main.tf` — Already reads `authorizer_lambda_invoke_arn` (we matched its expected output name)

## Gotchas

1. **Platform must be applied first** — The gateway now depends on `terraform_remote_state` from platform. If platform hasn't been applied, `terraform init/plan` on gateway will fail with "no state found"
2. **Security group IDs from networking module** — The platform's networking module creates RDS/Redis/ALB security groups even though the databases themselves are in the gateway module. This is correct: the SGs need to exist in the VPC before RDS/Redis is created, and they contain inter-SG rules referencing the EKS SG
3. **EKS Auto Mode cluster SG** — `eks_cluster_security_group_id` is the auto-created SG from `aws_eks_cluster.main.vpc_config[0].cluster_security_group_id`, NOT the Terraform-managed `eks_security_group_id` from the networking module. Both are needed for different purposes
4. **Namespace and ServiceAccount** — The platform EKS module creates both `kubernetes_namespace.bedrockgw` and `kubernetes_service_account.gateway_service`. Gateway infra does NOT recreate these
5. **`helm` provider block syntax** — In versions.tf the helm provider uses `kubernetes = { ... }` (map), not `kubernetes { ... }` (block). Watch for this difference between provider versions
6. **`account_id` must be passed** — Unlike agent-factory which uses `data.aws_caller_identity.current.account_id` internally, the gateway needs `account_id` as a variable for the remote state bucket name construction. Deploy scripts should set this: `ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)`

## Useful Patterns
- `data.terraform_remote_state.platform` is the standard way downstream modules consume shared infra — see `modules/agent-factory/infra/main.tf` as the reference implementation
- Using `aws_iam_role_policy` to attach policies to an existing role (by name) is the cleanest way to extend platform IAM without circular dependencies
