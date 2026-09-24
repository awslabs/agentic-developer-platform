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
  --account 879318057152 --region us-east-1 --environment dev \
  --user-pool-id us-east-1_JEhv9xSGG \
  --artifact-bucket adp-dev-chat-artifacts-879318057152 \
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
  --policy-arn arn:aws:iam::879318057152:policy/adp-dev-policy-gateway-task-api
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
retained `TASK_WORK_ID#` locators. DynamoDB IAM `LeadingKeys` restricts partition
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
