# Cyber tools Lambda deployment

This stack packages the cyber-owned service and adds POST `/tools/cyber` to an
existing REST API. It does not deploy the gateway, create a Function URL, create
an API stage, or replace a shared API deployment. Both infrastructure activation
(`enabled`) and operation admission (`capability_enabled`) default to false.

The same Lambda image optionally serves IAM `POST /tools/code-interpreter`,
separately gated by `code_interpreter_enabled=false`. See the shared
[AgentCore Task contract](../../../../../docs/tools/agentcore-integration.md)
for its grants, schemas, SANDBOX prerequisites, host route mapping, session
reconciliation and rollback. Provisioning this module does **not** publish
the existing REST API stage; coordinate deployment with its owner. Set the
exact operator-owned resource ID and ARN through `code_interpreter_identifier`
and `code_interpreter_arn`; verify isolation before enabling the separate flag.

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

## Additive Browser HTTP tool (#6636)

`browser_http_enabled=false` and `browser_admission_enabled=false` are separate
defaults. Enable the former to plan a dedicated `POST /tools/browser` AWS_IAM
route, Lambda gateway, encrypted FIFO queue and dead-letter queue, protected
claim/session DynamoDB table and **one** private EKS consumer. No LoadBalancer,
Function URL, public node access, cyber Lambda routing change or API stage
deployment is created. The EKS consumer has no inbound listener; it receives
only FIFO jobs. Its IRSA role can read Task authority, operate only its own
queue/table and use regional AgentCore Browser lifecycle/CDP APIs. The gateway
role can only authorize, read/write claims and enqueue. Set explicit namespace,
OIDC provider ARN/issuer, pinned `browser_service_image` and pinned `image_uri`
for the gateway, exact worker roles, Task authority endpoint and ARNs. Supply
Kubernetes provider credentials for the *existing* private cluster; this stack
does not create a cluster. Use a separate state key and coordinate ownership of
the shared `/tools` parent resource with the API owner.

Build the separate consumer image from the repository root with
`docker build --platform linux/amd64 --provenance=false -f modules/domain-apps/cyber/tools/Dockerfile.browser -t <private-ECR-repo>:<source-revision> .`.
Publish it only under an explicitly authorized ECR account and pin its returned
immutable digest as `browser_service_image`. Build the existing Lambda package
with `build-image.sh` above and pin its digest as `image_uri`. The service image
includes the maintained `local_browser`/Playwright adapter; it does not change
the Task worker image. Use `deploy.sh plan` and `deploy.sh apply` with confirmed
account and reviewed saved plan, then ask the shared API owner to publish the
new stage; merge does **not** deploy it. Set `browser_admission_enabled=true`
only in a later reviewed plan to accept new HTTP starts. Set
`browser_tools_endpoint` and `task_browser_http_enabled=true` in the cyber
worker integration **only for new Tasks**; all five browser routes, including
cleanup, switch together, while Common Crawl and default native Browser do not.
Keep older Task routing pinned; never fall back between backends on timeout.

An operation ID and verified Task/attempt/payload digest are durably claimed
before the provider call. The worker polls the same ID up to 210 seconds. FIFO
dispatch skips claims older than 225 seconds; outstanding receipts become
unknown after 240 seconds. Max four recorded sessions per Task attempt. The
native process lease defaults to 600 seconds (configured by
`browser_session_seconds` / `CYBER_BROWSER_SESSION_SECONDS` in the consumer image);
the owned session record retains its explicit expiry timestamp. The native
adapter passes this configured lease to AgentCore; its 15-minute service
default does not override our explicit session timeout. If a pod dies,
later actions fail closed; session records let an owned cleanup attempt stop
and verify the provider session using IRSA. A failed stop remains pending.
Do not reset the table: its claims prevent uncertain replay. Queue messages
contain short-lived Task proofs and are SSE-encrypted; restrict queue readers,
DLQ access and retention to the trusted service/operators. No model input can
select the queue, endpoint, region, provider ID or tenant identity.

Browser charges depend on concurrent active sessions, session seconds and
provider data transfer; the FIFO/SQS, DynamoDB, EKS pod and Task artifacts add
separate operational costs. Watch browser session metrics, SQS oldest age and
DLQ, table pending/unknown rows, pod restarts, artifact bytes and CloudTrail
stops. These are observations, **not** settled AWS cost. The Browser adapter
refuses explicit forbidden URLs/scope; it does not promise filtering of every
page redirect or subrequest inside the AWS-managed browser network.

For rollback set `browser_admission_enabled=false` (cleanup and same-ID polling
remain available), route only *new* attempts back to local via
`task_browser_http_enabled=false`, then drain/stop outstanding owned sessions.
Do not disable `browser_http_enabled` while claims or sessions are unsettled:
that would plan deletion of the queue/route and fail on the protected table.
Retire only after verified close or explicit provider lease expiry review,
retaining claims for the non-replay window. Never replace concurrent stage
deployments, shared login stores or existing cyber endpoints.

Mock-provider tests use isolated `BG_CONFIG_DIR` and do not create AWS sessions:

```bash
env -u ADP_TASK_TOOL_ROUTES -u ADP_TASK_TOOL_CLEANUP BG_CONFIG_DIR="$(mktemp -d)" \
  PYTHONPATH=modules/domain-apps/cyber/tools:modules/tools:modules/domain-apps/cyber/agent/skills/url-analysis:modules/agent-factory/agent-worker-image \
  python -m pytest -q modules/domain-apps/cyber/tools/tests modules/domain-apps/cyber/tools/infra/tests modules/agent-factory/agent-worker-image/tests/test_task_run_client.py
terraform -chdir=modules/domain-apps/cyber/tools/infra test
terraform -chdir=modules/domain-apps/cyber/infra/platform-integration test
```

For a separately authorized live smoke, first confirm the account, region,
IAM, quota and a capped one-session budget. Start one controlled public URL on
a new HTTP-opted Task, inspect evidence/artifact receipts and close within a
600-second lease. Verify `TERMINATED` through the provider API, record only
non-secret session/account identifiers in protected operator records, and
drain the queue before disabling admission. No live smoke is claimed here.

Web Search's optional dedicated IAM Gateway/target, pinned connector version,
exact IAM route and disabled-by-default rollout are specified in
`docs/tools/agentcore-integration.md`. Existing Browser and Common Crawl routes
are not switched by this stack.
