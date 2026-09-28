# Shared AgentCore tools — integration and operator handoff (#6671)

`modules/tools/agentcore/agentcore_tools` owns Web Search, Code Interpreter,
Browser's HTTP/Task adapters and their Browser runtime. `adp_tools` supplies
Task authority and storage contracts; `modules/tools/task-sdk` and the trusted
Task host supply the actual tool transport. The shared Lambda and Browser
images copy **only** `modules/tools` source. Cyber owns Common Crawl, sample
analysis, analyst skills, evidence interpretation and reporting. Its old Python
module names are thin compatibility imports; its default Browser route remains
`local:cyber_tools.task_browser.TaskBrowser`. Do not route new Tasks to the
HTTP Browser merely because the HTTP stack is present.

## Authorization and route mapping

The existing AWS_IAM REST API hosts `POST /tools/websearch`,
`POST /tools/code-interpreter`, `POST /tools/browser` (HTTP Browser opt-in).
The Task host, not a client-facing standalone endpoint, selects signed routes
from `ADP_TASK_TOOL_ROUTES`. The tool services verify the exact IAM worker
role and call the existing Task authority with the run/workload proofs; that
authority verifies stored Task ownership, active attempt, current principal
policy, immutable Task grants and current persona configuration. The service
rechecks before provider work. No persona name is special-cased in the shared
provider. Set explicit principal and persona grants **before submitting a new
Task**, e.g. `allowed_tools` and `ADP_TASK_PERSONA_TOOLS` may both include
`websearch.search`, `code_interpreter.start`, `code_interpreter.execute`,
`code_interpreter.result`, `code_interpreter.file`, `code_interpreter.close`,
`cyber.browser_start`, `cyber.browser_step`, `cyber.browser_inspect`,
`cyber.browser_close`. The Task's frozen `tool_grants` must contain each actual
name. A non-Cyber `agent-task-investigator` Task can invoke only the intersection;
adding a grant later does not enlarge an existing Task. `cyber.browser_*` stays
the **exact authorization name**, even in the shared Browser transport; there
is no neutral alias, wildcard or implicit upgrade from old grants. The host's
`browser_cleanup` route maps to Browser `cancel_jobs`; `code_interpreter.close`
and `code_interpreter.cancel_jobs` remain narrowly scoped cleanup operations.

Cyber `cyber.*` investigation and Common Crawl stay on their Cyber routes;
`websearch.search` maps to `/tools/websearch`, `code_interpreter.*` to
`/tools/code-interpreter`. Browser's `cyber.browser_*` and
`cyber.browser_cleanup` continue to map to the local compatibility adapter
unless the operator explicitly enables the separately published HTTP route.
The current Cyber platform-integration worker module derives the optional Web
Search/Browser routes; operators may supply the other exact host tool routes
for a non-Cyber Task using its normal Task worker configuration. Also include
the exact stage-qualified route ARNs in any protected worker invoke boundary.
Do **not** enroll principals or enable flags merely to prove local tests.

All POST bodies use the existing envelope. Substitute actual Task-authorized
UUIDv4 IDs and send the run credential and projected workload proof only through
the **trusted Task host** (examples deliberately contain no bearer secrets):

```http
POST /STAGE/tools/websearch
{"schema_version":"1.0","attempt":{"run":{"task_id":"tsk_TASK-UUID4","invocation_id":"INVOCATION-UUID4","generation":1},"runtime_attempt_id":"ATTEMPT-UUID4"},"operation_id":"OPERATION-UUID4","operation":"search","payload":{"query":"site:example.org advisory","maxResults":1}}
```

```http
POST /STAGE/tools/code-interpreter
{"schema_version":"1.0","attempt":{"run":{"task_id":"tsk_TASK-UUID4","invocation_id":"INVOCATION-UUID4","generation":1},"runtime_attempt_id":"ATTEMPT-UUID4"},"operation_id":"OPERATION-UUID4","operation":"start","payload":{}}
```

```http
POST /STAGE/tools/browser
{"schema_version":"1.0","attempt":{"run":{"task_id":"tsk_TASK-UUID4","invocation_id":"INVOCATION-UUID4","generation":1},"runtime_attempt_id":"ATTEMPT-UUID4"},"operation_id":"OPERATION-UUID4","operation":"browser_start","payload":{"url":"https://example.org"}}
```

Browser start URLs must be provided in the Task inputs; Code Interpreter
`execute`, `result`, `file`, `close` use the opaque Task-owned `session_id` (not
the provider ID). Successful operations keep the 1.0 result/status/artifact
SHA-256 envelopes; Web Search results retain URLs, titles, dates and pricing
estimates ($0.007 per paid search, plus Gateway/model charges). No new provider
limits, prices, hidden caps or direct Cognito/EC2 auth are introduced. See
`docs/tools/agentcore-integration.md` for the per-operation fields, evidence,
unknown outcomes and provider qualifications. An unknown/failed response
**must not** be retried under a new operation ID.

