# Task API additive deployment prerequisites

Use the existing account, gateway, Cognito pool, request table, authority table,
SQS queue, and private artifact bucket. This prepares Task API access; it does not
enable a pilot or resume an AI-DLC flow. Follow the canonical
[agent deployment guide](../adp-platform-deployment/deploy-with-agent.md).
The [webhook infrastructure hold](../../.github/deployment-holds/webhook-infra.md)
remains in force: do not apply the full webhook Terraform module.

## Prepare concrete artifacts

Run with a current gateway Python environment (boto3 with Cognito V3 and pool-tier
support), after confirming the account. The command performs only reads:

```bash
python modules/gateway/scripts/prepare-task-api-deployment.py \
  --account 000000000101 --region us-east-1 --environment dev \
  --user-pool-id us-east-1_Example002 \
  --artifact-bucket adp-dev-chat-artifacts-000000000101 \
  --output-dir /home/ubuntu/task-delivery-tmp/deploy-prepared
```

Review `bindings.json` and hashes. The generator requires real encrypted tables,
AES256 artifact encryption, the existing pre-token Lambda and a supported pool
tier. It derives request/authority ARNs from live tables and renders the **same
policy template** used by gateway Terraform. Require `task-work-index: ACTIVE`
before admitting tasks; creating its additive GSI is a separately reviewed T1
operation, not a reason to run a full infrastructure apply.

## Apply only reviewed additive changes

The following commands are operator steps after reviewing the prepared files.
Re-run preparation immediately before applying if live pool settings changed.

```bash
aws accessanalyzer validate-policy --region us-east-1 --policy-type IDENTITY_POLICY \
  --policy-document file:///home/ubuntu/task-delivery-tmp/deploy-prepared/gateway-task-api-policy.json
aws iam create-policy --policy-name adp-dev-policy-gateway-task-api \
  --policy-document file:///home/ubuntu/task-delivery-tmp/deploy-prepared/gateway-task-api-policy.json
aws iam attach-role-policy --role-name adp-dev-role-gateway-service \
  --policy-arn arn:aws:iam::000000000101:policy/adp-dev-policy-gateway-task-api
aws cognito-idp create-resource-server --region us-east-1 \
  --cli-input-json file:///home/ubuntu/task-delivery-tmp/deploy-prepared/cognito-task-resource-server.json
aws cognito-idp update-user-pool --region us-east-1 \
  --cli-input-json file:///home/ubuntu/task-delivery-tmp/deploy-prepared/cognito-pool-v3.json
```

If the named policy/resource server already exists, read and compare its current
content first; publish a reviewed policy version or resource-server update only
when required. Do not overwrite unrelated role policies or alter the existing
shared agent client's OAuth scopes.

`UpdateUserPool` resets omitted mutable properties. Never send only
`LambdaConfig`: the generated payload preserves every live mutable setting and
changes only `PreTokenGenerationConfig.LambdaVersion` from V2 to V3. The matching
`cognito-pool-before.json` is the restoration payload. V3 enables the existing
`TokenGeneration_ClientCredentials` code path; human login/refresh claims remain
covered by regression tests. No Lambda code deployment is required solely for
the version selection when the deployed handler already matches this code.

The IAM policy permits task partitions and the exact work index; it grants no
Scan, no bucket listing, no non-task S3 keys, and no authority deletion except
retained `TASK_WORK_ID#` locators and `TASK_ADMISSION_CLEANUP#` cleanup records. DynamoDB IAM `LeadingKeys` restricts partition
keys, not sort keys: writes under `TENANT#` cannot be narrowed to task sort keys
through IAM. Gateway protected authority validation remains mandatory. Existing
legacy role policies are not narrowed or removed by this additive change.

## Persist configuration and verify before activation

Gateway Terraform owns `aws_iam_policy.gateway_task_api`, its role attachment,
and optional `aws_ssm_parameter.task_api_config` entries. Set
`task_api_prerequisites_enabled=true`, the actual artifact bucket and existing
worker-runtime table/KMS bindings in reviewed environment configuration. Import
operator-created resources before a later normal Terraform plan. Cognito owns
`module.cognito.aws_cognito_resource_server.tasks`; import it using
`<pool-id>|adp-tasks` before the next gateway infrastructure apply.

The two gateway deployment paths now read these SSM parameters under
`/adp/dev/gateway/`:

| Parameter | Default | ConfigMap variable |
|---|---|---|
| `task-api-admission-enabled` | `false` | `ADP_TASK_API_ADMISSION_ENABLED` |
| `task-api-read-enabled` | `false` | `ADP_TASK_API_READ_ENABLED` |
| `task-api-worker-enabled` | `false` | `ADP_TASK_API_WORKER_ENABLED` |
| `task-api-recovery-enabled` | `false` | `ADP_TASK_API_RECOVERY_ENABLED` |
| `task-artifact-bucket-name` | empty | `TASK_ARTIFACT_BUCKET_NAME` |

`ADP_RUN_TASK_QUEUE_URL` aliases the existing dispatch queue URL; this does not
enable the unrelated `ADP_RUN_TASKS_ENABLED` migration gate. Leave that gate at
its independently approved value.

