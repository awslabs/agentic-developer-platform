# Cyber tools Lambda deployment

This stack packages the cyber-owned service and adds POST `/tools/cyber` to an
existing REST API. It does not deploy the gateway, create a Function URL, create
an API stage, or replace a shared API deployment. Both infrastructure activation
(`enabled`) and operation admission (`capability_enabled`) default to false.

Follow the repository's [agent deployment guide](../../../../../docs/adp-platform-deployment/deploy-with-agent.md)
and [quickstart](../../../../../docs/adp-platform-deployment/deploy-quickstart.md).
Confirm the active AWS account before any apply. These are separate module
maintenance steps, not an extension to `deploy-all.sh` or a claim of live wiring.

## Prerequisites and ownership

* An existing ECR repository and trusted build identity permitted to push only
  that repository. The image is a separate Python Lambda package; the build
  context is the repository root and copies only `modules/tools/adp_tools` and
  `modules/domain-apps/cyber/tools/cyber_tools`. Gateway source is excluded.
* The existing REST API ID, execution ARN, stage name, and resource ID of its
  `/tools` parent. If `/tools` is absent, its API owner must create it first. If
  `/tools/cyber` already exists, coordinate import/ownership instead of duplicating it.
* Exact worker IAM role ARNs. The module adds route-scoped invoke policies;
  workers must also be configured to use the new tools endpoint. The handler
  independently checks the authenticated API Gateway caller against these roles.
* The platform's generic `tool-authorize` and `artifact` routes and an explicit
  trust registration for this Lambda execution role. Supply exactly their two
  stage-qualified POST execution ARNs and the HTTPS authority base endpoint.
  IAM permission alone does not establish application authorization.
* Existing sample bucket, worker queues, result table, and optional CAPE/VT
  Secrets Manager ARNs. Supply non-secret endpoint/queue configuration separately.
  The existing cyber queue policy explicitly denies other producers: its owner
  must add this Lambda role to the allowed producer list without removing the
  gateway role during migration. This module never replaces queue policies.
* If using private CAPE/browser endpoints, provide private subnets and `vpc_id` for a service-owned security group
  (no ingress, HTTPS egress), or existing security groups with the required routing. NAT or service endpoints
  must support the authority API, S3, SQS, DynamoDB and Secrets Manager. This module
  can add narrowly scoped HTTPS ingress to explicitly named private endpoint security
  groups using `endpoint_security_group_ids`; the source is only its owned Lambda
  group. It does not replace shared groups, KMS key policies, or API resource policies.

The service owns a separate encrypted DynamoDB table with point-in-time recovery
and deletion protection. It has no access to the platform Task table. Artifacts
are uploaded through the platform authority API; the Lambda's S3 permission is
limited to the existing service-principal sample namespace. The table has no TTL:
operation claims must outlive possible retries. Define a reviewed retention policy
before enabling any cleanup that could remove non-replay evidence.

## Build, plan, and apply

From a clean reviewed checkout:

```bash
modules/domain-apps/cyber/tools/infra/build-image.sh \
  ACCOUNT.dkr.ecr.REGION.amazonaws.com/cyber-tools --push
```

The final output is an immutable `repository@sha256:...` URI. Use it as
`image_uri`; tags and empty placeholders cannot activate the Lambda. The optional
`buildspec.yml` runs the same publisher in an independently provisioned trusted
CodeBuild project. It does not create a build project or change existing CI.

Use a **separate S3 state key** for this module. Place explicit resource inputs in
an operator-owned tfvars file; never put credential values in it. With
`enabled=true` and `capability_enabled=false`, create a saved plan:

```bash
export EXPECTED_AWS_ACCOUNT_ID=123456789012
modules/domain-apps/cyber/tools/infra/deploy.sh plan \
  /absolute/backend.hcl /absolute/cyber-tools.tfvars /absolute/cyber-tools.tfplan
terraform -chdir=modules/domain-apps/cyber/tools/infra show /absolute/cyber-tools.tfplan
```

After the authorized review, apply that exact saved plan:

```bash
modules/domain-apps/cyber/tools/infra/deploy.sh apply \
  /absolute/backend.hcl /absolute/cyber-tools.tfplan
```

The script checks the active account and Terraform pins the provider to the
configured account. It does not publish an API deployment. The shared API owner
must include the new method/integration in its next reviewed deployment and
update its stage through the existing ownership process. An uncoordinated
`create-deployment --stage-name` can overwrite another owner's release and must
not be added to this module's apply step.

After route publication, verify unsigned requests are rejected, an unapproved
signed role is rejected, and an approved worker reaches the disabled capability
response. Confirm gateway tool authorization accepts only registered service
identity, task/attempt bindings, scopes and policy. Then apply a new reviewed plan
with `capability_enabled=true`, run a bounded task, and verify returned artifacts,
claim replay and cancellation cleanup. Verify the deployed Lambda's resolved
image digest matches the recorded build before calling this complete.

To stop new operations, set `capability_enabled=false`; cleanup stays available.
Do not set `enabled=false` as a pause: it plans resource deletion, and operation
claims intentionally have deletion protection. A full retirement requires
settling active jobs, preserving non-replay evidence, and coordinated API removal.

## Local checks (no AWS mutations)

```bash
terraform -chdir=modules/domain-apps/cyber/tools/infra init -backend=false -input=false
terraform -chdir=modules/domain-apps/cyber/tools/infra validate
terraform -chdir=modules/domain-apps/cyber/tools/infra test
python3 -m pytest modules/domain-apps/cyber/tools/infra/tests/test_package_contract.py
```

Terraform tests use a mocked provider. They validate default-off behavior,
immutable-image admission and exact IAM route scope, and do not establish live
network reachability or queue/application trust.
