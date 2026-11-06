# ADP Deployment Manifest

What gets deployed, where, and how to validate each component.

## Platform (Shared Foundation)

Terraform: `platform/infra/main.tf`
State key: `dev/platform/terraform.tfstate`

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| VPC + subnets | VPC | `aws ec2 describe-vpcs --filters "Name=tag:Project,Values=adp" --query 'Vpcs[].{Id:VpcId,State:State}'` | State: available |
| EKS cluster | EKS | `aws eks describe-cluster --name adp-dev-eks-cluster --query 'cluster.status'` | ACTIVE |
| EKS nodes | EC2 (Auto Mode) | `kubectl get nodes` | ≥1 node Ready |
| ECR repos | ECR | `aws ecr describe-repositories --query 'repositories[?starts_with(repositoryName,\`adp-\`)].repositoryName'` | adp-gateway, adp-agent-runtime |
| IAM roles | IAM | `aws iam list-roles --query 'Roles[?starts_with(RoleName,\`adp-dev\`)].RoleName'` | cluster role, node role |
| OIDC provider | IAM | `aws iam list-open-id-connect-providers` | EKS OIDC listed |
| Bedrock model agreements | Bedrock | `aws bedrock get-foundation-model-availability --model-id anthropic.claude-opus-4-6-v1 --query 'agreementAvailability.status'` | AVAILABLE (set by `platform/scripts/enable-bedrock-models.sh`, run automatically by deploy-all.sh / platform-infra-apply.yml) |

## Gateway Module

### Gateway Infrastructure

Terraform: `modules/gateway/infra/main.tf`
State key: `dev/modules/gateway/terraform.tfstate`

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| PostgreSQL | RDS | `aws rds describe-db-instances --query 'DBInstances[?starts_with(DBInstanceIdentifier,\`bedrockgw\`)].{Id:DBInstanceIdentifier,Status:DBInstanceStatus}'` | available |
| Redis | ElastiCache | `aws elasticache describe-replication-groups --query 'ReplicationGroups[?starts_with(ReplicationGroupId,\`bedrockgw\`)].{Id:ReplicationGroupId,Status:Status}'` | available |
| Cognito User Pool | Cognito | `aws cognito-idp list-user-pools --max-results 10 --query 'UserPools[?starts_with(Name,\`bedrockgw\`)].{Name:Name,Id:Id}'` | Pool listed |
| Cognito Identity Pool | Cognito | `aws cognito-identity list-identity-pools --max-results 10` | Pool listed |
| CloudFront distribution | CloudFront | `aws cloudfront list-distributions --query 'DistributionList.Items[?starts_with(Comment,\`bedrockgw\`)].{Id:Id,Domain:DomainName,Status:Status}'` | Deployed |
| S3 frontend bucket | S3 | `aws s3 ls \| grep bedrockgw.*frontend` | Bucket listed |
| S3 CloudFront logs | S3 | `aws s3 ls \| grep bedrockgw.*logs` | Bucket listed (if enabled) |
| ALB (internal) | ELB | `kubectl get ingress -n adp-gateway` | ADDRESS populated |
| CloudTrail | CloudTrail | `aws cloudtrail describe-trails --query 'trailList[?starts_with(Name,\`bedrockgw\`)].Name'` | Trail listed |
| CloudWatch dashboard | CloudWatch | `aws cloudwatch list-dashboards --query 'DashboardEntries[?starts_with(DashboardName,\`bedrockgw\`)].DashboardName'` | Dashboard listed |
| ECR repo | ECR | `aws ecr describe-repositories --repository-names adp-gateway` | Exists |
| Budget Lambda | Lambda | `aws lambda list-functions --query 'Functions[?starts_with(FunctionName,\`bedrockgw\`)].FunctionName'` | Functions listed (if enabled) |
| API Gateway | API GW | `aws apigateway get-rest-apis --query 'items[?starts_with(name,\`bedrockgw\`)].{Name:name,Id:id}'` | API listed (if enabled) |
| Lambda Authorizer | Lambda | `aws lambda list-functions --query 'Functions[?contains(FunctionName,\`authorizer\`)].FunctionName'` | Function listed (if enabled) |

### Gateway Backend (EKS)

Deploy: `kubectl apply -f modules/gateway/k8s/ -n adp-gateway`

| Resource | K8s Kind | Validation Command | Expected |
|----------|---------|-------------------|----------|
| Gateway pods | Deployment | `kubectl get pods -n adp-gateway -l app=bedrockgateway` | 2 pods Running |
| Service | Service | `kubectl get svc -n adp-gateway` | ClusterIP on port 8080 |
| Ingress (ALB) | Ingress | `kubectl get ingress -n adp-gateway` | ADDRESS populated |
| ConfigMap | ConfigMap | `kubectl get configmap bedrockgateway-config -n adp-gateway` | Exists |
| Secrets | Secret | `kubectl get secret bedrockgateway-secrets -n adp-gateway` | Exists |
| PDB | PodDisruptionBudget | `kubectl get pdb -n adp-gateway` | Exists |
| Health check | HTTP | `kubectl exec -n adp-gateway deploy/bedrockgateway -- curl -s http://localhost:8080/health` | 200 OK |