Register a dedicated M2M client with only the required `adp-tasks/*` scopes, its
server-owned agent-client metadata, canonical `cognito_m2m` alias, and protected
Task API policy. Defining scopes does not grant access to any client. Verify a
real client-credentials token receives service account/tenant claims; keep the
token and client secret out of logs. Check actual canonical owner resolution,
negative cross-owner access, and the qualified immutable worker image before
enabling the pilot flags. T8 owns client/readiness qualification and required
live evaluations; these infrastructure checks do not substitute for it.

## Dev bindings and later state reconciliation

The reviewed dev overlays now persist the twelve nonsecret SSM values in
[`dev-runtime-bindings.json`](dev-runtime-bindings.json). Gateway
`task_api_runtime_bindings` adds optional queue URL, admission/dispatch/recovery
producer role sets, qualification ID, immutable worker digests and workload
service account. Empty optional bindings are omitted from SSM rather than written
as invalid empty values. Admission and recovery remain false; reads and the
Task API worker are true in dev only. Generic authority remains false. The
webhook overlay prepares verifier RBAC and pins the qualified worker digest while
preserving the separate cyber-browser digest and existing memory/security settings.

Source mapping is reproducible: the built worker source
`ba086a30dd760d8c427e36da33ba56d13e0e3723` has identical worker/task-agent trees to
merged `34d685758`; the manifest records their Git tree IDs and image/build ID.
Gateway source `fd3c63b152fd63ad0ef3f30e0ce6b45587abc235` provides the recorded
runtime image. Its subsequent ConfigMap overlay comes from the merged source;
render its placeholders from the recorded twelve SSM values using the existing
workflow or deploy script. The overlay is deployment configuration, not a claim
that the image was rebuilt. Retain the independently captured image smoke and
actual deployment evidence; these pins do not establish live acceptance.

**Do not run these imports or a full apply during the webhook infrastructure
hold.** These are exact future reconciliation addresses, after authorized backend
initialization and state inspection. Import only resources absent from their
existing owning state. The gateway module is the working directory below:

```bash
cd modules/gateway/infra
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_iam_policy.gateway_task_api[0]' \
  'arn:aws:iam::000000000101:policy/adp-dev-policy-gateway-task-api'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_iam_role_policy_attachment.gateway_task_api[0]' \
  'adp-dev-role-gateway-service/arn:aws:iam::000000000101:policy/adp-dev-policy-gateway-task-api'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'module.cognito.aws_cognito_resource_server.tasks' \
  'us-east-1_Example002|adp-tasks'
```

The five `adp-tasks/*` OAuth scopes are embedded in that resource server and have
no separate Terraform import address. Cognito V3 is an update of the existing
`module.cognito.aws_cognito_user_pool.main`, already owned by gateway state; do
not import it twice or replace the pool. If recovering a genuinely absent state
entry, its import ID is `us-east-1_Example002`.

The manually added public `/v1/tasks` resource/method/integration is represented
by the OpenAPI `body` of existing
`module.api_gateway.aws_api_gateway_rest_api.main`, API ID `59o2rakc50`. There are
no separate resource/method/integration Terraform addresses to import. Preserve
that existing API state entry and reconcile the OpenAPI body with
`enable_task_api_route=true`; do not create duplicate standalone route resources.
Only if the entire API state entry is absent would its import be:

```bash
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'module.api_gateway.aws_api_gateway_rest_api.main' '59o2rakc50'
```

The scoped Lambda permission address is
`module.api_gateway.aws_lambda_permission.task_api_api_gateway[0]`. If the live
statement already exists as `AllowAPIGatewayInvokeTaskSubmit`, import with:

```bash
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'module.api_gateway.aws_lambda_permission.task_api_api_gateway[0]' \
  'adp-dev-github-webhook/AllowAPIGatewayInvokeTaskSubmit'
```

Check the actual Lambda policy statement first; permission IDs are statement IDs,
not API resource IDs. Do not assume a differently named manual statement has
already been reconciled. None of these instructions executes an import or apply.

Every manually prepared SSM binding is imported at its exact map key. From the
same gateway working directory, after the hold is lifted and absent-state checks:

```bash
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-api-admission-enabled"]' '/adp/dev/gateway/task-api-admission-enabled'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-api-read-enabled"]' '/adp/dev/gateway/task-api-read-enabled'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-api-worker-enabled"]' '/adp/dev/gateway/task-api-worker-enabled'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-api-recovery-enabled"]' '/adp/dev/gateway/task-api-recovery-enabled'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-artifact-bucket-name"]' '/adp/dev/gateway/task-artifact-bucket-name'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-api-queue-url"]' '/adp/dev/gateway/task-api-queue-url'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-qualification-id"]' '/adp/dev/gateway/task-qualification-id'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-worker-image-digests"]' '/adp/dev/gateway/task-worker-image-digests'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-worker-service-account"]' '/adp/dev/gateway/task-worker-service-account'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-admission-producer-roles"]' '/adp/dev/gateway/task-admission-producer-roles'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-dispatch-producer-roles"]' '/adp/dev/gateway/task-dispatch-producer-roles'
terraform import -var-file=../../../environments/dev/modules/gateway.tfvars \
  'aws_ssm_parameter.task_api_config["task-recovery-producer-roles"]' '/adp/dev/gateway/task-recovery-producer-roles'
```
