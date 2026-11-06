# Implementation backlog and approved child contracts

> **Current executor amendment (owner approved 2026-11-01):**
> [Docker Autoscaler with disposable EC2 jobs](q1-enforcement-contract.md) is
> authoritative for S2, S3 and their evaluations. Managers stay on EKS; jobs
> use the same VPC, private subnets and dedicated security groups. Earlier
> Kubernetes job, admission and Auto Mode pool details below are historical
> design context where superseded by that amendment. S1 is unchanged.

This is the actionable backlog for [#9283](https://github.com/aws-e/adp/issues/9283),
not a new set of issues or permission to deploy. Read [architecture.md](architecture.md)
first. Existing AC and SCN IDs are retained, qualified by story number. All files
and tests below are **proposed**, unless identified as existing. No implementation
or live acceptance result is claimed here.

## Delivery sequence

| Order | Owner / existing story | Deliverable and completion boundary | Dependency / acceptance |
|---|---|---|---|
| 1 | GitLab module owner, [#9284](https://github.com/aws-e/adp/issues/9284) | Additive private discovery, CA export and crash-recoverable group registration; live secret/runner readback remains required | Independent offline work; EC-01, enabling EC-04 |
| 2a | Module + platform owners, [#9276](https://github.com/aws-e/adp/issues/9276) | Resolve Q1 supported pod enforcement and lock runtime versions; write the qualification decision before isolation-dependent implementation | Known design prerequisite, not deferred inventory; EC-03 |
| 2b | GitLab module owner, #9276 | Separate stack, runtime secret loader, restricted managers/jobs, node pool/cache and contract tests | S1 contract + Q1; EC-02/03/04 |
| 3a | Delivery owner, [#9285](https://github.com/aws-e/adp/issues/9285) | Credential-free PR checks, trusted image build, manual apply/destroy, scheduled reconciliation, controlled runbook | Can scaffold against contracts while S1/S2 proceed; all ECs |
| 3b | Delivery owner, #9285 | Authorized dev canary, negative probes, idle/lifecycle verification and per-AC evidence | Integrated compatible revisions, access and private fixtures; all ECs |

These are milestones within the three native child stories, not new dispatches.
The GitLab module owner owns integration failures. Do not copy S1 scripts or S3
workflows into S2's scope merely because the old sketch lists them there.

## S1: approved design for #9284

**Approved design:** use architecture sections 2–3 and the authority matrix in
section 4. Approval covers the contract, not successful deployment. Preserve the
existing instance stack's independent lifecycle and public URL. Add private
runner URL discovery and a validated CA secret; do not expose plaintext through
Terraform. Register only verified tenant-parent **group** runners through the
existing isolated administrator, with locked/tagged/protected policy readback.
Store one versioned JSON authentication secret per numeric group ID. Create
paused, reconcile lost responses, rotate with a paused replacement and durable
operation record, and verify revocation before deleting secret material.

### Exact write scope

| File | Change / contract |
|---|---|
| `modules/source-control/gitlab/scripts/register-runner.py` | New operator CLI: create/rotate/revoke/reconcile; exact group ownership, schema, safe errors and SSM operation phases from architecture §3 |
| `modules/source-control/gitlab/scripts/export-runner-ca.py` | New ACM leaf/chain retrieval, operator-root validation, Secrets Manager write, private route verification; no certificate/private-key print |
| `modules/gateway/src/gitlab/admin_client.py` (existing) | Minimal extension for an explicit verified SSLContext/CA path with current strict defaults preserved; reuse request/pagination/error contracts for runner API; never introduce `verify=False` |
| `modules/source-control/gitlab/infra/runner_registration.tf` | New secret containers, resource policies and exact registrar grants; no secret-version values |
| `modules/source-control/gitlab/infra/ssm.tf`, `variables.tf`, `outputs.tf` (existing) | Add private runner URL and CA-secret-ARN discovery; reference-only outputs; retain `/gitlab/url` behavior |
| `modules/source-control/gitlab/scripts/tests/test_register_runner.py` | API/SM/SSM fault injection, tenancy, retries, expiry and logging tests |
| `modules/source-control/gitlab/scripts/tests/test_export_runner_ca.py` | Synthetic CA fixtures: valid chain, wrong host/root, expired/missing chain, redirect rejection |
| `modules/source-control/gitlab/infra/tests/runner_registration.tftest.hcl` | Policy boundary and secret-value/state regression |
| `modules/gateway/tests/gitlab/test_runner_admin_transport.py` | Existing admin callers retain strict URL, no-redirect and safe-error behavior |

No new database table, gateway route, tenant PAT, EKS provider or automatic group
creation. Reuse existing operator policy rather than authorizing every module
deploy identity to read the admin credential. The CA exporter may use a
separately authorized role that cannot read runner/admin tokens.

### Preserved acceptance and scenarios

| Stable key | Independent assertions / case contract | Phase and evidence |
|---|---|---|
| #9284 AC-01 | API readback is `group_type`, exact group, nonempty correct tag, `run_untagged=false`, `locked=true`, selected access policy; wrong group/instance type rejected; revoke makes old token fail | S1 unit + live API readback, module owner |
| #9284 AC-02 | Synthetic canary admin/runner strings never occur in stdout/stderr, HTTP exceptions, subprocess output, Terraform/Helm state or captured debug logs; fault paths included | Before review; automated and reviewed logs |
| #9284 AC-03 | Exact manager/CA reader succeeds; unrelated group/agent/gateway identities denied; hostname/chain verification fails for wrong root | Live IAM + TLS evidence; module owner |
| #9284 SCN-01 → AC-02 | Given fake API/storage and fixed canary credentials; When create/rotate/revoke and failures execute through the actual CLI; Then no canary or raw provider payload is emitted and unfinished operations remain resumable; Controls: fixed UTC clock, fixed UUID, independent expected call/state table, every retry and cleanup | `automatic-unit`, planned tests above; also exercises ownership and recovery contracts without claiming live AC-01 |
| #9284 SCN-02 → AC-01, AC-03 | Given run-owned tenant group plus allowed/denied roles; When create/read/rotate/revoke and CA reads run through approved identities; Then exact readback, denial and old-token rejection; Controls: private fixture manifest, bounded 10-minute phase deadlines, no shared existing tenants, final absence checks | `manual`, planned versioned S3 runbook; logs contain statuses/IDs only in private record |

First review checkpoint: CLI fault matrix, unchanged admin-client tests and
Terraform plan/mock tests. Actual registration/IAM/CA acceptance follows authorized
deployment. Token auto-rotation and existing admin expiry are activation checks.

## S2: approved design for #9276

**Approved design:** architecture sections 2, 4–5 and lifecycle section 6.
Implement only after the Q1 supported-enforcement decision for isolation-dependent
parts; safe offline contracts and the release-lock checkpoint can start now.
The separate backend consumes platform identifiers and S1 references, never
instance state mutations. One manager/job namespace pair per group; native
Kubernetes executor; runtime tmpfs secret delivery; manager-only IRSA/cache
signing; no job AWS/Kubernetes identity; restricted admission and exact egress;
dedicated Auto Mode NodeClass/NodePool. “Zero” means jobs and job nodes, not polling
managers. Privilege, TLS bypasses and arbitrary runner TOML are not configuration
options. This supersedes the sketch's state-held Kubernetes Secrets and public URL.

### Exact write scope

| File under `modules/source-control/gitlab/runners/` unless prefixed | Owned outcome |
|---|---|
| `versions.tf`, `main.tf`, `variables.tf`, `outputs.tf` | Separate providers/backend, read-only discovery, validated group map, non-secret outputs and dependency ordering |
| `iam.tf`, `cache.tf` | Exact group IRSA/cache roles and resource policies, dedicated node role/access entry, private cache lifecycle |
| `namespaces.tf`, `rbac.tf`, `admission.tf` | Manager/job namespaces, PSA restricted, quotas, minimal role bindings, admission enforcement including forbidden overrides |
| `network.tf`, `nodepool.tf` | All-pod deny, qualified FQDN policy, manager exceptions, dedicated Auto Mode NodeClass and NodePool; no mutation of shared defaults |
| `helm.tf`, `templates/runner-values.yaml.tftpl` | Digest/version lock, init-loader and memory mounts, strict runner config, timeout/cleanup/resources and verified helper trust |
| `versions.lock.json` | Chart URL/version/hash; manager/helper/bootstrap/probe digest+architecture, server/cluster compatibility, source provenance and Q1 qualification reference; no fake digest |
| `bootstrap/Dockerfile`, `bootstrap/requirements.lock`, `bootstrap/load_credentials.py` | Small non-root loader built from pinned base/dependencies; reads exact SM objects to tmpfs, validates identity, refuses invalid CA, emits only safe errors |
| `tests/security_contract.tftest.hcl`, `tests/test_rendered_runner.py`, `tests/test_bootstrap.py` | Terraform boundaries, real Helm render/entrypoint tests and actual loader fault tests; no live credentials |
| `environments/dev/modules/gitlab-runners.tfvars.example`, `environments/dev/modules/gitlab-runners-backend.tfvars.example` | Fictional portable schema examples only; deployment uses protected/private actual inputs |

Q1 may require an explicitly approved amendment to this write set. Do not
implement a guessed owner-reference controller or a second network stack under
the existing file list. Tests must inspect the rendered chart, policies and IAM
as a whole, including additive allows, not just find required strings in source.

### Preserved acceptance and scenarios

| Stable key | Independent assertions / case contract | Phase and evidence |
|---|---|---|
| #9276 AC-01 | A job reports expected runner ID/group; B with identical tags is never claimed by A and remains pending; no instance/shared runner fallback | Live GitLab API/job + Kubernetes UID evidence |
| #9276 AC-02 | Every denial in negative matrix; actual bare executor lifecycle covered by supported Q1 contract; admin-side admission tests show privilege/override rejection | Before review contracts; live actual job-pod evidence |
| #9276 AC-03 | Job pods removed and exact pool has no nodes within 15 minutes; manager remains 1; no jobs escape onto shared nodes | Live API snapshots with bounded polling, not sleep-only checks |
| #9276 AC-04 | Post-S1 instance plans are no-op with runner state absent/present/destroyed; runner state lifecycle independent; no unexpected state resource/import | Before review dependency tests; authorized lifecycle plans after deployment |
| #9276 AC-05 | Missing/wrong CA fails loader and waited Helm apply; no HTTP/insecure option in rendered config; helper clone with wrong root fails | Offline TLS fixtures + live negative readiness |
| #9276 SCN-01 → AC-04, AC-05 | Given fixed platform/SM metadata fixtures and synthetic certs; When Terraform tests, pinned Helm render and real loader run; Then independent expected resource/state boundary and TLS outcomes hold; Controls: pinned providers/archive, fixed clock, disposable local files, exact deny matrix | `automatic-integration`, planned tests above; no claim that mocked state proves live no-diff |
| #9276 SCN-02 → AC-01, AC-02, AC-03 | Given run-owned groups and qualified actual runner/node image; When tagged A/B jobs and negative probes run then idle; Then tenancy, denial and zero-job-node assertions hold; Controls: fixed digests, private endpoints, fresh policies, repeated cold starts, deadlines, all attempts retained, cleanup | `manual`, planned S3 versioned runbook |

The provider lock file generated by implementation must be committed. Test
S3 signing without helper credentials, actual chart secret/memory mounts and
manager logs; do not claim a missing-CA metadata check proves wrong-CA rejection.

## S3: approved design for #9285

**Approved design:** architecture §6 and the procedure/matrix below. Provide
manual reviewed-main apply/destroy, a secret-free PR check, trusted image build,
bounded scheduled operation recovery and a reproducible live acceptance runbook.
Use the existing protected deployment identity mechanism, but no ambient fallback,
hard-coded environment mismatch, raw secrets in `GITHUB_ENV`, or unreviewed-ref
deployment. Workflows must operate only on runner state. Live dev acceptance is
owned by the delivery owner and remains after merge; this design run does not
perform it. Refs to implementation PRs must not automatically close live stories.

### Exact write scope

| File | Owned outcome |
|---|---|
| `.github/workflows/gitlab-runners-check.yml` | PR path filters covering S1/S2/scripts/workflows; unit tests, render/contract/format/public-doc checks; no deployment credentials |
| `.github/workflows/gitlab-runners-build.yml` | Reviewed-main trusted bootstrap image build/push/scanning and digest receipt; reviewed recipe, no source/ref override and no privileged GitLab jobs |
| `codebuild/bs-gitlab-runner-bootstrap.yml` | New fixed bootstrap-image build recipe consumed through existing `codebuild-run` action; no secret fetch or tenant pipeline source |
| `platform/infra/modules/codebuild/main.tf`, `variables.tf`; `platform/infra/main.tf`, `variables.tf` (existing) | Optional default-off `enable_gitlab_runner_build` project entry, exact bootstrap ECR repository/source prefix/build role using existing per-project pattern; independently reviewed platform apply before first build |
| `platform/infra/modules/codebuild/tests/gitlab_runner_build.tftest.hcl` | Optional-off no-diff and enabled exact-project/IAM scope tests; no authority to run arbitrary tenant source |
| `.github/workflows/gitlab-runners-apply.yml` | Explicit environment/backend/target role, prerequisite checks, saved-plan guard and approved apply, pause/qualify/activate sequencing |
| `.github/workflows/gitlab-runners-destroy.yml` | Exact `gitlab-runners` confirmation; shared lock with apply; bounded drain/revoke/destroy/absence; retain instance state and shared CA |
| `.github/workflows/gitlab-runners-reconcile.yml` | Reviewed-main schedule plus manual recovery; exact allowed groups; resume S1 operation records, expiry alerts and safe rotation; fixed maximum run time |
| `.github/scripts/tests/test_gitlab_runners_workflows.py` | Entry inputs, identity assertion, concurrency, path coverage, secret safety, main-only mutation and no collateral-state contract |
| `modules/source-control/gitlab/scripts/runner-stack-preflight.py` | Read-only schema/API/version/identity/TLS/policy prerequisites and runner readiness; no secret output; failure exit codes |
| `modules/source-control/gitlab/scripts/runner-stack-plan-guard.py` | Parse plan JSON privately; own state/resource/address allowlist, replacement/delete gate; independently reject instance/platform changes |
| `modules/source-control/gitlab/scripts/tests/test_runner_stack_guards.py` | Allowed and adversarial saved-plan/config fixtures, false-positive healthy-page detection |
| `modules/source-control/gitlab/docs/runners.md` | Versioned registration/build/apply/canary/rotation/teardown/incident runbook and every scenario mapping |
| `modules/source-control/gitlab/tests/acceptance/.gitlab-ci.yml`, `probe.py`, `verify.py`, `fixtures.schema.json` | Pinned probe job, private endpoint manifest, independent GitLab/Kubernetes assertions and structured outcomes |
| `modules/source-control/gitlab/tests/acceptance/privileged-pod.yaml`, `override-pods.yaml` | Synthetic, explicitly rejected admission fixtures; never a successful privileged deployment |

Existing `.github/actions/trusted-deployment/action.yml` and load-config interfaces
are reused, not copied or broadly rewritten. Existing instance workflows, generic
`deploy.sh`/`undeploy.sh`, gateway deploy and ARC build are unchanged by this epic.
The optional shared CodeBuild project is a build prerequisite, not part of the
runner apply/destroy state. Its ECR image remains available after runner teardown
for recovery. The existing per-project build pattern permits repository creation
on first build against its exact ARN; do not create competing ECR Terraform
ownership. Build-container privilege, if needed by the existing trusted image
builder, is confined to that reviewed CodeBuild job and must never be inherited
by GitLab jobs. If policy forbids it there too, image publication waits for an
approved nonprivileged builder rather than weakening runner admission.

### Preserved acceptance and scenario

| Stable key | Independent assertions / case contract | Phase and evidence |
|---|---|---|
| #9285 AC-01 | A completes on intended runner; B remains pending throughout observation; job pods/nodes disappear by configured deadline | Live delivery-owner result linked to #9276 AC-01/03 |
| #9285 AC-02 | Actual job-network negatives fail for intended reason; privileged/override API requests receive admission denial, not merely invalid GitLab YAML | Live probe + admission reason, linked to #9276 AC-02 |
| #9285 AC-03 | No token or CA value in apply logs, state artifacts or workflow outputs; synthetic canary capture verifies error paths before review; live inspect only in private session | Before review tests and private live log review |
| #9285 SCN-01 → AC-01, AC-02, AC-03 | Given private run-owned A/B fixtures, approved roles, pinned integrated revision and image lock; When apply, A/B, probe, idle, rotation and teardown run; Then explicit matrix and absence checks hold; Controls: 10-minute pipeline bound, 5-minute B observation while A is online, 15-minute idle bound, connection timeout 3s with bounded retries, record all attempts, no secret logs, cleanup required | `manual`, planned versioned runbook; offline workflow tests are supporting checks, not manual acceptance |

## Security-negative matrix

The live verifier runs outside the untrusted job using a read-only acceptance
identity plus a separately scoped admission-test actor. The job cannot supply
its own sole pass/fail verdict. Save structured outcomes and private evidence
references; no raw env dumps, headers, signed URLs or tokens in published logs.

| Case | Stimulus / required independent oracle | Mapping |
|---|---|---|
| Group escape | Same tags in A and unrelated B; A runner stays online and A control succeeds; B remains unclaimed/pending for 5 minutes; cancel B afterwards | #9276 AC-01; #9285 AC-01; EC-02 |
| Wrong registration | Request instance type, wrong tenant marker, changed numeric group, or wrong token JSON binding; registration/bootstrap refuses before activation | #9284 AC-01; #9276 AC-01; EC-01/02 |
| Gateway/Dev Box/RDS/Redis | Actual job attempts DNS name and resolved private IP/port; trusted positive fixture confirms target is listening; failed connection alone to a down server is inconclusive | #9276 AC-02; #9285 AC-02; EC-03 |
| Bypass egress | Arbitrary external HTTPS, internet proxy, direct IP, alternative DNS/DoH, other namespace, metadata IPv4/IPv6, API/STS/SM; all blocked; GitLab HTTPS and scoped cache positive controls succeed | #9276 AC-02; #9285 AC-02; EC-03 |
| Early/policy-loss reachability | Actual newly created runner pod probes before first instruction and repeatedly across cold node starts/controller restart; independently correlate policy endpoint UID and node default-deny; unsupported lifecycle is a block even if sampled probes pass | Q1; #9276 AC-02; EC-03 |
| Privilege and override | Privileged/hostNetwork/hostPath/capability/token-volume/SA/node-selector/ephemeral-container create or update using manager-equivalent actor; API admission rejects each with expected reason; legal non-root fixture admitted | #9276 AC-02; #9285 AC-02; EC-03 |
| Tenant credential/cache escape | Job has no manager token/IRSA/kube token mount or environment; attempts another group's secret/cache object; network and IAM denials recorded independently; manager cache operation succeeds | #9284 AC-03; #9276 AC-02; EC-01/03 |
| Wrong TLS | Valid root/name control succeeds; missing/expired/wrong root or hostname and cross-origin redirect fail in loader, manager and helper clone/artifact clients; apply readiness fails without activating runner | #9276 AC-05; EC-04 |
| Token-loss recovery | Inject lost create response, failed SM write, restart before version promotion, failed revoke and reporting failure; paused orphans reconciled, old token rejected and pending records retained until resolved | #9284 AC-01/02; #9285 AC-03; EC-01 |
| Secret emission | Canary secrets through success, HTTP failure, TLS failure and chart registration path; search full stdout/stderr/artifacts/state for exact canaries and derived encodings; capture live result privately | #9284 AC-02; #9285 AC-03; EC-01 |
| Idle and confinement | After legal job and failed job cleanup: no job pods, no pool nodes within 15m, manager still running, unrelated ARC jobs unchanged; never force-delete to manufacture pass | #9276 AC-03; #9285 AC-01; EC-02 |
| Lifecycle independence | Compare post-S1 baseline plan absent/present/after destroy at same source/private inputs; inspect both state address sets; instance no-op, only own resources destroyed, no active token/node/cache residue | #9276 AC-04; EC-04 |

Bounded retry attempts must all be retained; never report only the eventual pass.
For network tests, record timeout/refusal/admission reason without interpreting
every failure as policy enforcement. Use fresh run-owned resources and one
cleanup owner. Cleanup failures make that scenario incomplete. A Kubernetes
executor does not recognize `privileged: true` as a magic `.gitlab-ci.yml` request;
invalid YAML is not proof that cluster admission refused a privileged pod.

## Reproducible review and handoff

Before implementation review, run each story's new tests plus adjacent existing
admin-client/instance tests, render the pinned Helm chart, validate Terraform and
workflow contracts, and scan public docs. Verify schema/role policy differences
against the independent expected matrix. Record exact selectors when created;
the proposed file paths above are not claims that tests already exist.

After deployment, report `EC → story AC → SCN → run/attempt → outcome → evidence →
source/config/image revision`. Use accepted/rejected/blocked/inconclusive for
acceptance, separate from test pass/fail. The delivery owner supplies catalog
Project/Module/Feature mappings before marking execution readiness, preserves
native parent links, and keeps all live criteria pending until measured. The
design PR is a reviewable contract, not a completion report for the epic.