### Gateway Frontend (S3 + CloudFront)

Deploy: `npm run build` → `aws s3 sync` → CloudFront invalidation

| Resource | Where | Validation Command | Expected |
|----------|-------|-------------------|----------|
| Static files | S3 | `aws s3 ls s3://<frontend-bucket>/ --summarize \| tail -1` | Total Objects > 0 |
| CDN | CloudFront | `curl -s -o /dev/null -w "%{http_code}" https://<cloudfront-domain>/` | 200 |
| API proxy | CloudFront → ALB | `curl -s https://<cloudfront-domain>/api/health` | `{"status":"healthy"}` JSON **body** — never assert HTTP 200 alone: when the VPC origin is missing, `/api/*` falls through to the S3 SPA fallback which also returns 200 (HTML). Both 608-deploy incidents (#3085) were masked by status-only probes. |

## Agent Factory Module

### Agent Factory Infrastructure

Terraform: `modules/agent-factory/infra/main.tf`
State key: `dev/modules/agent-factory/terraform.tfstate`

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| Runner IAM role (IRSA) | IAM | `aws iam get-role --role-name adp-dev-agent-runner-role --query 'Role.Arn'` | Role exists |
| Permissions boundary | IAM | `aws iam get-policy --policy-arn arn:aws:iam::<ACCOUNT>:policy/adp-dev-agent-runner-boundary` | Policy exists |
| Secrets (GitHub App) | Secrets Manager | `aws secretsmanager list-secrets --filter Key=name,Values=adp/gh-app --query 'SecretList[].Name'` | 6 secrets (3 IDs + 3 keys) |
| Beads DynamoDB | DynamoDB | `aws dynamodb describe-table --table-name adp-dev-agent-beads-manifest --query 'Table.TableStatus'` | ACTIVE |
| Beads S3 bucket | S3 | `aws s3 ls \| grep adp-dev-agent-beads-state` | Bucket listed |
| EKS access entry | EKS | `aws eks list-access-entries --cluster-name adp-dev-eks-cluster \| grep runner` | Entry listed |

### Agent Factory — ARC Runners (EKS)

Deploy: Helm via Terraform `arc-runner` module

| Resource | K8s Kind | Validation Command | Expected |
|----------|---------|-------------------|----------|
| ARC controller | Deployment | `kubectl get pods -n arc-systems` | 1 pod Running |
| Runner namespace | Namespace | `kubectl get ns arc-runners` | Active |
| Runner service account | ServiceAccount | `kubectl describe sa github-runner-sa -n arc-runners` | IRSA annotation present |
| Runner scale set | AutoscalingRunnerSet | `kubectl get pods -n arc-runners` | 0 pods (scales on demand) |
| GitHub registration | GitHub API | `gh api orgs/<ORG>/actions/runners --jq '.total_count'` | ≥0 (runners register on job) |

### Agent Factory — Agent Gateway

Terraform: `modules/agent-factory/infra/gateway-main.tf`
Deploy: `modules/agent-factory/scripts/deploy-gateway.sh`

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| WebSocket API | API Gateway v2 | `aws apigatewayv2 get-apis --query 'Items[?starts_with(Name,\`adp\`)].{Name:Name,Endpoint:ApiEndpoint}'` | API listed with wss:// endpoint |
| Input SQS queue | SQS | `aws sqs list-queues --queue-name-prefix adp --query 'QueueUrls'` | Queue URL listed |
| Response SQS queue | SQS | (same as above) | Second queue URL |
| DynamoDB sessions | DynamoDB | `aws dynamodb list-tables --query 'TableNames[?starts_with(@,\`adp\`) && contains(@,\`session\`)]'` | Table listed |
| Ingest Lambda | Lambda | `aws lambda list-functions --query 'Functions[?contains(FunctionName,\`ingest\`)].FunctionName'` | Function listed |
| Response Lambda | Lambda | `aws lambda list-functions --query 'Functions[?contains(FunctionName,\`response\`)].FunctionName'` | Function listed |
| KEDA | Helm | `kubectl get pods -n keda` | KEDA operator Running |
| SQS consumer | ScaledJob | `kubectl get scaledjobs -n adp-gateway-agents` | ScaledJob listed |
| Consumer image | ECR | `aws ecr describe-images --repository-name adp-agent-gateway --query 'imageDetails[0].imageTags'` | Image tagged |
| Chat start-failure recovery (#8061) | EKS + SQS | Force one sandbox start failure on dev (e.g. a temporarily low `ADP_CHAT_SANDBOX_START_DEADLINE_MS` on the chat-agent ScaledJob), send a turn, then `kubectl logs -n adp-gateway-agents -l app.kubernetes.io/name=chat-agent-worker --since=30m \| grep '"step":"sandbox_start"'` and `aws sqs get-queue-attributes --queue-url $(aws sqs get-queue-url --queue-name adp-dev-agent-gateway-dlq.fifo --query QueueUrl --output text) --attribute-names ApproximateNumberOfMessages` | Log names the pod (`"status":"failed","pod_uid":…`) and ends with `"status":"reconciled"`, or the redelivered turn completes (`"step":"dispatch","status":"completed"`) with no `reconciliation deadline exceeded`; the turn ends as a retryable interruption instead of hanging; DLQ `ApproximateNumberOfMessages` unchanged |

### Agent Factory — Delegated chat (ScaledJob + pre-pull DaemonSet)

Deploy: `modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh`, run by `deploy-all.sh --chat delegated`
(self-managed) and by `.github/workflows/chat-agent-deploy.yml` (ADP-managed). On both paths the script
resolves the Crew values from SSM when the caller leaves them unset (#8058; only `ParameterNotFound` means empty — any other read failure such as `AccessDeniedException` stops the rollout, and the ADP-managed identity `<prefix>-chat-trusted-deployment` holds the read grant in `platform/automation-infra/chat-deployment.tf`) and pins both release digests
into the `chat-agent-image-prepull` DaemonSet, waiting for every node, before it applies the ScaledJob
(#8059). After the ScaledJob apply the same script publishes the supervisor/sandbox digests the gateway
verifies chat workloads against to SSM (`/adp/<env>/gateway/chat-authority-worker-images`,
`chat-sandbox-image-digests`, `chat-crew-image-digests`; put only when the value differs) and converges the
live `bedrockgateway-config` in `adp-gateway` (`ADP_CHAT_WORKER_IMAGE_DIGESTS`, `ADP_CHAT_SANDBOX_IMAGE_DIGESTS`,
`ADP_CHAT_CREW_IMAGE_DIGESTS` plus the CA/URL keys) with a gateway restart bounded by
`ADP_CHAT_GATEWAY_ROLLOUT_TIMEOUT` (default `600s`) — on both paths, so an ADP-managed image deploy no
longer ends in `chat creation image refused` until an operator publishes by hand (#8086; the ADP-managed
identity holds `ssm:PutParameter` on the three parameters and the `adp:chat-release-publish` RBAC group,
`platform/automation-infra/chat-release-publish-rbac.yaml`). One-shot check of every row below (what the
workflow's drift step runs):
`ENVIRONMENT=<env> SUPERVISOR_IMAGE=<registry>/adp-chat-agent@sha256:<supervisor> SANDBOX_IMAGE=<registry>/adp-chat-agent@sha256:<sandbox> bash modules/agent-factory/agent/k8s/verify-chat-rollout.sh`.

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| Chat ScaledJob | KEDA | `kubectl -n adp-gateway-agents get scaledjob chat-agent-worker -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'` | `True` |
| ScaledJob env == SSM (#8058) | SSM / KEDA | `diff <(kubectl -n adp-gateway-agents get scaledjob chat-agent-worker -o jsonpath='{.spec.jobTargetRef.template.spec.containers[0].env[?(@.name=="ADP_CREW_DASHBOARD_ORIGIN")].value}') <(aws ssm get-parameter --name /adp/<env>/gateway/crew-dashboard-origin --query Parameter.Value --output text 2>/dev/null \| sed 's/^None$//' \| tr -d '\n')` | no output (identical; both empty while the Crew dashboard edge is off) |
| Sandbox start deadline on the ScaledJob (#8059) | KEDA | `kubectl -n adp-gateway-agents get scaledjob chat-agent-worker -o jsonpath='{.spec.jobTargetRef.template.spec.containers[0].env[?(@.name=="ADP_CHAT_SANDBOX_START_DEADLINE_MS")].value}'` | `120000` (or the `ADP_CHAT_SANDBOX_START_DEADLINE_MS` the deploy exported; always 5000..300000) |
| Persistent-executor default == SSM (#8115) | SSM / KEDA | `diff <(kubectl -n adp-gateway-agents get scaledjob chat-agent-worker -o jsonpath='{.spec.jobTargetRef.template.spec.containers[0].env[?(@.name=="ADP_CHAT_PERSISTENT_EXECUTOR")].value}') <(aws ssm get-parameter --name /adp/<env>/gateway/chat-persistent-executor --query Parameter.Value --output text 2>/dev/null \| sed 's/^None$//' \| grep . \|\| echo sandbox-loop \| tr -d '\n')` | no output (identical; `claude-code` on dev once published, `sandbox-loop` while the parameter is unpublished/empty). Publish with `aws ssm put-parameter --name /adp/<env>/gateway/chat-persistent-executor --type String --overwrite --value claude-code` then re-run the chat rollout (`deploy-all.sh --chat delegated` also publishes `ADP_CHAT_PERSISTENT_EXECUTOR` from the environment) |
| DaemonSet digest == ScaledJob sandbox digest (#8059) | EKS | `diff <(kubectl -n adp-gateway-agents get daemonset chat-agent-image-prepull -o jsonpath='{.spec.template.spec.containers[?(@.name=="prepull-sandbox")].image}') <(kubectl -n adp-gateway-agents get scaledjob chat-agent-worker -o jsonpath='{.spec.jobTargetRef.template.spec.containers[0].env[?(@.name=="ADP_CHAT_SANDBOX_IMAGE")].value}')` | no output |
| DaemonSet digest == ScaledJob supervisor digest | EKS | `diff <(kubectl -n adp-gateway-agents get daemonset chat-agent-image-prepull -o jsonpath='{.spec.template.spec.containers[?(@.name=="prepull")].image}') <(kubectl -n adp-gateway-agents get scaledjob chat-agent-worker -o jsonpath='{.spec.jobTargetRef.template.spec.containers[0].image}')` | no output |
| Pre-pull DaemonSet converged | EKS | `kubectl -n adp-gateway-agents get daemonset chat-agent-image-prepull` | DESIRED == UP-TO-DATE == READY (node count); a first turn on any node then shows `Pulled ... already present` rather than a multi-minute pull |
| Supervisor tolerates a pull (#8059) | EKS | `kubectl -n adp-gateway-agents logs job/<chat-agent-worker job> \| grep '"step":"sandbox_start"'` | `status=pulling` only on a node without the digest, then `status=started`; never `did not start before the supervisor deadline` |
| SSM digests == ScaledJob digests (#8086) | SSM / KEDA | `diff <(aws ssm get-parameter --name /adp/<env>/gateway/chat-authority-worker-images --query Parameter.Value --output text) <(kubectl -n adp-gateway-agents get scaledjob chat-agent-worker -o jsonpath='{.spec.jobTargetRef.template.spec.containers[0].image}' \| sed 's/.*@//'; echo) && for p in chat-sandbox-image-digests chat-crew-image-digests; do diff <(aws ssm get-parameter --name /adp/<env>/gateway/$p --query Parameter.Value --output text) <(kubectl -n adp-gateway-agents get scaledjob chat-agent-worker -o jsonpath='{.spec.jobTargetRef.template.spec.containers[0].env[?(@.name=="ADP_CHAT_SANDBOX_IMAGE")].value}' \| sed 's/.*@//'; echo); done` | no output (SSM holds exactly the digests the ScaledJob was pinned to; `verify-chat-rollout.sh` reports `DRIFT SSM chat-…` otherwise) |
| Gateway ConfigMap digests == SSM == ScaledJob (#8086) | EKS / SSM | `diff <(kubectl -n adp-gateway get configmap bedrockgateway-config -o jsonpath='{.data.ADP_CHAT_WORKER_IMAGE_DIGESTS}{"\n"}{.data.ADP_CHAT_SANDBOX_IMAGE_DIGESTS}{"\n"}{.data.ADP_CHAT_CREW_IMAGE_DIGESTS}{"\n"}') <(for p in chat-authority-worker-images chat-sandbox-image-digests chat-crew-image-digests; do aws ssm get-parameter --name /adp/<env>/gateway/$p --query Parameter.Value --output text; done)` | no output; the deploy log shows `[deploy-chat] published /adp/<env>/gateway/chat-…` (or `already holds`) and `[deploy-chat] bedrockgateway-config patched … and deployment/bedrockgateway restarted` (or `already carries the release digests`) |
| First turn after an image deploy is admitted (#8086 AC-03) | EKS | `kubectl -n adp-gateway logs deploy/bedrockgateway --since=30m \| grep -c 'chat creation image refused'` | `0` — no manual `aws ssm put-parameter` / ConfigMap patch after `chat-agent-deploy.yml` |

## Dev Box Module (optional)

Module: `modules/user-services/dev-box/`. Deployed by `deploy-all.sh --dev-box on` /
`--dev-box-only` → `modules/user-services/dev-box/scripts/deploy-dev-box.sh` (self-managed)
or `.github/workflows/dev-box-deploy.yml` (ADP-managed). An environment without the
module has none of the SSM parameters below; both gateway renderers then render `""` /
`"0"` and the gateway fails closed (admission 403, SSH access 503).

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| Deployment identity | SSM | `aws ssm get-parameter --name /adp/<env>/dev-box/deployment-id --query Parameter.Value --output text` | the `deployment_id` from `dev-box.tfvars` |
| Gateway admission keys | ConfigMap | `kubectl -n adp-gateway get configmap bedrockgateway-config -o jsonpath='{.data.BG_DEV_BOX_DEPLOYMENT_ID}'` | same deployment id |
| Controller | Deployment | `kubectl -n adp-gateway get deploy dev-box-controller` | Ready 1/1 |
| SSH grant role (#8049) | IAM / SSM | `aws ssm get-parameter --name /adp/<env>/dev-box/ssh-grant-role-arn --query Parameter.Value --output text` | `arn:aws:iam::<ACCOUNT>:role/adp-dev-box-ssh-grant-*` (a `" "` value means the foundation ran without `gateway_principal_arn`) |
| SSH grant in the gateway (#8049) | ConfigMap | `kubectl -n adp-gateway get configmap bedrockgateway-config -o jsonpath='{.data.BG_DEV_BOX_SSH_GRANT_ROLE_ARN}'` | same ARN as the SSM value; `adp devbox ssh <box>` then opens a shell on the guest as `ec2-user` (503 "Dev Box SSH access is not configured" when empty) |
| Gateway may assume the grant (#8049) | IAM | `aws iam get-role-policy --role-name <gateway IRSA role name> --policy-name adp-dev-box-assume-ssh-grant --query 'PolicyDocument.Statement[0].Resource'` | the grant role ARN |

## Tools Module (separate deploy and teardown)

The independent tools gateway uses the isolated Terraform key `tools/platform/<environment>.tfstate`
in the privately configured tools state bucket. The module script is `modules/tools/deploy.sh`; the
platform launcher passes `--tools-only` through to it. `--plan` checks prerequisites and prints stage
order without applying; the protected platform release uses `--infra --targets` for its saved Terraform
plan, readiness check and SSM outputs. The tool-Lambda, façade, authenticated `tools/list` and CI
bootstrap stages still stop until their reviewed implementation assets are available. Neither an open
PR nor a passing mocked stage check is live deployment evidence.

| Resource | Owner | Expected verification after authorized deploy |
|----------|-------|-----------------------------------------------|
| AgentCore gateway, targets, scoped invocation role and KMS key | Tools Terraform | Private state, `READY` gateway readback, and an unchanged Terraform plan on immediate rerun |
| Tools release values under `/adp/<env>/tools/` | Protected tools platform workflow | `release.json` agrees with SSM and the reviewed source revision |
| Façade Deployment and immutable ECR image | Façade owner (TLS-S10) | Pinned running digest, then authenticated gateway `tools/list` smoke; not yet verifiable from this checkout |
| Interceptor `live` Lambda alias | Protected interceptor workflow | Published version and code hash in release receipt; restore by an approved alias flip |

`platform/scripts/undeploy.sh --tools-only --dry-run` selects tools before its gateway/platform
providers. Set the original private `ADP_TOOLS_TFVARS_JSON` and, if different from the platform bucket,
`ADP_TOOLS_STATE_BUCKET`; never put their real values in public docs. The state-bound deletion plan
must list the exact façade identity and tools-owned Terraform resources before a real run. A real
`--tools-only` run requires typing the confirmed account ID even with `--yes`; it removes the
validated façade before gateway targets and KMS, retains both state backends, and rejects a changed
façade or SSM parameter. Do not use teardown as a rollback or infer live success without the
post-deploy reviewer's transcript.

## Dev Box Module (optional, self-managed stage)

Deployed by `modules/user-services/dev-box/scripts/deploy-dev-box.sh` (`deploy-all.sh
--dev-box on` / `--dev-box-only`) into `<env>/modules/dev-box{,-platform}/terraform.tfstate`;
see the phase reference for the seven steps. Validation lines for the Crew bootstrap
modes (#8055):

| Resource | Owner | Expected verification after authorized deploy |
|----------|-------|-----------------------------------------------|
| SSM `/adp/<env>/dev-box/crew-bootstrap` | Foundation Terraform (`infra/self_managed.tf`) | `aws ssm get-parameter --name /adp/<env>/dev-box/crew-bootstrap --query Parameter.Value --output text` → `image` (default) or `user-data`; the gateway ConfigMap key `BG_DEV_BOX_CREW_BOOTSTRAP` carries the same value after the next gateway render |
| Bundle inputs the stage consumed | Stage (`deploy-dev-box.sh --crew-bootstrap user-data`) | `base64 -d` of the launch template user data (below) carries `BUNDLE_SHA256=`/`PROFILE_REVISION=` equal to `--crew-bundle-sha`/`--profile-revision`, or, when omitted, to the bundle build's script-owned `/adp/<env>/dev-box/{crew-bundle-sha256,crew-bundle-revision}`; the foundation declares no copy of those keys |
| Launch template user data | Foundation Terraform (`infra/crew_bootstrap.tf`) | image mode: `describe-launch-template-versions ... --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData'` is empty. user-data mode: `base64 -d` of it is `infra/crew-bootstrap.sh.tftpl` rendered with the S3 paths, digests and revisions only — no credential, token or key (AC-04) |
| Guest inline policy `crew-bootstrap-inputs-read` and the 443 egress to the S3 prefix list | Foundation Terraform | present only in user-data mode; `runtime.py` (stage step 4) refuses either in image mode and refuses their absence in user-data mode |
| On-box receipt `/run/adp/crew-bootstrap.json` | First-boot script (root) | user-data mode: `"status": "ok"` with the pinned `profile_revision`, `source_revision`, `bundle_sha256`; the controller sets `crew_ready` only after reading it over SSM together with the installed profile (AC-03) |
| `GET /cloud-desktops/{id}` → `crew_bootstrap` | Gateway (`src/cloud_desktop/routes.py`) | equals the SSM value; absent configuration reads as `image` |

## Engineering Insights Module

Module: `modules/insights/`. Terraform state key:
`dev/modules/insights/terraform.tfstate` in the selected account's existing
state bucket. This row describes the reviewed deployment shape, **not** a live
inventory or evidence that the dev target has been provisioned.

| Resource | Owner | Expected verification after authorized deploy |
|----------|-------|-----------------------------------------------|
| Versioned Evidence Lake bucket `adp-<env>-insights-evidence` and KMS key | Insights Terraform | Encryption, private access and versioning readback; only `assurance/v1/…/insights/` receives emitter writes |
| Emitter and distiller ECR and IRSA roles | Insights Terraform | Repositories present; service-account trust bound to the `adp-insights` namespace and separate roles |
| Namespace, quota and network policies | Insights Terraform | `kubectl get namespace,resourcequota,networkpolicy -n adp-insights`; deny-all plus scoped gateway/S3/GitHub egress |
| Digest-pinned emitter/distiller Deployments | Owning Insights pipeline | Only created when real images exist; zero replicas until explicitly enabled, then D3 requires owned ready Pods |
| `adp-release-insights` and D3 receipt | Insights pipeline and deployment engine | A run-bound release artifact followed by `insights-health-verification` on a verified target; a plan alone is not a receipt |

The module is optional on a basic platform deploy. The authorized operator may
run `platform/scripts/deploy-all.sh --insights-only`; the `--ci` variant plans
without applying. `platform/scripts/undeploy.sh --insights-only` selects this
module before shared dependencies. **After the required account confirmation,
this command permanently deletes Insights evidence, including object versions
and delete markers, then removes the bucket.** The teardown orchestrator empties
the bucket explicitly; Terraform's `force_destroy = false` does not protect that
data. Satisfy retention requirements and create and verify a backup outside the
selected resources before confirming. Do not use teardown as a deployment retry
or rollback when evidence must be retained.
The platform administrator must review `modules/insights/deploy.sh
--bootstrap-ci`, apply its runner policy, register the two ADP machine service
accounts and configure the dev/prod protected environments before the first
pipeline run. See [the module boundary and live handoff](../engineering-insights/architecture.md).

## Superplane Domain App

Module: `modules/domain-apps/superplane/`
Deploy: `modules/domain-apps/superplane/deploy.sh` or the module's dispatch-only workflows. The basic `platform/scripts/deploy-all.sh` does not apply Superplane.
Undeploy: `platform/scripts/undeploy.sh` — **first** phase in `PHASE_ORDER` (`phase_superplane` in `undeploy-phases.sh`); `.github/workflows/undeploy.yml` Phase 1/6
Legacy `platform/scripts/deploy-all.sh --destroy` also calls `phase_superplane` first and stops if it fails.

Deploys **last** and is destroyed **first**: a domain app sits on top of the platform, the
gateway and the agent runtime, so teardown must remove it before its dependencies go.

Superplane has its own installation path and state. Cyber's sandbox
has its own deployment script and state; the base `deploy-all.sh` does not run it.
The hosted Cyber integration has its own Terraform root and state; webhook
Terraform reads its worker outputs only with `enabled_domain_integrations = ["cyber"]`.
Cyber and Superplane image build jobs live in their app Terraform states and
are omitted from a basic platform apply.

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| Feature gate (off by default) | Gateway API | `curl -s https://<cf-domain>/api/features -H "Authorization: Bearer <token>" \| python3 -c 'import json,sys; print(json.load(sys.stdin)["features"]["superplane"])'` | `False` unless `FEATURE_SUPERPLANE_ENABLED=true` |
| Undeploy phase registered | Shell | `bash -c 'source platform/scripts/undeploy-phases.sh && declare -F phase_superplane'` | `phase_superplane` listed |
| Undeploy phase ordering | Shell | `grep -n 'PHASE_ORDER=' platform/scripts/undeploy.sh` | `superplane` first, before `agent_context` |
| Terraform state | S3 | `aws s3api head-object --bucket adp-terraform-state-<account> --key <env>/modules/superplane/terraform.tfstate` | Object exists only after module deployment |

**While the gate is off** there are no AWS resources to validate — the module is a skeleton
plus a default-off flag, so an operator running these checks on a default deployment should
expect `False`, a registered phase and **no** Terraform state. Terraform, the domain
workload rollout and pinned images arrive with later units.

## Dev Box Module (optional)

Module: `modules/user-services/dev-box/`
Deploy: `platform/scripts/deploy-all.sh --dev-box on` (Step 12) or `modules/user-services/dev-box/scripts/deploy-dev-box.sh --env <env>`; Crew image release: `modules/user-services/dev-box/scripts/release-crew-image.sh --env <env> --crew-model-id <id> --apply`
State keys: `<env>/modules/dev-box-platform/terraform.tfstate`, `<env>/modules/dev-box/terraform.tfstate`

An environment without `environments/<env>/modules/dev-box.tfvars` has none of this. The Crew image
pipeline (#8054) adds the bundle build and the release pin to the module's AMI build.

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| Image CodeBuild project | CodeBuild | `aws ssm get-parameter --name /adp/<env>/dev-box/image-builder-project --query Parameter.Value --output text` | `adp-<env>-dev-box-image` |
| Crew bundle inputs | S3 | `aws s3 ls s3://adp-terraform-state-<account>/dev-box/image-inputs/$(aws ssm get-parameter --name /adp/<env>/dev-box/crew-bundle-sha256 --query Parameter.Value --output text)/` | `crew.tar.gz` and `profile.json` |
| Crew bundle revision | SSM | `aws ssm get-parameter --name /adp/<env>/dev-box/crew-bundle-revision --query Parameter.Value --output text` | `crew-<yyyymmdd>-<sha12>` (the profile revision of the last bundle build) |
| Published AMI | SSM | `aws ssm get-parameters --names /adp/<env>/dev-box/image-id /adp/<env>/dev-box/image-revision /adp/<env>/dev-box/profile-revision /adp/<env>/dev-box/image-source-revision --query 'Parameters[].[Name,Value]' --output table` | `image-id` an `ami-…`; `image-revision` ends with `-crew-<profile-revision>`; `image-source-revision` = the released commit |
| Crew model pin | SSM | `aws ssm get-parameter --name /adp/<env>/dev-box/crew-model-id --query Parameter.Value --output text` | the approved model id (never `" "` once a Crew image is released); equals `crew_model_id` in `dev-box.tfvars` |
| AMI tags match the pin | EC2 | `aws ec2 describe-images --image-ids $(aws ssm get-parameter --name /adp/<env>/dev-box/image-id --query Parameter.Value --output text) --query 'Images[0].Tags[?starts_with(Key, \`adp:\`)]' --output table` | `adp:profile-revision` = SSM `profile-revision`, `adp:image-revision` = SSM `image-revision`, `adp:source-revision` = SSM `image-source-revision`, `adp:component` = `dev-box` |
| Release pin journal | Local | `tail -1 .adp-dev-box/<env>/release/crew-image-journal.jsonl` | JSON line with `action: crew-image-release-pin` and the four SSM values |
| Foundation on the Crew image | EC2 | `aws ec2 describe-launch-template-versions --launch-template-id $(aws ssm get-parameter --name /adp/<env>/dev-box/launch-template-id --query Parameter.Value --output text) --versions '$Latest' --query 'LaunchTemplateVersions[0].LaunchTemplateData.ImageId'` | = SSM `image-id` (after `deploy-dev-box.sh --skip-image`) |
| Controller | EKS | `kubectl -n adp-gateway get deploy dev-box-controller` | Ready 1/1 |
| Crew ready on a box | Gateway API | `python3 modules/user-services/dev-box/scripts/verify-dev-box.py --env <env> --require-crew` | exit 0 (`crew_ready: true`) |

The SSM keys `/adp/<env>/dev-box/{image-id,image-revision,profile-revision,crew-model-id}` are the
release pin `release-crew-image.sh` writes and `deploy-dev-box.sh` reads; the ADP-managed workflow
(`.github/workflows/dev-box-crew-image.yml`) writes the same keys and reports them as
`ADP_DEV_BOX_IMAGE_ID`, `ADP_DEV_BOX_IMAGE_REVISION`, `ADP_DEV_BOX_CREW_PROFILE_REVISION`,
`ADP_DEV_BOX_CREW_MODEL_ID` for `dev-box-deploy.yml`.

## Deployment State File

Location: `s3://<state-bucket>/deploy/<environment>/state.json`

The deploy-all.sh script and the agent both read/write this file to track progress:

```json
{
  "version": 1,
  "environment": "dev",
  "account_id": "123456789012",
  "aws_region": "us-east-1",
  "github_org": "acme-corp",
  "started_at": "2026-04-16T14:30:00Z",
  "modules": ["gateway", "agent-factory"],
  "phases": {
    "bootstrap":         {"status": "complete", "completed_at": "..."},
    "platform_infra":    {"status": "complete", "codebuild_id": "...", "completed_at": "..."},
    "gateway_infra":     {"status": "complete", "codebuild_id": "...", "completed_at": "..."},
    "gateway_backend":   {"status": "complete", "codebuild_id": "...", "completed_at": "..."},
    "gateway_frontend":  {"status": "complete", "codebuild_id": "...", "completed_at": "..."},
    "agent_factory":     {"status": "running",  "codebuild_id": "adp-dev-agent-factory:abc123"},
    "agent_gateway":     {"status": "pending"},
    "github_apps":       {"status": "pending"},
    "verification":      {"status": "pending"}
  },
  "outputs": {
    "eks_cluster": "adp-dev-eks-cluster",
    "cloudfront_domain": "gateway-7.example.com",
    "cognito_user_pool_id": "us-east-1_abc123",
    "ecr_registry": "123456789012.dkr.ecr.us-east-1.amazonaws.com",
    "gateway_ws_endpoint": "wss://abc123.gateway-14.example.com/prod"
  },
  "validation": {
    "eks_cluster": "ACTIVE",
    "gateway_pods": "2/2 Running",
    "gateway_health": "200 OK",
    "frontend": "200 OK",
    "rds": "available",
    "arc_controller": "1/1 Running",
    "github_runners": "registered"
  }
}
```

### Status values
- `pending` — not started yet
- `running` — in progress (check `codebuild_id` for build status)
- `complete` — finished successfully
- `failed` — failed (check `error` field)
- `skipped` — user chose to skip this module

### Agent resume logic
1. Read state file from S3
2. Find first phase that is not `complete` or `skipped`
3. If `running` with a `codebuild_id`, poll that build
4. If `failed`, retry the phase
5. If `pending`, start the phase

## Production Deploys via GitHub Actions vs. Local Cold-Start via deploy-all.sh

### CI-first flow (recommended for production)

Infrastructure changes are deployed via GitHub Actions workflows that run on ARC self-hosted runners with IRSA credentials. Each Terraform module has a plan + apply workflow pair:

| Module | Plan Workflow | Apply Workflow | Path Filter |
|--------|--------------|----------------|-------------|
| Platform | `platform-infra-plan.yml` | `platform-infra-apply.yml` | `platform/infra/**` |
| Gateway | `gateway-infra-plan.yml` | `gateway-infra-apply.yml` | `modules/gateway/infra/**` |
| Agent Factory | `agent-factory-infra-plan.yml` | `agent-factory-infra-apply.yml` | `modules/agent-factory/infra/**` |
| Agent Context | `agent-context-infra-plan.yml` | `agent-context-infra-apply.yml` | `modules/agent-context/terraform/**` |

**How it works:**
1. Open a PR that touches a module's infra path. The plan workflow runs and posts a comment on the PR with the plan output.
2. Review the plan. Merge the PR. **Merging does NOT auto-apply.**
3. When ready to deploy, the operator triggers the apply workflow manually: Actions → `<module> Infra Apply` → Run workflow → main.
   Gateway Infra Apply requires `reviewed_source_sha`: the exact reviewed main
   commit (40 lowercase hex characters). It must match the run and checkout;
   if main advances before dispatch, review the new commit and dispatch again.
4. Apply is gated by `environment: production` (requires reviewer approval in GitHub) AND by the `destructive-apply-approved` label gate: if resources would be destroyed, the source PR must have the label.

**Why manual apply:** separates "reviewed" from "deployed" — prevents Friday-evening surprise applies on merge, lets operators batch multiple merged PRs into one apply, and matches the project's "carefully consider reversibility and blast radius" rule. For routine non-destructive changes this is one extra click; for anything risky, it's the right default.

**Safety guardrails:**
- Per-module concurrency control (`concurrency.group: tf-apply-<module>`) prevents state lock races.
- Destroy-safety gate requires explicit label approval for destructive changes.
- Agent-context has a GraphRAG cost guard: `graphrag_enabled=true` requires the `CONFIRM_GRAPHRAG_COST=yes` repo variable.
- Plan comments are updated in-place (one per module per PR).

**Prerequisites:**
- `production` environment created in repo Settings -> Environments with required reviewers.
- ARC runner controller deployed and `arc-runner-org` runner label available.
- `destructive-apply-approved` label created in the repo.

### Local cold-start via deploy-all.sh

For bootstrapping a new AWS account or running everything locally (no CI):

```bash
# Full deploy (CodeBuild-based)
./platform/scripts/deploy-all.sh

# Local mode (needs Terraform, Docker, Node, kubectl locally)
./platform/scripts/deploy-all.sh --local
```

### CI validation mode

After CI owns the infra path, use `--ci` to verify all modules have been applied:

```bash
./platform/scripts/deploy-all.sh --ci
```

This checks Terraform state files in S3 for each module and validates the EKS cluster is active. If any module is missing, it prints the URL of the GitHub Actions workflow to run. It does NOT re-apply anything.
