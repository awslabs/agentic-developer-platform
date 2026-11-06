# GitLab CI runners on EKS — architecture and implementation contract

> **Current executor amendment (owner approved 2026-11-01):**
> [Docker Autoscaler with disposable EC2 jobs](q1-enforcement-contract.md) is
> authoritative for S2, S3 and their evaluations. Managers stay on EKS; jobs
> use the same VPC, private subnets and dedicated security groups. Earlier
> Kubernetes job, admission and Auto Mode pool details below are historical
> design context where superseded by that amendment. S1 is unchanged.

**Status: design ready for review; isolation-dependent implementation is conditional.**
The existing GitLab instance can gain tenant-group CI without acquiring a dependency
on the runner stack. The proposed deployment keeps a small polling manager per
group; job pods and their dedicated nodes disappear when idle. It does not grant
jobs platform credentials, privileged containers, Docker-in-Docker, or ADP access.

There is one unresolved feasibility boundary, not a passed security check:
**AWS documents unreliable network-policy enforcement for standalone pods, and
the GitLab Kubernetes executor creates precisely those pods.** The module and
platform owners must resolve [Q1](#q1-pod-policy-qualification) before implementing
the isolation-dependent launch path. S1 registration/CA work, offline contracts,
and workflow scaffolding can proceed. A ready *design PR* does not mean the epic
is deployed, accepted, or that this qualification can be skipped.

Scope: [epic #9283](https://github.com/aws-e/adp/issues/9283), its
[authoring amendment](https://github.com/aws-e/adp/issues/9283#issuecomment-6491359661),
and native children [#9284](https://github.com/aws-e/adp/issues/9284),
[#9276](https://github.com/aws-e/adp/issues/9276),
[#9285](https://github.com/aws-e/adp/issues/9285). Repository baseline:
`a3ffb462b29e6b6c88e32b0619ae04a3a2b5df78`, inspected 2026-10-29.
All target identities and addresses are private operator inputs. This document
contains no approved deployment target. No AWS access or live checks were used.

## 1. Evidence and corrections to the sketch

| Observed source at the baseline | Consequence / selected design |
|---|---|
| [Instance URL](../../../modules/source-control/gitlab/infra/ssm.tf#L43) chooses the CloudFront `/gitlab` URL or an HTTP internal URL | Do not hand that value directly to runners. Add a private HTTPS runner URL; preserve existing consumers of `/gitlab/url`. |
| [ALB](../../../modules/source-control/gitlab/infra/alb.tf#L70), [private DNS](../../../modules/source-control/gitlab/infra/dns.tf#L10), [security groups](../../../modules/source-control/gitlab/infra/security_groups.tf#L12) | Private HTTPS can use the certificate's DNS name, not the ALB-generated name. Retain the installation path `/gitlab` where configured. CA trust alone does not fix hostname mismatch. |
| [GitLab relative URL](../../../modules/source-control/gitlab/infra/user_data.sh#L97) and [package default](../../../modules/source-control/gitlab/infra/variables.tf#L21) | Source default is `19.5.1-ce.0`, not the older version in comments. Neither source nor a past workflow establishes the installed version or redirect behavior. |
| [ARC secret data sources and Kubernetes Secret](../../../modules/agent-factory/infra/modules/arc-runner/main.tf#L45) | Copying this pattern persists credentials in Terraform state. Reuse the Helm/namespace shape, **not** secret-value data sources or ARC authority. |
| [ARC minimum](../../../modules/agent-factory/infra/modules/arc-runner/main.tf#L133) and [deploy-pool module](../../../modules/agent-factory/infra/modules/deploy-runner-pool/main.tf#L1) | GitHub CI is a separate execution surface. Do not reuse its service account, namespaces, warm minimum or deployment permissions. |
| [Platform remote-state consumption](../../../modules/agent-factory/infra/main.tf#L54), [outputs](../../../platform/infra/outputs.tf#L16) | Reuse EKS/VPC identifiers read-only. Reading a remote-state object exposes the whole object to the deploy identity; managers/jobs must not receive that access. |
| [Auto Mode compute](../../../platform/infra/modules/eks/main.tf#L205), [existing NodePool](../../../platform/infra/nodepool-gvisor.tf#L39) | Use `karpenter.sh/v1` NodePool with `eks.amazonaws.com/v1` NodeClass, not `EC2NodeClass` or another Karpenter installation. |
| [Policy enablement](../../../platform/infra/modules/eks/main.tf#L1028), [dev configuration](../../../environments/dev/platform.tfvars#L36), [runbook](../../runbooks/network-policy-enforcement.md#section-3--ordered-enablement-procedure) | Source enables the controller for dev; enforcement still needs live evidence. Do not silently enable/disable a cluster-wide controller in the runners stack. |
| [Isolated administrator](../../../modules/gateway/src/gitlab/admin_client.py#L15), [admin secret policy](../../../modules/source-control/gitlab/infra/portal_sso.tf#L40) | Reuse safe error handling, no redirects, and explicit operator authorization. The ordinary gateway and agent roles do not become runner registrars. |
| [Tenant parent and Product groups](../../../modules/gateway/src/gitlab/home_worker.py#L23) | Canonical `tenant_id` hashes identify tenant-parent groups; Product subgroups hold human memberships. Tags, group display names and user-provided paths are not authorization. |
| [Instance Terraform](../../../modules/source-control/gitlab/infra/main.tf#L28), [optional reconciler IAM](../../../modules/source-control/gitlab/infra/portal_sso.tf#L75) | The instance already has optional OIDC/reconciler inputs. “EKS-independent” means no new Kubernetes/Helm provider, runner-state reference or runner prerequisite; do not claim it never mentions EKS. |

The repository-wide inventory and deployment coverage are in [audit.md](audit.md),
with every selected tracked artifact in [inventory.tsv](inventory.tsv). This is
not a representative sample. Source inspection does not close the open security
findings [#8847](https://github.com/aws-e/adp/issues/8847) (registration/SSO),
[#4995](https://github.com/aws-e/adp/issues/4995) (CI secret logging), or
[#2312](https://github.com/aws-e/adp/issues/2312) (historical scanner triage).

## 2. Stack boundaries and public contracts

```text
Existing instance state                 Existing platform state
  GitLab / private ALB / DNS / CA         EKS / VPC / OIDC / endpoints
  isolated administrator                    |
          | S1 registration + CA export       | identifiers only
          v                                  v
SSM discovery + Secrets Manager  --->  Separate runners state (S2)
                                      manager namespace per group
                                      job namespace per group
                                      group cache + dedicated job nodes
                                              |
                                      S3 pre-signed cache requests
                                      CI_JOB_TOKEN -> GitLab
```

No reference flows back from the runners state into the instance or platform
state. S1 adds discovery metadata and secret containers/permissions. After S1's
baseline is applied, the instance plan must be identical with runners absent,
present, and destroyed. Comparing pre-S1 and post-S1 plans would incorrectly
expect no change for the authorized exports.

### Inputs and outputs

All paths in this table are **proposed contracts**, except the platform outputs
and the existing public URL. Environment values come from private configuration.

| Input | Producer / rule | Consumer |
|---|---|---|
| Platform state location | Explicit backend bucket/key/region; default convention remains `<env>/platform/terraform.tfstate` | Deploy identity only; EKS name, endpoint, CA, OIDC issuer/provider, VPC and subnet IDs |
| `/adp/<env>/gitlab/url` | Existing public URL; retain unchanged | Existing clients; registrar uses it only as canonical instance identity |
| `/adp/<env>/gitlab/runner-url` | S1, HTTPS private certificate hostname plus existing rooted path | Registrar, manager, clone URL and live verifier; reject HTTP, userinfo, query, fragment and redirects outside the approved origin |
| `/adp/<env>/gitlab/ca-chain-secret-arn` | S1 SSM String containing ARN only | Deploy identity discovers reference; manager reads exact secret at runtime |
| `adp/<env>/gitlab/ca-chain` | Secrets Manager PEM trust bundle, public CA certificates only; never a private key | Registrar and group managers; no Terraform secret-version resource/data source |
| `adp/<env>/gitlab-runner/<group-id>` | S1, numeric immutable GitLab group ID; JSON schema below | Exactly that group's manager IRSA role |
| `groups` | Private map keyed by numeric group ID: canonical tenant ID, expected path/marker, tags, protected policy, token ARN, resource profile | Registration checks ownership; Terraform emits one isolated manager/job namespace pair per group |
| Image and chart lock | Reviewed chart archive SHA-256, version, manager/helper/bootstrap/probe image digests and architecture | Offline render, admission allowlist and apply preflight; missing pins reject |
| Private network discovery | Cluster DNS address, service/VPC CIDRs, approved endpoint names and target identities | Operator-reviewed policy render and negative probes; never copied to public docs |

Runner stack backend: `<env>/modules/gitlab-runners/terraform.tfstate`, with its
own backend configuration, encryption, locking and deploy role permissions.
Outputs are non-secret: namespace names, NodePool/NodeClass names, role ARNs,
runner IDs, secret ARNs/version IDs, chart/image lock revision and cache bucket
reference. Do not output URLs/identities into public evidence or secret values
anywhere. There is no new Postgres/DynamoDB data model or gateway API.

## 3. Group registration, rotation and CA export (S1)

### Authoritative identity

The registrar is an operator command, not a tenant-facing HTTP endpoint and not
a job invoked by untrusted pipeline code. It uses the existing isolated admin
secret only through a role admitted by its resource policy. The command takes
`--environment`, `--region`, a protected group configuration file and exactly one
of `--create`, `--rotate`, `--revoke`, `--reconcile`. It returns only safe status,
operation ID, runner ID and secret version ID; success is exit 0, validation or
ownership rejection 2, retryable provider/storage failure 3. No response bodies,
tracebacks containing credentials, shell tracing or token arguments.

Resolve the numeric group and expected canonical instance. For ADP-managed
tenants, verify `tenant-<sha256(tenant_id)[:24]>`, the `ADP tenant <hash>` marker,
parent position and service-owned memberships using the existing reconciler
convention. Reject group moves, tenant mismatches, ambiguous paths, missing
groups and attempts to register at instance scope. An operator path change
requires deliberate reconciliation against the same numeric ID; it must not
silently allocate a new tenant binding. Do not grant human tenant-parent
membership. The existing `tenant_id` is opaque; do not derive it from GitLab login.

Create via `POST /api/v4/user/runners` with `runner_type=group_type`, `group_id`,
`run_untagged=false`, `locked=true`, nonempty immutable group tag, explicit
`access_level` (`ref_protected` when selected), `paused=true`, and a description
containing a run-owned operation marker. Read back runner type, group association,
tags, locked/access policy and pause status. `locked=true` is a required readback,
not the cross-tenant security mechanism: GitLab group association provides that.
Do not fall back to deprecated registration tokens if the API rejects a field.

The secret value is JSON with versioned fields:
`schema_version=1`, `tenant_id`, `group_id`, `runner_id`, `instance_url`,
`runner_url`, `tag_list`, `access_level`, `token`, `token_expires_at` (nullable only
if provider returns no expiry), `operation_id`, `generation`. The manager checks
these against its non-secret configuration before use. Renames do not rename
the secret. The token is the new runner authentication token, never an admin PAT
or `CI_JOB_TOKEN`. No secret value is passed through Terraform or GitHub outputs.

### Crash recovery and rotation

Use a per-group, non-secret SSM operation record
`/adp/<env>/gitlab-runner-operations/<group-id>` containing operation/generation,
old/new runner IDs, intended secret version ID and phase. Serialize workflow and
operator runs by the same group; refuse an unfinished operation unless resuming
its operation ID. SSM alone is not a compare-and-swap lock: the manual runbook
requires exclusive operator ownership, and the workflow concurrency group
serializes CI. Concurrent operator invocation is an explicit refusal/precondition,
not an asserted distributed lock.

1. Persist intent, create a **paused** candidate, verify identity, then write its
   token directly from process memory to a Secrets Manager pending version using
   the operation UUID as `ClientRequestToken`.
2. If the response was lost, list by exact operation marker/group and reconcile
   before retrying. A lost plaintext token is not recoverable: delete the paused
   orphan, confirm absence, and create another paused candidate. A failed
   Secrets Manager write never leaves an active orphan.
3. Initial activation belongs to S3 after stack/policy qualification. Rotation
   pauses the old runner, drains bounded in-flight work, promotes the new secret
   version, restarts the manager and verifies its runner ID before unpausing the
   new runner. Delete the old registration and confirm token rejection.
4. Revocation pauses immediately, stops polling, deletes the runner registration,
   verifies absence, then schedules secret deletion. Existing jobs may continue
   with already-issued job tokens; cancellation and namespace cleanup are
   required for emergency revocation. Revoking a runner is not retroactive
   revocation of all job credentials.
5. A scheduled trusted S3 reconciliation job resumes unfinished records, cleans
   paused orphans and reports expiry/rotation deadlines. Failed cleanup remains
   durable and is retried on the next bounded run; never erase a record merely
   because reporting failed. Retire only after authoritative absence/readback.

Do not use an in-place reset as normal rotation: it invalidates the only token
before durable storage succeeds. Disable uncontrolled manager-local token
rotation for this registration cohort or qualify an explicit write-back design;
if the server enforces automatic rotation that cannot be disabled/scoped, S1
activation blocks until token persistence is resolved. Never grant managers
admin access or unrestricted secret writes to work around it. Expiry monitoring
must include the existing administrator token's independent expiry.

### CA and private route

Use the configured ALB certificate ARN and `GetCertificate` (not private-key
export) to obtain the leaf/chain. Validate hostname, validity dates and chain
against the operator-approved root. Imported ACM chains may omit the root; an
unrelated self-signed leaf cannot be promoted to trusted root by discovery alone.
For a deliberately self-signed installation, require the owner-approved trust
fingerprint in private configuration. Store only the validated PEM trust bundle
in Secrets Manager, outside Terraform state, and publish its ARN plus the private
runner URL through SSM. CA replacement uses overlap old+new roots, reload and
positive/negative TLS checks before retiring the old trust anchor.

The internal name must support API, repository fetch, trace, artifact and cache
paths without redirecting to CloudFront. Set runner `url` and `clone_url` to the
private rooted URL, and propagate the CA to the helper (`CI_SERVER_TLS_CA_FILE`)
and approved job images. Container images must install/use the job CA without
disabling verification; manager trust alone is insufficient. GitLab's public
canonical URL stays unchanged. If GitLab emits required cross-origin URLs that
cannot be handled by supported runner configuration, stop and amend the routing
design; do not add CloudFront/gateway egress or a credential-bearing redirect.

## 4. Managers, secrets, IAM and cache (S2)

### Runtime shape and supply chain

One pinned GitLab Runner Helm release and one manager replica per tenant group
in `gitlab-runner-<group-id>`; jobs run in separate `gitlab-jobs-<group-id>`.
Managers use ordinary trusted platform capacity, never the job NodePool. This
keeps job nodes at zero while the manager polls. No KEDA manager scale-to-zero
is proposed: without a polling manager the native executor cannot discover work.

The audited upstream candidate is chart **0.94.2**, Runner **19.5.2**, archive
SHA-256 `6a63ce370db021252f4f6f0414a4ff1fb09f8105d683dd8539ee670263944f21`.
The archive was downloaded and hashed; it was not deployed. Pin manager and
matching helper images by digest separately. Their platform-specific digests
and compatibility with the actual installed server remain an S2 release-lock
checkpoint, not fabricated values. Verify the archive before Helm consumes a
local file; a version string alone is not content integrity. No chart download
or image lookup using `latest` during apply.

Publish the bootstrap through the existing per-project
[CodeBuild pattern](../../../platform/infra/modules/codebuild/main.tf#L58) and
[source-isolated build action](../../../.github/actions/codebuild-run/action.yml#L1),
not through GitLab itself. S3 adds an optional, default-off dedicated build
project with an exact repository/source-prefix role; a reviewed platform apply
precedes its first build. This is a shared build dependency, not a runner-state
or instance-state coupling. Retain the image repository across runner teardown.

The chart's `deploymentInitContainers`, `volumes` and `volumeMounts` support a
small, digest-pinned credential loader. It fetches the exact two Secrets Manager
objects with the group's IRSA identity, validates the schema and TLS, and writes
`/secrets/runner-token` plus CA files to a memory-backed volume readable only by
the manager UID. The chart already uses memory-backed runner configuration
volumes. Keep `runnerToken`, `runnerRegistrationToken`, `runners.secret` and
`certsSecretName` unset: the latter requires a persistent Kubernetes Secret.
Use `tls-ca-file` and mounted certificates instead; this deliberately replaces
the sketch's secret-copy mechanism while preserving verified TLS.

Do not expose the loader volume, manager environment, config or IRSA token to
job pods. The chart startup registration path consumes `/secrets/runner-token`;
test the rendered entrypoint with synthetic credentials for logging leaks. If
the pinned chart logs credential material, suppress/sanitize that subprocess
output with an explicitly reviewed wrapper before use, not GitHub masking alone.
Readiness requires valid credentials and successful private HTTPS verification;
a wrong/missing CA makes loader readiness fail and `helm_release` wait/atomic
apply fail. Terraform cannot prove a wrong CA by inspecting secret metadata.

Secret source of truth remains Secrets Manager. Necessary runtime copies exist
only in memory/tmpfs and per-job transient files; “only in Secrets Manager” cannot
literally exclude the memory of the process authenticating with that token.
No long-lived copies in Kubernetes Secrets, Helm release payloads, state, build
layers, caches, logs, artifacts or issue bodies. Project-supplied secret handling
is a separate risk; runner isolation does not make arbitrary CI scripts safe to
print their own secrets. Debug trace is forbidden in the managed profile.

### Authority matrix

| Principal | Allowed | Explicitly not allowed |
|---|---|---|
| S1 registrar | Exact admin, group token and CA secret operations; exact operation/discovery parameters; selected certificate read | Routine pipeline use; arbitrary group or environment; platform administration |
| S2 deploy identity | Own state, read platform state/discovery metadata, own IAM/cache/NodePool/namespaces; reviewed namespace RBAC provisioning | Reading token/CA values as Terraform inputs; changing instance resources; unbounded `iam:PassRole` |
| Per-group manager IRSA | `GetSecretValue` on its token and CA; regional STS; presign S3 cache for its own prefix | Other group/admin secrets, secret writes, RDS, ADP API, deployment roles |
| Manager Kubernetes SA | Job-namespace pods create/get/list/watch/delete and attach/exec/log as pinned executor requires; ephemeral executor Secrets/services only if required and tested | Cluster-admin, other groups, namespace/RBAC/policy edits; any manager-namespace Secret access |
| Job SA | `automountServiceAccountToken=false`; no IRSA annotation and no RBAC bindings | Kubernetes API, STS, Secrets Manager, instance metadata and node role credentials |
| Dedicated job-node role | EKS bootstrap and ECR pull of approved repositories | Admin/runner secrets, application roles, unrestricted repository pull or deployment |

IRSA trust uses the cluster OIDC provider, exact namespace/SA `sub` and
`aud=sts.amazonaws.com`; strip the issuer's `https://` exactly once. Separate
roles per group, not one wildcard role. New resource-capable IAM actions use exact
ARNs/prefixes and conditions; `ecr:GetAuthorizationToken` has no resource-level
ARN support and requires a documented `Resource="*"` exception, regional condition
and separate repository-scoped pull statements. Do not copy the shared node
role's broader managed policies. Node role creation/access entry and any required
`PassRole` approval belong to S2's bounded deploy permissions, not jobs.

Use one private SSE-S3 encrypted cache bucket owned by this stack, public access
blocked, TLS-only bucket policy and seven-day expiration/one-day incomplete
multipart abort. Set per-group immutable prefix and runner `Shared=false`;
retain GitLab's project and protected/unprotected cache separation. Manager
presigns exact object operations; helpers get short-lived URLs, not AWS keys or
IRSA credentials. Qualify the pinned S3 adapter in this mode; no helper-side
STS/role assumption. Cross-group cache object access must fail independently of
network isolation. Do not reuse GitLab backups or ARC artifact buckets. Enforce
a 1 GiB archive limit and test both upload/download behavior; if a provider cannot
enforce upload size server-side, document it as an operational bound rather than
claiming a hard security limit. No cost estimate was measured.

ECR image pulls are performed by kubelet using the **node** identity, not manager
IRSA. Mirror approved build/helper/service/probe images into scoped ECR
repositories and lock by digest. No Docker-in-Docker, registry password stored
in a long-lived pull Secret, or job-side ECR push capability is included.

## 5. Job confinement and bounded capacity

### Admission, namespaces and resources

Both namespace classes enforce Pod Security **restricted**, pinned to the
qualified Kubernetes minor version. All build, helper, init and service
containers: `privileged=false`, `allowPrivilegeEscalation=false`, non-root UID,
drop `ALL` capabilities, seccomp `RuntimeDefault`. Deny host PID/IPC/network,
hostPath, host ports, privileged ephemeral containers, added capabilities and
unsafe volume types. PSA is necessary but does not by itself restrict service
accounts, node selectors or all writable API fields.

Add a namespace-scoped binding of a ValidatingAdmissionPolicy for job pods and
ephemeral-container updates: exact job SA, no token projections/IRSA annotations,
only approved volume types, locked job-node selector/toleration, required owner
contract once Q1 is resolved, bounded resources and allowed image digests for
every container. Users cannot edit namespace labels or this policy. Manager RBAC
is confined to its paired job namespace. Deny node/namespace/SA/bearer-token,
label/annotation, resource and toleration overwrite mechanisms in Runner config;
do not expose arbitrary `pod_spec` or TOML via Terraform input. Empty GitLab
image/service allowlists may mean “allow everything”: render an explicit allowlist
and test denial, never assume an empty list is deny-all.

Default profile per group: manager 100m CPU/128Mi request, 500m/512Mi limit;
`concurrent=2`, per-runner `limit=2`, `request_concurrency=1`. Build container
500m/512Mi request, 2 CPU/2Gi limit; helper 100m/128Mi request, 500m/512Mi limit;
at most one approved service with 250m/256Mi request, 500m/512Mi limit. Job timeout
30 minutes, pod active deadline job timeout plus 60 seconds, cleanup grace
60 seconds. Job-namespace ResourceQuota: 4 pods, 8 requested CPUs/8Gi requested
memory, 12 limited CPUs/12Gi limited memory, 40Gi ephemeral storage; LimitRange
requires requests and limits, including scratch storage. These are initial
operator-reviewed ceilings, not measured workload sizing. Hard resource bounds
and protected profiles cannot be loosened by pipeline variables.

### Network policy

Start with ingress+egress deny-all selecting **all** pods in each job namespace,
including unlabeled pods. No inbound service exposure is needed for the initial
profile; sidecars communicate over loopback. Managers have a different policy:
GitLab private HTTPS, cluster DNS/API, regional STS, exact Secrets Manager endpoint
and cache destination only. Never allow a whole VPC CIDR or `0.0.0.0/0:443` to
solve dynamic endpoint addressing.

Standard Kubernetes NetworkPolicy is IP/port based, not an FQDN firewall. The
selected Auto Mode direction adds `networking.k8s.aws/v1alpha1`
**ApplicationNetworkPolicy** for exact DNS endpoint names, alongside the ordinary
default-deny policy, with different resource names to avoid AWS policy-endpoint
collisions. This vendor extension must exist and be qualified on the target; the
baseline has no declaration proving that API is served there. Do not install a
second CNI or replace the existing cluster controller as a hidden dependency.

| Job egress destination | Limit / purpose |
|---|---|
| Cluster DNS | Discovered cluster DNS address, UDP/TCP 53 only; no arbitrary DNS/DoH endpoints |
| Private GitLab certificate hostname | TCP 443; API, clone, logs and artifacts at the rooted path; CA+hostname verified |
| Exact cache bucket regional S3 hostname | TCP 443; pre-signed per-project cache requests, IAM/bucket policy still applies |
| Approved regional ECR/owned registry and required ECR layer S3 endpoint | TCP 443 only where needed; node pull path qualified separately, no broad `*.amazonaws.com` |
| Anything else | Denied, including gateway, Dev Box controller, RDS/Redis, other job namespaces, cluster API, STS, Secrets Manager, metadata IPv4/IPv6 and arbitrary internet/proxy |

No general package-internet egress initially: use prebuilt images. Any later
approved egress change names an exact purpose/destination and repeats negative
acceptance. FQDN policy tracks resolved IPs, not HTTP requests; shared destination
IPs, DNS rebinding and SNI/Host confusion remain test cases. Use dedicated private
GitLab endpoint addressing and bucket/repository IAM boundaries, not DNS names
as authorization. A DNS allowlist is not comprehensive protection against data
exfiltration through the intentionally allowed GitLab/cache service.

### Q1: pod-policy qualification

AWS's [network-policy considerations](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html)
explicitly warn that standalone pods without `metadata.ownerReferences` may not
be reliably covered. Inspection of the upstream Kubernetes executor shows job
Pod ObjectMeta populated with name, namespace, labels and annotations, without
an owner reference; helper Secrets/services are owned by the job pod, not the
reverse. A successful Deployment-controlled probe cannot settle this mismatch.

**Owner: GitLab module owner with platform owner. First S2 checkpoint:** pin the
actual runner, node image and policy API; obtain a supported enforcement contract
for the exact runner-created pod lifecycle, including the window before policy
attachment. Require DefaultDeny on the new NodeClass, repeated cold-start probes,
policy-endpoint attachment and denial evidence for actual runner pods. Confirm
controller loss/restart fails closed. A few passing probes alone do not override
the vendor's unsupported/reliability warning.

If that contract cannot be established, stop S2's isolation-dependent work and
return an explicit design amendment choosing a supported executor/pod-controller
integration or isolation platform. Do not add fake owner references, pretend a
pod-spec patch can modify ObjectMeta, silently change CNI/node provisioning, or
weaken the acceptance criteria. No such extension is approved by this document.
This is a **known design feasibility gap**; the repository audit itself is complete
within its stated scope and is not delegated to this checkpoint.

### Dedicated job pool

Create a new Auto Mode NodeClass with a dedicated minimal node role, selected
private subnets/security groups, `networkPolicy: DefaultDeny` and policy event
logging. Do not mutate the shared `default` NodeClass. Block metadata from job
network namespaces, require IMDSv2 where supported and verify actual behavior;
IMDSv2 alone is not a pod isolation control. If the managed NodeClass cannot
express a required control, fail preflight rather than claim it does.

NodePool labels/taint `adp.io/workload=gitlab-ci`, `NoSchedule`, with exact job
nodeSelector and toleration. On-demand Linux amd64 only, bounded c/m/r families
and 2–8 vCPU instances; initial pool limit 16 CPU/64Gi memory shared across
groups. Karpenter limits may temporarily overshoot during parallel provisioning;
namespace quotas and runner concurrency are the admission caps. Use
`consolidationPolicy: WhenEmpty`, `consolidateAfter: 5m`, maximum one voluntary
disruption at a time, no underutilization eviction of active CI, and a bounded
drain/termination grace consistent with job timeout. Manager/system DaemonSet
requests are accounted for; prove that residual system pods do not prevent empty
node reclamation. Node affinity keeps jobs off shared pools; taints alone only
keep other pods out and do not constrain the jobs' destination.

Scale-to-zero acceptance: zero job pods after cleanup, then zero nodes owned by
the job NodePool within 15 minutes of last job completion. Manager replicas and
shared infrastructure still cost money. Missed deadline is failure, not a reason
to force-delete nodes or relax disruption policy. Existing ARC capacity and
agent workloads must not change scheduling or permissions.

## 6. Delivery, rollback and verification ownership

S3 supplies explicit manual-only apply/destroy workflows, **not** deployment from
this design PR or generic launcher integration. Reuse the protected-environment
OIDC mechanism in [trusted-deployment](../../../.github/actions/trusted-deployment/action.yml#L1),
but assert both account and exact role after any cross-account selection. Do not
fall back to the ARC node/runner role. Shell operators use the credential broker
and canonical deployment guide; knowing a target account is not authorization.

1. S1: validate group/CA/private URL, add only instance-owned exports/permissions,
   create paused registration. Verify post-S1 instance baseline with runner
   inputs absent. Test administrator expiry and negative secret readers.
2. S2: resolve Q1; S3 provisions the optional scoped shared build project through
   a separate reviewed platform apply, then the reviewed main-only workflow builds/publishes the
   small bootstrap image, verifies chart checksum, renders and validates policy,
   and checks cluster APIs/endpoint access. Missing prerequisites fail before
   an active runner is introduced. No deployment permissions in PR checks.
3. S3 apply: protected environment approval, target verification, explicit backend,
   `terraform plan -input=false -detailed-exitcode -out=...`; review the saved
   plan's exact state/resource allowlist, then apply that same plan. Keep binary
   plans and detailed target data private. A textual “will be destroyed” grep is
   not the safety oracle. Apply/destroy share a runners-only account/environment
   concurrency key and do not cancel in-flight operations.
4. Keep registrations paused until policy readiness, manager TLS and required
   admission rejections are verified. Delivery owner activates only the scoped
   canary group and runs [the negative matrix](backlog.md#security-negative-matrix).
   B has no authorized matching runner; matching A tags must not change that.
5. Expand only after all applicable ACs pass. Record exact source/config/image
   revisions, attempts and redacted outcomes; never publish raw job variables,
   secret payloads, plans, private hostnames or connection identifiers.

Teardown: pause registrations, cancel/drain jobs with a deadline, verify no active
pods, revoke registrations, remove managers/policies/namespace-owned resources,
delete job pool and await its node claims' absence, then remove own IAM/cache.
Keep policies effective until jobs are gone. Empty only this stack's cache after
explicit confirmation; bucket deletion must not use blanket force-destroy of
unrelated data. Instance secrets and CA are S1-owned: revoke/delete the group
token deliberately, but do not delete shared CA/discovery on ordinary S2 destroy.
Destroy failure leaves a durable cleanup record; do not report success on an
ignored command failure. Infrastructure recovery is a saved-state reconciliation,
not “revert the PR.” A prior chart can be restored only with current valid token
generation and CA; a revoked token is never a rollback credential.

S3 owns scheduled reconciliation, rotation/expiry alerts and runbook handoff;
the delivery owner performs live acceptance. The module owner integrates S1/S2;
platform owner resolves the policy/platform prerequisites. No story is complete
merely because its enabling PR merged. No dispatch or deployment is authorized
by writing these contracts.

## 7. Acceptance, decisions and remaining uncertainty

| Epic criterion | Design and owner | Required evidence (not obtained in this design assignment) |
|---|---|---|
| EC-01 | S1 identity, runtime-only secret delivery, CA and rotation | Group readback; allowed/denied secret reads; revocation; no credential bytes in state/logs |
| EC-02 | S2 per-group manager and namespace; S3 live A/B and idle procedure | A completes on expected runner; B stays pending with A tags; zero job pods/nodes by deadline |
| EC-03 | Q1 + network/admission/identity/resource controls; S3 adversarial probes | Actual job-pod negative connections; admission rejections; credential/metadata and cross-cache denial |
| EC-04 | Separate state, private TLS and independent lifecycle | Post-S1 baseline plans absent/present/destroyed; runner apply/destroy; missing/wrong CA fails readiness/apply; no insecure paths |

Open facts and decisions are bounded, with consequences:

- **Q1 (module/platform owners):** supported, fail-closed policy enforcement on
  actual executor pods is unresolved and blocks isolation-dependent implementation.
  ApplicationNetworkPolicy availability on the target is also unverified.
- **S1 live prerequisites:** actual certificate/root provenance, private rooted
  GitLab API/clone/artifact behavior, admin expiry and token API field readback,
  token auto-rotation configuration. Failure blocks activation, not offline tests.
- **S2 lock/access prerequisites:** actual server/cluster/node versions, image
  digests, registry mirror provenance, EKS access and permission to create a
  minimal job-node role. The chart archive is verified, not the resulting system.
- **S3 fixtures:** existing SSM test-project values are placeholders in source;
  no live tenant group or test-project availability is inferred from them.
  Delivery owner supplies private run-owned fixtures and deployment authority.
- **Catalog lineage:** existing story Project/Module/Feature catalog mappings
  are unresolved. Delivery owner supplies registered identifiers; no invented
  catalog IDs or new issue hierarchy. Native child links were checked.

The backlog gives exact proposed files, preserved AC/scenario mappings and
independent test oracles. This document approves the stated direction and safe
independent work; it does not approve the unresolved confinement mechanism as
implemented or waive any epic criterion.

## 8. External references and evidence limit

Consulted 2026-10-29; vendor web pages are mutable, so qualify the selected runtime:

- [GitLab create runner API](https://docs.gitlab.com/api/users/#create-a-runner): one-time returned token, group type, policy fields.
- [GitLab runner API](https://docs.gitlab.com/api/runners/): readback, pause, delete and token lifecycle.
- [Kubernetes executor](https://docs.gitlab.com/runner/executors/kubernetes/): overwrite settings, security contexts, SA and pod-spec boundary.
- [Runner Kubernetes installation](https://docs.gitlab.com/runner/install/kubernetes/), [chart index](https://charts.gitlab.io/index.yaml), [audited chart archive](https://gitlab-charts.s3.amazonaws.com/gitlab-runner-0.94.2.tgz).
- [Executor source](https://github.com/gitlabhq/gitlab-runner/blob/896d1e6abc0a5868e2612dce45793ffc2f282668/executors/kubernetes/kubernetes.go#L2592): `preparePodConfig`, job metadata and owner-reference direction; this upstream snapshot is not the release qualification.
- [EKS Auto Mode policy](https://docs.aws.amazon.com/eks/latest/userguide/auto-net-pol.html): controller switch and ApplicationNetworkPolicy.
- [EKS NodeClass](https://docs.aws.amazon.com/eks/latest/userguide/create-node-class.html): Auto Mode API, network policy default and node role.
- [AWS policy considerations](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html): standalone pod reliability warning.
- [Related credential-broker design at its reviewed branch revision](https://github.com/aws-e/adp/blob/aa02b1f25815d64ac26a577cccf749bf1770ee4c/docs/design-notes/9189-gitlab-credential-broker/architecture.md): identity context only; PR #9198 was open during this audit, not present at this checkout. CI uses `CI_JOB_TOKEN`; no dependency on the proposed ADP broker.