## Build, CI and isolated infrastructure

The independent CI is `.github/workflows/shared-agentcore-tools-ci.yml`.
Cyber regression CI remains `.github/workflows/cyber-tools-ci.yml`. The
**manual-only** `.github/workflows/shared-agentcore-tools-deploy.yml` checks a
reviewed full source SHA from the default branch, protected GitHub environment,
confirmed AWS account and region, existing ECR repositories, repository-tracked
configuration paths and an isolated `tools/agentcore/` S3 state key. `plan`
builds both images in an existing Docker-capable CodeBuild project (no push)
and produces a saved no-delete/no-replace plan. `apply` explicitly pushes source-SHA-tagged images, resolves digests and
applies that saved no-delete/no-replace plan after environment approval. Built
image digests are passed as explicit plan arguments so tfvars cannot override
them. Neither
mode publishes the shared API stage or enables new routes on merge. Supply
`ADP_DEPLOY_ROLE_ARN`, `ADP_TOOLS_BUILD_BUCKET` and
`ADP_TOOLS_CODEBUILD_PROJECT` in the protected environment. For HTTP Browser,
also set `ADP_TOOLS_EKS_CLUSTER_NAME` to the existing cluster; the workflow
uses a private temporary kubeconfig and its deployment role needs EKS describe
and scoped Kubernetes access. pre-create that
Docker-capable project with source override trust (the archived full reviewed
SHA), scoped ECR push on the **two exact** repositories, and S3 write only to
`codebuild/artifacts/shared-agentcore-tools/` in the build bucket. The
protected workflow role needs only the scoped CodeBuild start/read, source
archive and artifact read, and Terraform permissions. Also supply approved
role trust bindings, a separate S3 state key with the repository's existing
DynamoDB lock table and reviewed tfvars. The ARC runner has no Docker daemon;
CI checks packaging/imports offline and the manual CodeBuild lane compiles images.
Check the `aws sts get-caller-identity` account **with the requester** before any
subsequently authorized deployment; follow `AGENTS.md` and
`docs/adp-platform-deployment/deploy-with-agent.md`. Examples of *non-secret*
config shapes: `modules/tools/agentcore/infra/{backend.hcl.example,terraform.tfvars.example}`.
Do not commit actual backend credentials, run proofs, plans or provider results.

Local review, no service provisioning:

```bash
export BG_CONFIG_DIR="$(mktemp -d)"  # isolate the agent config/token store
export PYTHONPATH=modules/tools/agentcore:modules/tools:modules/domain-apps/cyber/tools:modules/domain-apps/cyber/agent/skills/url-analysis:modules/agent-factory/agent-worker-image
python -m pytest -q modules/tools/agentcore/tests modules/domain-apps/cyber/tools/tests
terraform -chdir=modules/tools/agentcore/infra init -backend=false -input=false
terraform -chdir=modules/tools/agentcore/infra validate
terraform -chdir=modules/tools/agentcore/infra test
terraform -chdir=modules/domain-apps/cyber/tools/infra init -backend=false -input=false
terraform -chdir=modules/domain-apps/cyber/tools/infra test
# Optional local image builds if Docker is available; no registry push:
modules/tools/agentcore/infra/build-image.sh ACCOUNT.dkr.ecr.REGION.amazonaws.com/TOOLS_REPO
modules/tools/agentcore/infra/build-image.sh ACCOUNT.dkr.ecr.REGION.amazonaws.com/BROWSER_REPO --browser
```

The Lambda needs a separately qualified exact AgentCore code interpreter ARN
and IAM Gateway Web Search target 1.2.0; `websearch_enabled`,
`code_interpreter_enabled`, `browser_http_enabled` and
`browser_admission_enabled` default **false**. The Browser gateway Lambda,
private single-replica Kubernetes consumer, DynamoDB claim table and SQS FIFO
remain separately gated. Browser network policy permits TCP 443 and DNS: it
is **not** an assertion of arbitrary internet isolation. Destination, DNS and
redirect guards remain in the unchanged Browser runtime. The standalone HTTP
Browser must not be exposed with a public listener, tunnel or Function URL.

## One-time state migration (operator only)

Do not apply the Cyber stack with this version against its old state without
first transferring ownership: Terraform would otherwise propose deleting the
old operation table/routes. Preserve the existing `name` prefix to retain the
**same DynamoDB table** and Browser queue/session records. A plan based solely
on mock providers is not evidence of a safe live migration. Obtain reviewed
state inventories and live AWS resource IDs (unverified at PR time), backups,
API route owner agreement, approved maintenance/rollback window and an
explicit go-ahead. Coordinate concurrent releases and lock **both** backends.

