# S20 — Triage of unscored scanner records (2026-09-21 run)

Work package **S20** ([#5619](https://github.com/aws-e/adp/issues/5619)) of the
2026-09-21 security review ([#5599](https://github.com/aws-e/adp/issues/5599)).

This directory holds the disposition of every scanner record that arrived with no
security severity: **234 Semgrep error-level records + 626 Checkov failures = 860**.

| File | Contents |
|------|----------|
| `README.md` | This document — method, group-by-group verdicts, proposed follow-ons |
| `s20-dispositions.json` | Machine-readable disposition for all 860 records |
| `s20-source-records.json` | Local normalized copy of every source record's identity and location fields |
| `s20-ownership-evidence.json` | S01-S21 identity map and record-key/file evidence for mapped and unowned open records |
| `s20-ownership-contracts.json` | Hash-pinned S01-S21 contract/path projection used for strict overlap resolution |
| `validate.py` | Reconciliation, aggregate, workflow-triage, and ownership validation |

## Scope and boundary

The S20 triage is **diagnosis only**: no IAM, workflow or manifest remediation
is included, and **no scanner suppression or baseline entry was created**. The
S20-owned evidence is limited to the six triage documentation artifacts in this
directory. Where a record is a false positive, the reason is recorded
against the specific location so a future genuine instance still fires.

The **13 explicitly suppressed critical/high Semgrep records** in
`findings.json.suppressed_explicit_findings` are deliberately **out of scope**:
they remain historical dispositions, are not reclassified as new findings, and are
not part of the 860.

## Provenance

| Item | Value |
|------|-------|
| Source run | [35545387749](https://github.com/aws-e/adp/actions/runs/35545387749) |
| Inventory artifact commit | `3193c78b167f583eed5026c582ab58794c88c34e` |
| Independently reconciled PR revision | `428eac3b2151f24d6c829179141239da3532257d` |
| Audited disposition SHA-256 | `c4180d9b7990a0e099e7a46982f8ef02f5401d1dbb17088c177cfd73e474ebb1` |
| Independent reconciliation attestation | `cf8f0c93...:data/code-review/review-20260921-pr-5702.md` (`314699db041d806ba3b385091540c560e1607fa2ec02890a4a5640908ddb7fe9`) |
| Normalized source export SHA-256 | `bbac1c39fe13bc971062e36e8ddbc1ceebc966f769e0cc5ea2701ce066b45a8b` |
| Normalized source export Git blob | `abad8d5b9efc877a28e76fff89416424bd317d94` |
| **Code reviewed at (scanned commit)** | `fb4bc620d065a124f1c73f53c3956159e60b4d36` |
| Ownership plan commit | `42ade12e0f09570179c86a75a1c224cabab77cb4` |
| Ownership contract projection SHA-256 | `c89a617572317c1a6ee8774d2736123eedb87bcded9d7f977411ade15610f3aa` |
| Work-package identity attestation | `4683b3ea...:doc/ai-dlc-engine/ai-dlc-topology-preview.json` (`8cb7ce37479ab6140a41d9640bea034f2598eb7d97c74f2f752dbaedf8388b47`) |
| Semgrep artifact | `original/semgrep/semgrep-results.sarif` |
| Checkov artifact | `original/checkov/results_sarif.sarif` |

Verdicts were reached by reading the cited code **at the scanned commit**, not at
current `main`. A record's disposition may therefore need re-checking if the file
changed after `fb4bc620`.

## Completeness

Every record is keyed by `(tool, artifact, result_index)`, which is unique across
the source inventory. Duplicate occurrences of the same rule stay individually
traceable because each keeps its own `result_index`, `file` and `line`.

```
source keys       860   (234 semgrep + 626 checkov)
disposition keys  860
missing             0
extra               0
unaccounted         0
```

`s20-source-records.json` is the local normalized copy of all source identity
and location fields retained from the inventory commit. It lets reviewers compare
all 860 records without fetching another revision. Reproduce the reconciliation
and all aggregate checks with:

```bash
python3 docs/security/runs/2026-09-21/triage/validate.py
```

The validator compares every retained source field, not only counts: tool,
artifact, result index, run, rule/check, file, and start/end lines. This preserves
duplicate occurrences while excluding triage-only verdict fields from the source
side of the comparison. Those fields must exactly match the hash-pinned disposition
file at `428eac3b...`, the revision independently reconciled against the canonical
inventory by its immediate child review commit `cf8f0c93...`.

The authoritative check is `validate.py --require-canonical`. It prefers a direct
comparison with the canonical commit object, or an exact local copy supplied with
`--canonical-findings <path> --canonical-sha256 <trusted-digest>`; both direct
modes also require exactly 13 unique historical suppressions disjoint from the 860.
When neither is present, it validates the repository-local evidence chain instead: the audited
revision and attestation blobs must match their recorded SHA-256 values, the
attestation must be the audited revision's immediate child, all 860 retained fields
must match that audited revision, and the attestation must record the independent
234 + 626 reconciliation and zero overlap with the 13 historical suppressions.
This fallback is an immutable independent verification record, not a claim that the
absent canonical blob was compared again in the current checkout.

## How severity was assigned

Assessed severity reflects **demonstrated reachability at the scanned commit**, not
the rule's own name or default level:

- **none** — not a security defect here (fixture, intended behavior, guarded by a
  control the scanner cannot see, or a non-security correctness rule).
- **low** — defense-in-depth or posture gap; no demonstrated exposure.
- **medium** — a real weakening of a control boundary, exploitable only after
  another failure or with existing access.
- **high** — a control boundary is open by default in committed configuration.

| Severity | Records |
|---|---|
| high | 4 |
| medium | 368 |
| low | 184 |
| none | 303 |
| info | 1 |

Counts are per **record**, so a single cause spread over many files dominates the
medium row (244 of the 368 are one Kubernetes baseline gap across 20 manifests).

## Group dispositions

| Group | Recs | Files | Severity | Verdict | Owner | Rules |
|---|---|---|---|---|---|---|
| `k8s-pod-hardening` | 244 | 20 | medium | needs-followon | FOLLOWON-E | CKV_K8S_10/11/12/13 +15 |
| `observability-hardening` | 70 | 39 | 67 low; 3 none | 67 accepted-risk-low; 3 false-positive-resolved-configuration | S20-reviewed | CKV2_AWS_10/11/20/29 +30 |
| `iam-wildcard-policy` | 74 | 14 | 56 medium; 16 none; 2 low | 56 routed-existing-owner; 15 accepted-risk-required; 2 accepted-risk-documented; 1 not-currently-reachable | 27 S12; 26 S14; 18 S20-reviewed; 3 S17 | CKV_AWS_109/111/286/287 +5 |
| `encryption-cmk` | 62 | 45 | low | accepted-risk-low | S20-reviewed | CKV_AWS_119/136/145/173 +12 |
| `workflow-shell-interpolation` | 54 | 30 | 41 medium; 13 none | 41 needs-followon; 11 false-positive-constrained-context; 2 false-positive-callsite-controlled | 41 FOLLOWON-F; 13 S20-reviewed | run-shell-injection, github-script-injection |
| `secret-pattern` | 50 | 7 | none | 44 false-positive-fixture; 6 false-positive-pattern-match | S20-reviewed | detected-jwt-token, node_api_key, node_secret, gcm-service-account |
| `resilience-backup` | 45 | 31 | 39 none; 6 medium | 39 not-security; 5 routed-existing-owner; 1 needs-followon | 39 S20-reviewed; 4 S21; 1 FOLLOWON-G; 1 S17 | CKV_AWS_28/51/293/300 +12 |
| `ssm-plaintext-parameter` | 43 | 10 | none | false-positive-nonsensitive | S20-reviewed | CKV2_AWS_34 |
| `sql-construction` | 36 | 20 | none | false-positive-bound-params | S20-reviewed | avoid-sqlalchemy-text, sqlalchemy-execute-raw-query |
| `subprocess-audit` | 30 | 22 | none | 28 false-positive-operator-tooling; 2 intended-behavior | S20-reviewed | dangerous-subprocess-use-audit, dangerous-os-exec-* |
| `code-correctness` | 21 | 16 | none | not-security | S20-reviewed | arbitrary-sleep, leaky-time-after, tempfile-without-flush +2 |
| `codebuild-privileged` | 16 | 3 | low | accepted-risk-required | S20-reviewed | CKV_AWS_316 |
| `secret-rotation` | 15 | 9 | low | needs-followon | FOLLOWON-D-SECRET-ROTATION | CKV2_AWS_57 |
| `terraform-backend` | 14 | 14 | 12 none; 1 low; 1 medium | 12 false-positive-partial-backend; 1 accepted-risk-low; 1 needs-followon | 13 S20-reviewed; 1 FOLLOWON-H | CKV_TF_3, CKV_TF_1 |
| `path-handling` | 12 | 11 | none | false-positive-fixed-paths | S20-reviewed | hooks-path-traversal-python |
| `supply-chain-curl-pipe` | 10 | 5 | medium | needs-followon | FOLLOWON-A | gha-curl-pipe-shell, hooks-wget-pipe-bash |
| `lambda-vpc` | 9 | 8 | none | false-positive-by-design | S20-reviewed | CKV_AWS_117 |
| `workflow-secrets-inherit` | 9 | 9 | 8 medium; 1 low | 8 needs-followon; 1 accepted-risk-low | 8 FOLLOWON-I; 1 S20-reviewed | secrets-inherit |
| `xml-parsing` | 9 | 2 | 6 low; 3 none | 6 needs-followon; 3 false-positive-constrained-context | 6 FOLLOWON-B; 3 S20-reviewed | use-defused-xml, B410 |
| `network-egress-open` | 8 | 7 | low | accepted-risk-low | S20-reviewed | CKV_AWS_382 |
| `network-exposure-eks` | 6 | 3 | 4 high; 2 low | 4 needs-followon; 2 accepted-risk-documented | 4 FOLLOWON-C; 2 S20-reviewed | CKV_AWS_38, CKV_AWS_39 |
| `api-auth` | 4 | 2 | none | false-positive-guarded | S20-reviewed | CKV_AWS_59, CKV_AWS_309 |
| `network-default-sg` | 3 | 3 | low | needs-followon | FOLLOWON-D-DEFAULT-SG | CKV2_AWS_12 |
| `iam-administrator-access` | 2 | 2 | 1 info; 1 medium | 1 intended-behavior; 1 needs-followon | 1 S14; 1 S20-reviewed | CKV_AWS_274 |
| `network-public-subnet` | 2 | 1 | none | false-positive-by-design | S20-reviewed | CKV_AWS_130 |
| `shell-network-egress` | 2 | 2 | none | false-positive-intended | S20-reviewed | hooks-dns-exfiltration |
| `imds-v1` | 1 | 1 | medium | needs-followon | FOLLOWON-D-IMDSV2 | CKV_AWS_79 |
| `archive-extraction` | 1 | 1 | none | false-positive-guarded | S20-reviewed | tarfile-extractall-traversal |
| `terraform-test-fixture` | 7 | 1 | none | false-positive-fixture | S20-reviewed | CKV_AWS_7/73/76/120, CKV2_AWS_4/51/64 |
| `public-template-access` | 1 | 1 | low | accepted-risk-documented | S20-reviewed | CKV2_AWS_6 |

### The large false-positive groups, and why

**`workflow-shell-interpolation` (54).** These require a trust-boundary review,
not a rule-wide false-positive verdict. At the scanned commit the split is:

- **41 reachable, medium, FOLLOWON-F:** records 50, 51, 78, 79, 84, 85, 86, 88, 93,
  118, 120, 201, 205, 227, 237, 238, 244, 254, 260, 279, 283, 284, 292,
  293, 295, 304, 306, 310, 312, 350, 354, 356, 363, 367, 368, 370, 422,
  423, 452, 476 and 478. Free-form `workflow_dispatch` strings are substituted
  before shell parsing in 37 records. Records 50/51 are a `workflow_call` entry
  point in `_deploy-eks.yml`: repository documentation enables calls from other
  organization repositories and makes `arc-runner-org` available to the
  organization, so absence of a same-repository caller is not unreachability.
  Its free-form module, namespace and cluster inputs enter shell source after AWS
  credential setup. Records 254/260 interpolate `github.ref`
  while permitting dispatch from tags; Git tag names can contain shell syntax.
  Dispatch/tag creation requires existing repository access, which limits the
  attacker population but does not make the data repository-controlled. These
  jobs use self-hosted runners and many later assume cloud credentials, so command
  execution is a real control-boundary failure.
- **11 constrained false positives:** records 81, 94, 190, 193, 199, 209, 210,
  211, 223, 224 and 239 use typed booleans/choices, fixed platform contexts, or
  repository-controlled environment/build outputs. Each JSON rationale states
  the exact constraint.
- **2 call-site-controlled false positives:** records 11/18 are composite actions
  whose complete scanned-commit call graph supplies fixed scanner paths/names and
  repository-controlled bucket/prefix values.

Records 422/423 are especially concrete: `seed-hosted-tenant.yml` substitutes
`tenant_id`, `installation_id`, and `org_name` into shell source. The regex checks
run only after the shell parses the substituted text, so they cannot prevent
command substitution; the SQL builder also inserts `org_name` as an unparameterized
literal. No rule suppression is proposed.

**`secret-pattern` (50).** 43 of 44 JWT hits are one file,
`modules/agent-factory/agent/src/__fixtures__/control-envelope-vectors.json`,
whose own header states it carries a **public verification key only** and that the
signing key is generated per run and never written. Consistent with that, the
vector set deliberately includes `alg: none` and `hs256` negative-test cases. The
44th is a frontend unit-test literal ending `.abc123`. The remaining hits match
the *string* `"service_account"` used as a DynamoDB discriminator, and frontend
constants whose values are CLI instruction text. **No live credential.** No secret
value is reproduced in this directory — locations only.

**`ssm-plaintext-parameter` (43).** All 43 are `type = "String"` holding
non-secret wiring: endpoints, queue URLs, bucket names, role ARNs, Cognito IDs,
ports, feature flags. Five have secret-shaped *names* (`jwt-secret-name`,
`api-token-arn`, `*-signing-key-arn`); each stores a Secrets Manager **ARN or
name**, not key material. The secrets themselves live in Secrets Manager.

**`sql-construction` (36).** Every site interpolates only module-level constants
or a dialect literal chosen by a boolean (`uuid_expr`, `tenant_clause`, index
names); all caller-supplied values are bound parameters (`:sub`, `:tid`,
`:asset_id`). Most are Alembic migrations; the two request-path sites
(`assets_router.py`, `status_callback_routes.py`) build their `WHERE` clause from
a fixed condition list with bound values. No caller text reaches SQL.

**`terraform-backend` (13 × CKV_TF_3, 1 × CKV_TF_1).** For 12 CKV_TF_3
records, the flagged `backend.tf` is an intentionally empty partial backend and
the corresponding deployment path supplies `dynamodb_table =
"adp-terraform-locks"` at initialization, so Checkov cannot see the effective
lock configuration. Checkov index 0 is different: `modules/agent-context/deploy.sh`
changes into the flagged Terraform directory and runs `terraform init -upgrade`
without `-backend-config` in both deployment branches. A fresh initialization can
therefore omit the optional DynamoDB lock table. That medium finding remains open
as FOLLOWON-H. The CKV_TF_1 record is unrelated to backend selection:
`modules/agent-factory/runner-infra/infrastructure/vpc.tf:1` uses the registry VPC
module with `version = "~> 5.0"` rather than an immutable commit. It remains a low
supply-chain hardening decision, not a local-backend disposition.

**`api-auth` (4).** `CKV_AWS_59` on the webhook route: `authorization = "NONE"` at
API Gateway is correct because the GitHub webhook authenticates by **HMAC
signature verification inside the Lambda** (`lambda/common/signature.py`), which a
config scanner cannot observe. `CKV_AWS_309` on the WebSocket API: `$connect`
carries `authorization_type = "CUSTOM"` with a Lambda authorizer, and AWS does not
permit authorizers on `$disconnect` / `sendMessage` / `$default`, which are
unreachable without an authorized connection.

**`archive-extraction` (1).** `tarfile.extractall(..., filter="data")` is the
supported Python guard against traversal, device nodes and escaping links — the
mitigation the rule asks for is already present.

**`terraform-test-fixture` (7).** Checkov indexes 97, 98, 99, 100, 297,
306 and 341 all refer to `webhook-ingress/tests/fixtures/waf/dependencies.tf`.
Its first two lines establish that it has no backend and every WAF test uses the
mocked AWS provider. The empty KMS resource and dummy API Gateway stage supply
test dependencies. Their missing production controls are non-reachable fixture
findings, not deployed posture debt; these seven records have severity **none**.

**`CKV_AWS_339` (3).** Checkov did not resolve EKS version variables. Runner
`variables.tf:19` and cyber `variables.tf:116` both default their directly passed
cluster version to `1.35`; platform `variables.tf:36` has the same default and
`environments/dev/platform.tfvars:8` explicitly selects `1.35`. EKS supported
1.35 on the scan date, so indexes 59, 110 and 239 are location-specific
`false-positive-resolved-configuration` records with severity **none**, not
accepted observability risk.

**`CKV_AWS_51` (6).** Mutable ECR tags are a supply-chain integrity decision,
not availability hygiene. Cyber explicitly publishes `latest` plus a commit tag
and deploys `latest` (index 106, medium, routed to S17). Gbrain stores mutable tags
and its Fargate task consumes `:latest` (index 196, medium, FOLLOWON-G). Four
platform module occurrences (231, 233, 235, 237) inherit the `MUTABLE` default;
platform scripts publish `latest` alongside release/SHA tags. Those shared
release/tag semantics route to S21. In every case, an authorized ECR writer can
retarget an already reviewed tag, so all six remain open.

### Accepted-risk groups

`encryption-cmk` (62) — data **is** encrypted at rest with AWS-managed keys; these
ask for customer-managed KMS keys. A key-custody decision with cost and rotation
consequences, best taken platform-wide rather than per-resource.
`observability-hardening` has 67 accepted low records for access/flow logs,
X-Ray, caching, WAF, request validation and TLS policy; the other three are the
resolved EKS-version false positives above. `network-egress-open` (8) is
conventional `0.0.0.0/0` egress for image pulls and AWS API calls.
**`codebuild-privileged` (16).** All occurrences require Docker-in-Docker, but
the projects and role boundaries differ by package:

| Package / records | Projects at `fb4bc620` | Actual service role | Disposition |
|---|---|---|---|
| Agent context (5) | `ingestion`, `codegraph-context`, `litellm-proxy`, `deepwiki`, `context-mcp` | `var.codebuild_service_role_arn`, supplied from the platform remote-state `codebuild_role_arn` | Privileged mode accepted for image builds; the shared role is part of S14 scope |
| Gbrain (1) | `gbrain_build` | Package-local `aws_iam_role.codebuild` with ECR push, CloudWatch Logs and S3 source-read policies | Package-local privileged-mode risk; not part of the platform AdministratorAccess finding |
| Platform (10) | `gateway-build`, `chat-agent`, `agent-gateway`, `arc-runner`, `cyber-worker`, `agent-runtime`, `pyjwt-layer`, `psycopg2-layer`, `grype-scan`, `syft-scan` | Shared `aws_iam_role.codebuild` with `AdministratorAccess` | Privileged mode accepted for Docker-requiring builds; role scope remains open under S14 |

The shared platform role therefore has **15 consumers**, not four: all ten
platform projects plus all five agent-context projects. A safe S14 replacement
must inventory needs across those consumers. The module header explicitly lists
ECR push, S3 read, CloudWatch Logs, EKS describe and Secrets Manager read; the
module also grants security-scan S3 uploads, while the layer buildspecs upload
artifacts to S3. Gbrain is independently scoped and must not be folded into that
AdministratorAccess remediation.

`workflow-secrets-inherit` has one location-specific accepted risk: record 343
calls `./.github/workflows/eval-cli-uplift.yml` in the same repository and
revision. Records 2368–2375 are not same-repository calls and remain open under
FOLLOWON-I. The 39 non-ECR `resilience-backup` records and all 21
`code-correctness` records are non-security concerns such as PITR, multi-AZ,
deletion protection, `time.sleep` and Go `time.After`.

**`public-template-access` (1, Checkov index 381).** The optional
`modules/agent-factory/infra/main.tf` bucket deliberately serves public
CloudFormation templates. `enable_public_cfn_bucket` defaults to `false`; when
enabled, all four public-access-block settings are disabled and the bucket policy
allows anonymous `s3:GetObject` for every object. Public read is intentional,
with low-severity documented risk that only public content may be uploaded.
This is not evidence of the live feature setting or of actual bucket contents.
It is separate from generic observability hardening.

These are **not closed**. They are recorded as accepted posture with a stated
reason so S21 can confirm or overturn them; none is suppressed.

## Proposed follow-on work items

Definitions only — S20 does not assign or dispatch. Each follow-on has a bounded
write scope. The hash-pinned contract projection now resolves every open record
against all S01–S19 contract topics and path selectors even when the verbatim
immutable plan object is unavailable locally.

**FOLLOWON-C — EKS API endpoints public by default (high, 4 records, unowned).**
`modules/domain-apps/cyber/infra/eks.tf:150` sets `endpoint_public_access = true`
with `cyber_eks_public_access_cidrs` defaulting to `["0.0.0.0/0"]`, and
`modules/agent-factory/runner-infra/infrastructure/eks.tf:10` sets it true while
supplying **no** `public_access_cidrs` (AWS then treats it as `0.0.0.0/0`).
Authentication still applies, so this exposes the control-plane API surface rather
than granting cluster access — but neither is a deliberate allowlist. The contrast
is the point: `platform/infra/modules/eks` drives both flags from variables and
`environments/dev/platform.tfvars` deliberately leaves
`eks_public_access_cidrs` unset with a comment requiring a per-operator
`TF_VAR_…` value. Files: the two `eks.tf` above plus their `variables.tf`. No
current package owns either tree.

**FOLLOWON-E — shared Kubernetes pod/container baseline (medium, 244 records, 20
manifests, unowned).** No `securityContext`, writable root filesystems, root or
low UIDs, capabilities not dropped, no seccomp profile, mutable tags / no digest,
automounted service-account tokens, missing limits and probes. Each alone is
minor; together they remove the guardrails that contain a compromised container.
Should be one baseline change (plus per-manifest overrides), not 244 edits.
Heaviest files: `superplane/src/superplane-api/deploy/*.yaml` (59),
`agent-context/manifests/*` and `kubernetes/*` (~80),
`agent-factory/agent/k8s/image-prepull-daemonset.yaml` (13),
`gateway/k8s/deployment.yaml` (12). Crosses several packages' trees, so it needs
an explicit owner before anyone edits shared manifests.

**FOLLOWON-F — workflow input/ref shell injection (medium, 41 records, 24
workflow files).** Replace `${{ inputs.* }}`, `${{ github.event.inputs.* }}` and
`${{ github.ref }}` inside shell source with step-level environment variables;
validate slugs, numeric IDs, account IDs, paths, image tags and revisions as data;
and use arrays for optional CLI arguments. `seed-hosted-tenant.yml` additionally
needs parameterized SQL (for example `psql` variables with safe literal quoting)
rather than a generated SQL heredoc. The owner must cover the listed `.github`
workflow files as one consistency change and add negative tests containing quotes,
command substitutions, newlines and option-looking values. `_deploy-eks.yml`
must validate `cluster_name`, `namespace`, and `module` before using them as data
and must be tested through an authorized cross-repository `workflow_call` path.
Dispatch/caller authorization is defense in depth, not the repair.

**FOLLOWON-I — cross-repository blanket secret inheritance (medium, 8 records,
8 client workflow templates).** Files:
`modules/agent-factory/client-workflows/.github/workflows/call-agent-architect.yml`,
`call-agent-developer.yml`, `call-agent-operations.yml`, `call-agent-pm.yml`,
`call-agent-product.yml`, `call-agent-pt-superpower.yml`,
`call-agent-reviewer.yml`, and `call-skill-agent.yml`, plus the eight matching
called workflows under `.github/workflows/` when declarations are needed. The
templates are explicitly copied into other organization repositories, call
`aws-innovate/adp/.github/workflows/*@main`, and use `secrets: inherit`; caller
secrets therefore cross into code selected by a mutable branch ref. Remove blanket
inheritance, audit active secret references in each callee, declare and explicitly
map only required secrets (the scanned callees otherwise source app credentials
from AWS Secrets Manager), and pin each called workflow to an immutable reviewed
revision. Add a static test that rejects `secrets: inherit` and mutable refs in
these cross-repository templates while leaving same-repository record 343 distinct.

**FOLLOWON-A — unverified remote-payload execution (medium, 10 records).**
`modules/agent-factory/actions/setup-beads/action.yml:46` pipes a fetched
third-party release into a shell on the runner with no checksum or pinned
version; `modules/gateway/cli/install.sh` (6) and three bootstrap scripts do the
same for operator installs. Fix is pinning plus checksum verification.

**FOLLOWON-B — untrusted XML feed parsing (low, 6 records).** Only the
Superplane `src/superplane-api/app/services/scanner.py` occurrences belong here:
they parse arbitrary third-party arXiv/RSS responses. Move that package path to
`defusedxml`, retain malformed/oversized-feed handling, and run its feed-parser
unit tests plus the six exact Semgrep checks. Gateway indexes 10316–10318 are
separate constrained false positives: `work_routes.py` parses at most 8 KiB from
a fixed AWS STS TLS endpoint with redirects and environment proxy trust disabled,
and denies transport or parse failures. FOLLOWON-B does not edit gateway files.

**FOLLOWON-D-IMDSV2 — gVisor node IMDSv2 enforcement (medium, 1 record).** File:
`platform/infra/gvisor-nodegroup.tf:37`. Platform-infra ownership only. Set launch
template metadata options to require IMDSv2 while preserving the current hop
limit and endpoint settings. A compromised pod/process can otherwise turn SSRF
into node-role credentials through IMDSv1. Review the Terraform plan for launch
template replacement, roll one node first, verify bootstrap/workloads and IMDSv1
rejection, then roll the group. Validate with `terraform fmt`, `terraform
validate`, the platform plan, and the exact `CKV_AWS_79` scan.

**FOLLOWON-D-DEFAULT-SG — deny-all default VPC security groups (low, 3
records).** Files: `modules/domain-apps/cyber/infra/vpc.tf:16`,
`modules/domain-apps/superplane/infra/workspaces/network.tf:50`, and
`platform/infra/modules/networking/main.tf:7`; ownership stays with the cyber,
Superplane, and platform-infra trees respectively. Adopt each VPC's default SG
into Terraform and remove all ingress/egress rules. Before apply, inventory ENI
attachments and import/state ownership to avoid disrupting an accidental user;
then verify zero attachments, an empty-rule plan, and no replacement. Validate
each package's Terraform checks and the three exact `CKV2_AWS_12` records.

**FOLLOWON-D-SECRET-ROTATION — coordinated secret rotation (low, 15 records).**
Exact files: `modules/agent-factory/webhook-ingress/infra/engine-command-signing.tf`
and `modules/agent-factory/webhook-ingress/infra/secrets.tf` (engine/webhook
owner, indexes 318–323); `modules/domain-apps/cyber/infra/peering.tf` (cyber
owner, 324); `modules/gateway/infra/modules/cognito/github_idp.tf` and
`modules/gateway/infra/modules/cognito/main.tf` (gateway identity owner,
325–328); `modules/research/gbrain/terraform/main.tf` and
`modules/research/gbrain/terraform/modules/rds/main.tf` (gbrain owner, 329–330);
and `modules/source-control/gitlab/infra/secrets.tf` and
`modules/source-control/gitlab/infra/ssm.tf` (GitLab owner, 331–332). These resources hold HMAC/signing material, webhook/API/OAuth tokens,
test credentials, and database/break-glass credentials; values are intentionally
not copied here. Define producer/consumer ownership, dual-key or grace-period
semantics, reload behavior, rollback and audit alarms per package before adding
rotation schedules. Validate staged rotation in each package, acceptance of the
new value during overlap, rejection of the old value after expiry, Terraform
plans, and all 15 exact `CKV2_AWS_57` records. This is a design/rollout item, not
a drop-in `rotation_rules` edit or a single cross-tree implementation assignment.

**FOLLOWON-G — gbrain immutable image references (medium, 1 record, unowned).**
Files: `modules/research/gbrain/terraform/modules/storage/main.tf:39` and
`modules/research/gbrain/terraform/main.tf:68`; ownership is limited to the
gbrain Terraform tree. Publish a unique build tag or digest, make the ECR
repository immutable, and pass that immutable reference to Fargate instead of
`:latest`. Plan migration for the currently deployed task definition and preserve
a known-good digest for rollback. Validate Terraform, the exact `CKV_AWS_51`
record, a failed same-tag overwrite, and a task definition resolved to the
expected digest. Cyber's analogous record stays with S17; the four shared
platform-module records stay with S21 because they require a coordinated release
and consumer-tag contract.

**FOLLOWON-H — lock the agent-context self-managed Terraform backend (medium, 1
record, unowned).** Files: `modules/agent-context/deploy.sh:289`,
`modules/agent-context/deploy.sh:309`, and
`modules/agent-context/terraform/backend.tf:1`. Both deployment branches run
`terraform init -upgrade` without a backend configuration, so a fresh operator
deployment can initialize the S3 backend without DynamoDB locking. Require a
module-specific backend configuration that includes the state bucket, key,
region, encryption, and lock table, pass it on both initialization paths, and
fail before apply when it is missing. Validate both deployment modes, a fresh
initialization, concurrent lock contention, and the exact CKV_TF_3 record. Keep
the change inside the agent-context deployment/Terraform tree.

### Routed to existing owners

**`iam-wildcard-policy` (74) — policy-level split.** Every scanner record was
reviewed against its complete policy block at `fb4bc620`; records covering the
same block retain distinct `(artifact, result_index)` keys and rule-specific
rationales in JSON.

| Policy blocks | Recs | Verdict and evidence | Owner |
|---|---:|---|---|
| Two self-hosted-runner implementations: boundary, base, services | 26 | medium, open — the attached policies make broad IAM/KMS/Secrets Manager permissions effective; `sts:AssumeRole` has no runner-side role allowlist | S14 |
| DynamoDB/Secrets Manager KMS service grants outside cyber | 24 | medium, open — key-policy `Resource = "*"` is required, but service `CreateGrant`/cryptographic use lacks caller, via-service, encryption-context, and grant constraints | S12 |
| Gateway CloudWatch Lambda and platform EKS-node ECR policies | 3 | medium, open — account-wide log read/write and repository reads exceed the functions' named resources | S12 |
| Cyber DynamoDB KMS service grant | 3 | medium, open — same unconstrained service/grant condition gap in the cyber-owned key | S17 |
| CloudWatch KMS key policies | 15 | none, required — KMS `Resource = "*"` denotes only the attached key and log use is account/region constrained | S20-reviewed |
| Gateway Bedrock policy | 1 | low, documented — list APIs require `*`, but `GetInferenceProfile` could be split onto profile ARNs; impact is metadata only because invocation is already scoped | S20-reviewed |
| Cognito `sts:TagSession` identity policy | 1 | none, inert — the role has no `AssumeRole` permission, so `TagSession` alone cannot create or retag a session | S20-reviewed |
| Agent-context Resource Explorer inventory | 1 | low, documented — all operations are read/search/list and intentional account-wide discovery; several Tagging APIs cannot be resource-scoped | S20-reviewed |

The 56 open records route to exactly one existing owner as additional evidence,
not three ambiguous owners: S14 owns both runner policy trees, S12 owns the
agent-worker/gateway/platform execution policies, and S17 owns the cyber key.
The 18 closed records are location-specific required, inert, or documented
read-only behavior; no rule-wide suppression is proposed.

`s20-ownership-evidence.json` and `s20-ownership-contracts.json` make the
ownership review self-contained. They bind all 21 issue numbers and exact titles
to the immutable topology and freeze the contract projection at SHA-256
`c89a617572317c1a6ee8774d2736123eedb87bcded9d7f977411ade15610f3aa`.
The evidence partitions all 396 open records by
`(tool, artifact, result_index)`: 62 map
to an existing owner and 334 are validated as unowned follow-ons after comparison
with S01-S19. Every partition carries its record-key digest, complete scanner-file
set, and any additional repair-scope files. Every open record is evaluated by
finding group, rule ID, and repository path: mapped records must resolve to exactly
one contract, while allegedly unowned records must resolve to none. A path overlap
alone does not conflate distinct work, such as an image refresh and pod hardening.

| Mapped domain | Records | Existing owner contract |
|---|---:|---|
| Non-runner vault and worker KMS/service policies | 24 | `[Security 2026-09-21] S12: Bind vault credentials and worker authority to the verified run` |
| CloudWatch worker and platform node execution policies | 3 | `[Security 2026-09-21] S12: Bind vault credentials and worker authority to the verified run` |
| Both self-hosted runner IAM trees | 26 | `[Security 2026-09-21] S14: Constrain CI runner IAM escalation in both infrastructure trees` |
| Shared CodeBuild administrator role | 1 | `[Security 2026-09-21] S14: Constrain CI runner IAM escalation in both infrastructure trees` |
| Cyber KMS and ECR worker infrastructure | 4 | `[Security 2026-09-21] S17: Secure cyber worker script execution and tenant-scoped object access` |
| Shared platform release ECR policy | 4 | `[Security 2026-09-21] S21: Integrate fixes, reconcile AWS Security Agent results and verify the complete run` |

All 62 mapped records must match exactly one domain. The CodeBuild record keeps
`needs-followon` because remediation remains open, but its owner is the existing
S14 work package rather than an unowned follow-on. The hash-pinned projection is
the fail-closed fallback when the original plan object is absent. If that object is
available, optional verbatim validation additionally extracts every quoted path
scope, applies longest-match resolution, and rejects conflicting ownership.

**`iam-administrator-access` (2) → split.**
`platform/infra/modules/codebuild/main.tf:48` attaches `AdministratorAccess` to a
shared role used directly by the ten platform projects listed above and exported
through `platform/infra/modules/codebuild/outputs.tf:1` to the five agent-context
projects wired at `modules/agent-context/terraform/main.tf:267`. The in-repo
`TODO: Scope this down` → **S14** (medium) must cover all 15 consumers and preserve
their package-specific ECR, S3, Logs, EKS-describe, Secrets Manager and scan-upload
requirements. Reducing this role to only ECR push and logs would break source,
layer, scan, and other documented build paths. The independently scoped gbrain
role is not a consumer and is not part of this CKV_AWS_274 record.

`platform/release-infra/main.tf:59` is **intended behavior** (info): it runs
full-platform Terraform, is isolated by account and GitHub environment with an
OIDC `sub` condition pinned to `repo:aws-e/adp:environment:<env>`, and is
documented in-file as deliberately administrative — not to be described as
least-privilege.

## Observation for S10/S16 (not an S20 finding)

While reviewing `sql-construction` I noticed
`modules/gateway/src/internal/status_callback_routes.py:132-142`: when a status
callback omits `tenant_id`, the tenant predicate is **left unconstrained** so an
asset update is not scoped to a tenant. This is deliberate and documented as a
pre-rollout compatibility path (the worker image that sends `tenant_id` ships
separately), it is logged, and the file already flags tightening the `else` branch
as a follow-up. Recorded here only because it is a tenant-isolation boundary and
overlaps gateway identity work (S10) and chat/artifact ownership (S16) rather than
anything S20 owns. Not counted among the 860.

## Handoff to S21

- Complete disposition inventory for all 860 records: `s20-dispositions.json`,
  reconciling exactly with no unaccounted record.
- **No suppressions or baseline entries were created**, so nothing here silently
  removes future coverage.
- `needs-followon` records are **open**, not closed: 4 high, 307 medium and 24
  low. Of these, 334 are validated-unowned records and one CodeBuild record maps
  to existing owner S14. This includes 41 workflow records in FOLLOWON-F, 8 cross-repository secret
  records in FOLLOWON-I, 244 Kubernetes records in FOLLOWON-E, one gbrain ECR
  record in FOLLOWON-G, and the independent IMDSv2 (1), default-SG (3),
  secret-rotation (15), and agent-context backend-locking (1) scopes.
- Existing-owner mappings are also **open**: 62 medium records — the 61
  `routed-existing-owner` records plus the shared CodeBuild administrator record
  already assigned to S14. Totals are S12 (27), S14 (27), S17 (4), and S21 (4).
- `accepted-risk-*` records are posture decisions with stated reasons, offered for
  confirmation or reversal — not closures.
- Verdicts are anchored to scanned commit `fb4bc620`; re-check any record whose
  file changed after it.
- IAM triage is complete at policy-block level: no record retains a multi-owner
  label or a deferred per-policy verdict. Remediation stays with the single
  existing owner recorded for each open item.


## Validation

Current-fix reference: `agent/issue-5619` at the assigned revision plus this
controller repair; the six files listed at the top are the complete S20 triage
evidence.

Run the focused checks from the repository root:

```bash
(cd modules/gateway && uv run --frozen --extra dev ruff check \
  ../../docs/security/runs/2026-09-21/triage/validate.py)
python3 -m py_compile docs/security/runs/2026-09-21/triage/validate.py
python3 docs/security/runs/2026-09-21/triage/validate.py
python3 docs/security/runs/2026-09-21/triage/validate.py --require-canonical
python3 docs/security/runs/2026-09-21/triage/validate.py --require-ownership-plan
(cd modules/gateway && uv run --frozen --extra dev pytest -q \
  tests/proxy/test_provider_rejection_settlement.py \
  tests/orchestration/test_policy_worker_failure.py \
  tests/orchestration/test_shared_attempt_boundaries.py)
git diff --check
```

Repair-run results at this assigned revision: Python compilation, JSON parsing,
the normal 860/860 reconciliation, canonical provenance, hash-pinned contract
validation of all 396 open records, scanned-source CodeBuild assertions,
secret-pattern scanning, and `git diff --check` pass. Negative-path checks reject
both a digest-mismatched projection and a digest-valid projection with an incorrect
S12 selector. The gateway regressions report 18 passed and 14 skipped;
the skips are PostgreSQL-backed cases because this checkout has Python 3.13 while
`pgserver` supports Python <3.13.

The normal validator reconciles 860/860 records, recalculates every aggregate and
README group row, and verifies that all 62 existing-owner mappings belong to
exactly one of six explicit domains. It independently validates the remaining 334
open records as unowned follow-ons, including exact record-key digests and file
sets. It also checks the 5/1/10 CodeBuild split and shared-role consumer boundary
and asserts the repaired workflow reachability, cross-repository secret boundary,
backend, ECR, EKS, gateway XML, and split follow-on dispositions.

`--require-canonical` directly compares the 860 projections and verifies 13 unique,
disjoint historical suppressions when the canonical object or a trusted local copy
is available. In this checkout it instead passes through the hash-pinned
independent evidence chain described under Completeness:
the current projections equal the audited `428eac3b...` revision, and its immediate
child attests an independent comparison with `3193c78b.../findings.json`, including
all 234 Semgrep records, all 626 Checkov records, and the disjoint 13 historical
suppressions.

`--require-ownership-plan` fails closed unless the frozen
`s20-ownership-contracts.json` projection is present and hash-valid. The projection
verifies all S01-S21 identities against the immutable topology object, requires all
62 existing-owner records to match exactly one group/rule/path contract selector,
and checks every one of the 334 proposed-follow-on records against every contract
selector with zero matches. Passing `--ownership-plan <path>` with
`--ownership-sha256 <trusted-digest>` additionally checks the verbatim source plan:
path scopes are extracted from every definition, existing owners resolve by longest
match, and a validated-unowned file may not overlap any S01-S19 scope.
