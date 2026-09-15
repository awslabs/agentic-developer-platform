# Bedrock invocation logging

Shared platform Terraform enables AWS Bedrock model invocation logging by default in the platform's AWS account and `aws_region`. Both `deploy-all.sh` and the platform infrastructure workflow apply this configuration. No separate console step or CLI enablement script is needed.

The module in [`platform/infra/modules/bedrock-invocation-logging`](../../platform/infra/modules/bedrock-invocation-logging) provisions:

- CloudWatch log group `/aws/bedrock/<name_prefix>/model-invocations`.
- S3 bucket `<name_prefix>-bedrock-logs-<account_id>-<region>`, with `invocations/` for log records and `large-data/` for CloudWatch payloads exceeding the inline limit.
- A rotating KMS key for both destinations, a Bedrock delivery role scoped to its log stream, and an S3 delivery policy restricted to Bedrock in this account and region. Public access and ACLs are disabled; TLS is required.
- Text, image, embedding and video delivery enabled, with **30-day retention** in CloudWatch and S3 by default. S3 expiration is asynchronous.

The large-data destination matters for Codex and agent prompts: AWS includes JSON bodies up to 100 KB inline and stores larger bodies/binary data separately. Follow the S3 references when investigating a request; a metadata-only CloudWatch event does not mean the full body was lost.

These are provider invocation logs, including prompts and outputs where supported, without ADP's application-level redaction. They cover supported Runtime traffic throughout the account/region, including non-ADP callers. They are separate from the gateway's chat logs and budget-settlement objects; the new bucket does not trigger the budget tracker or create a second cost ledger. Log readers need explicitly authorized CloudWatch/S3 access and KMS decrypt access; the module does not grant gateway users access to these raw logs. CloudWatch/S3 ingestion, storage and KMS requests incur AWS charges.

## Configuration and ownership

The following defaults are platform Terraform variables and can be overridden in `environments/<env>/platform.tfvars`:

```hcl
manage_bedrock_invocation_logging       = true
bedrock_invocation_logging_enabled      = true
bedrock_invocation_log_retention_days   = 30
```

Bedrock has **one invocation-logging configuration per account/region**, not one per application or environment. Exactly one Terraform state must own it. If another environment or platform team already owns it, set `manage_bedrock_invocation_logging = false` **before this state's first apply**. Changing a non-owning state must not overwrite the owner's destinations.

For an existing manually configured singleton, inspect its current destinations first:

```sh
aws bedrock get-model-invocation-logging-configuration --region <region>
```

To adopt it into the platform state, initialize the correct backend and import the regional configuration, then review the plan. Importing does not preserve its old destinations: this module's plan will move delivery to the ADP destinations.

```sh
terraform -chdir=platform/infra import \
  -var-file=../../environments/<env>/platform.tfvars \
  'module.bedrock_invocation_logging[0].aws_bedrock_model_invocation_logging_configuration.this[0]' \
  <region>
```

This is also the upgrade path for existing ADP deployments. Where the singleton is absent, the next platform apply creates it. Nothing is enabled in a running account merely by checking out this code, and enabling it cannot recover past invocations.

The deployment identity needs `bedrock:GetModelInvocationLoggingConfiguration`, `bedrock:PutModelInvocationLoggingConfiguration`, `bedrock:DeleteModelInvocationLoggingConfiguration`, and `iam:PassRole` for the delivery role, in addition to permissions for its S3/CloudWatch/KMS/IAM resources. The checked-in runner IAM policy and permissions boundary include the required additions. For an existing CI runner, apply the runner IAM update with the normal privileged deployment identity **before** its first platform plan/apply using this change; its old boundary does not allow these Bedrock control-plane actions. Fresh manual deployments use the bootstrap administrator and subsequent runner deployments inherit the updated policy.

To stop collecting new invocations while retaining stored logs and the encryption key, set `bedrock_invocation_logging_enabled = false` and apply the owning platform state. Destinations continue to enforce their retention periods. Use this switch for an operational disable; do not use the ownership switch as a logging toggle. Removing the module or destroying the platform attempts to remove its resources, and a nonempty S3 bucket blocks deletion because `force_destroy` is false. Retain/export the logs and key, or explicitly arrange log disposal, before a full teardown.

## Coverage

[AWS supports invocation logging on `bedrock-runtime`](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html), including its OpenAI-compatible Responses and Chat Completions APIs. This covers the Astra Runtime path examined in #4997. Calls to `bedrock-mantle` are not currently captured by this feature; application-level logging remains necessary for that endpoint.

Only same-account, same-region destinations are supported. If callers use other Bedrock endpoint regions or signing accounts, provision the module once in each of those account/region scopes using the corresponding AWS provider. Configuring the platform region does not enable every AWS region. Cross-region model routing does not make this a global logging configuration.

This setup establishes prospective provider logging, not a guarantee that every attempted client call has a complete record. Validate delivery for the models/APIs actually used, allow for ingestion lag, and correlate provider request IDs with gateway requests. It does not repair the gateway Astra route's usage-only application records or past missing cache telemetry.

## Verification after deployment

Read the Terraform output and compare it to the AWS configuration in the deployment region:

```sh
terraform -chdir=platform/infra output -json bedrock_invocation_logging
aws bedrock get-model-invocation-logging-configuration --region <region>
aws logs describe-log-streams \
  --region <region> \
  --log-group-name /aws/bedrock/<name_prefix>/model-invocations
aws s3 ls s3://<bucket_name>/invocations/ --recursive --summarize
aws s3 ls s3://<bucket_name>/large-data/ --recursive --summarize
```

After ordinary application traffic, verify a completed invocation appears and its request/response payload or S3 references can be read. Confirm a request larger than 100 KB has its referenced object. Empty streams immediately after enabling logging do not prove a failure; avoid generating paid inference solely to poll delivery. Access-denied delivery errors should prompt checks of the bucket policy, Bedrock role trust, CloudWatch role permissions and KMS policies.

## Infrastructure tests

The platform plan workflow runs the module's mocked Terraform tests. With Terraform 1.7 or later:

```sh
terraform -chdir=platform/infra/modules/bedrock-invocation-logging init -backend=false
terraform -chdir=platform/infra/modules/bedrock-invocation-logging test
```

They verify default modalities, both destinations, large-payload storage, source-scoped delivery permissions, encryption, retention, and disabling logging without deleting its destinations. Mocked applies do not call AWS or prove live delivery; the post-deployment checks above supply that evidence.

References: [AWS invocation logging](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html), [Terraform resource and singleton/import behavior](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/bedrock_model_invocation_logging_configuration).