1. Confirm the target account and region; record the Cyber state key and use a
   different locked tools state key. Back up both state files to restricted
   storage (`terraform state pull` contains secrets; never commit or print).
   Pause Cyber/shared applies, stop admitting new Browser sessions, drain its
   SQS queue and confirm *every owned session* stopped or pending for cleanup.
2. Inventory `terraform state list` / `terraform state show` under the Cyber
   backend. Transfer `aws_dynamodb_table.operations[0]`, and all resources
   whose basename occurs in the moved
   `modules/tools/agentcore/infra/{browser.tf,code-interpreter.tf,websearch.tf}`:
   route resource/method/integration, worker IAM inline policy
   instances, Browser FIFO queue/DLQ/table/log/roles/Kubernetes objects,
   and any already-owned Web Search Gateway/target/connector role. For each
   *actual present instance*, record old state address, ID, and new same-name
   address. Under separate locked backends use `terraform state rm 'ADDRESS'`
   in Cyber followed by `terraform import 'ADDRESS' 'EXACT_EXISTING_ID'` in
   tools; use the per-resource Terraform provider import format (IAM policies,
   Kubernetes resources and API Gateway integrations have different IDs).
   Browser's existing Lambda permission transfers unchanged because its Lambda
   name stays the same. **Do not import** `aws_lambda_permission.websearch[0]`
   or `aws_lambda_permission.code_interpreter[0]` onto the new shared Lambda:
   their function identity changes and Terraform would replace them. Record
   and detach those two old permission records from the Cyber state; retain
   the scoped AWS permissions on the old Lambda for rollback. The tools state
   creates fresh permissions on the new Lambda. Likewise detach, rather than
   transfer, `aws_iam_role_policy.websearch_gateway[0]` and
   `aws_iam_role_policy.code_interpreter_provider[0]` attached to the old Cyber
   role; retain them for rollback and let tools create policies on its new role.
   These detached legacy permissions must be inventoried for explicit removal
   after successful rollout and the rollback window, never silently deleted.
   Never import a resource twice or recreate a transferred resource as a replacement. Remove
   orphaned old provider inline-policy addresses from Cyber state only after
   retaining their IDs; the new **shared** Lambda role/policies attach to the
   new role (not the Cyber role). Review old policy retirement separately.
3. Supply `operations_table_name` and exact `operations_table_arn` from the
   transferred table to Cyber (same confirmed account and region). Reset the
   old Cyber Code Interpreter/Web Search admission variables; they do not
   configure the new shared Lambda. Pin the shared Lambda and Browser image digests,
   authority endpoint/allowlist, exact worker role ARN(s), provider ARN and
   qualified Gateway target; update the platform authority trust binding to
   recognize the **new shared execution role** without removing Cyber's role.
   Run `terraform plan` for **both** locked states; require zero `delete` or
   replacement actions and review every change in the stage integrations,
   permissions, queue and protected worker invoke boundary. The shared deploy
   script rejects deletes/replacements. Do not apply a destructive plan.
4. Only after checks and operator approval, explicitly apply reviewed plans;
   publish the shared API deployment with its **existing stage owner** and
   deploy the reviewed worker image/route config. Do not touch unrelated Cyber
   route/Common Crawl settings. Remove obsolete provider policies on the old
   Cyber role only after switching traffic and verifying the shared role works.
   Do not tear down the protected claims table while receipts can be replayed.

## Smoke, cleanup and rollback (operator only)

In a subsequently authorized environment, submit a fresh non-Cyber Task with
explicit principal/persona/frozen grants. Exercise one `websearch.search`
(maxResults=1, disclose the estimated charge), one Code Interpreter start,
execute/result/file/close cycle, and Browser start/inspect/close; check exact
Task identity, artifact digest, citation, CloudTrail/IAM and session stop.
Separately refuse a denied Task before any paid call. Record operation and
session receipts privately without tokens or queries. Mocked local results do
not establish AWS quota, provider region qualification, or live success.

On cancel/revocation/deadline/quota exhaustion, use the host's existing
`code_interpreter.cancel_jobs` / `cyber.browser_cleanup` with the original
Task identity; only close **owned** sessions and retain unknown/pending
receipts for reconciliation. Never replay unknown paid operations; do not
purge the claim tables. For rollback first disable Web Search, Code Interpreter
and Browser HTTP admission, restore old worker route mappings/image digest and
have the existing API stage owner publish the previously reviewed Cyber
integrations. A full pre-extraction rollback also requires its reviewed Cyber
provider environment/IAM configuration and matching image, not only a URL change. Keep Cyber role/trust and claims available until outstanding
operations and sessions are settled. If Cyber's old provider IAM policy was
retired, restore it only after a separate scoped review. State ownership is
not rolled back by switching a URL: reverse the same locked state transfer
only if an approved rollback requires it and plans remain no-destroy.
