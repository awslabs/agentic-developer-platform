# Repository and deployment coverage audit

## Boundary and result

Audited source: `a3ffb462b29e6b6c88e32b0619ae04a3a2b5df78` in `/work/repo`,
2026-10-29. The design branch is `agent/issue-9283`; no proposed infrastructure,
workflow, runtime or live target was changed during this design assignment.

The exhaustive tracked-artifact selection covers **13,629 tracked entries**:
**3,689 candidates** are individually enumerated in [inventory.tsv](inventory.tsv),
and the other **9,940 non-candidates** are counted by repository root in
[inventory-summary.json](inventory-summary.json). All **166 GitHub workflows**
are included. **1,064** files contain deployment/build-entry text; that count
includes tests and documentation and is not a claim of 1,064 executable entry
points. Every candidate has a component and an explicit disposition. There are
zero unclassified candidates under the published selection rules.

This is an inventory of the complete declared scope, not a sample of modules.
It does **not** assert that every unrelated artifact's internals were manually
reviewed or that all paths are active in a live installation. Semantic review
focused on GitLab, shared EKS/network/IAM, ARC precedents, delivery entry points
and the denied services; the remainder has an explicit exclusion/context class.

## Reproduce and interpret the inventory

From the repository root, with Python 3 and the baseline Git object available:

```bash
python3 docs/design-notes/9283-gitlab-runners/audit.py --check
python3 docs/design-notes/9283-gitlab-runners/validate.py
git diff --check
python3 scripts/check-public-docs.py
```

The audit command reads immutable Git blobs, not ambient generated/untracked
files, and checks exact TSV/JSON equality. Expected result: the summary above and
exit 0. It needs no AWS access, network or credentials. The publication checker
checks tracked/staged docs, so stage intended artifacts before the final check.
Regeneration mode (without `--check`) uses the Codex `apply_patch` utility; it is
not needed for read-only validation. Do not replace the baseline when reproducing
this historical audit; a later design revision can intentionally rebase it.

Selection is a union, evaluated across **every** tracked path/text blob:

1. Terraform/tfvars/HCL, YAML, shell/PowerShell, Bicep, Docker/Containerfiles,
   Make/Task/Jenkins files, buildspecs, language package/build manifests.
2. Every tracked file under infra, k8s, manifests, releases, image/images and
   scripts directories; all root CodeBuild, environment and composite-action files.
3. Path or case-insensitive content references to GitLab, ARC/deploy pools,
   secret-store CSI, NodePool/NodeClass and the network-policy APIs/controller.
4. Build/deploy invocation text (Terraform, Helm, kubectl, Docker, CodeBuild,
   npm/make build, workflow dispatch/call and launcher references).
5. Any non-blob tracked entry, so a submodule cannot silently disappear.

Each TSV row records path, component, disposition, artifact-name match, **all**
matching reference/entry line numbers, Git object ID and content SHA-256. It does
not copy source contents, private deployment configuration or secret values.
Generated artifacts, ignored files, remote scripts and environment-specific
resources are not tracked source; their unresolved live coverage is listed below.
Tests/manifests under unrelated modules are enumerated, not silently discarded.

## Component ledger

| Class | Count | Disposition |
|---|---:|---|
| instance | 38 | Entire GitLab module; change only additive registration/discovery/CA contract |
| instance-config | 2 | Existing GitLab environment and backend configuration; separate runner backend |
| identity | 13 | Existing isolated admin, Product/tenant mapping and related tests/schema |
| gitlab-ci | 5 | Existing GitLab workflows, composite config and regression checks |
| runner-precedent | 72 | Agent-factory Terraform/ARC/deploy pool; patterns only, no copied authority |
| platform | 377 | Shared infra, launchers, EKS/IAM/network, build machinery and tests |
| delivery | 284 | Workflows/composite actions/CI scripts, including unrelated payload exclusions |
| build-support | 85 | Root CodeBuild and scripts, image scanning/publication helpers and fixtures |
| gateway-boundary | 524 | Gateway/CloudFront/DB/identity context; no CI-to-ADP trust |
| denied-target | 45 | Dev Box infrastructure and artifacts; negative target, not runner reuse |
| agent-boundary | 350 | Existing agent/webhook GitLab path; not GitLab CI execution |
| other-module-excluded | 1,290 | Other modules' build/deploy/configuration artifacts, explicitly out of scope |
| reference-only | 349 | Documentation/history/design/tracking material, not deployment proof |
| fixture-excluded | 57 | Tests/experiments, not production runner entry points |
| repository-context | 96 | Other root-level references/build/configuration |
| other-artifact-excluded | 102 | Remaining unrelated tracked configuration/build artifacts |

The TSV supplies the per-file enumeration for every row, including all 38
GitLab module files. Counts and membership are machine-checkable, not hand-picked.

## Deployment and build entry-point map

The table connects entry families to actual consumers. It complements, rather
than substitutes for, the exhaustive per-file ledger.

| Entry / existing source | Behavior found / disposition |
|---|---|
| [GitLab apply](../../../.github/workflows/gitlab-infra-apply.yml#L3), [destroy](../../../.github/workflows/gitlab-infra-destroy.yml#L3) | Manual `workflow_dispatch`, main-only job, instance Terraform directory/backend; protected identity. Add sibling runners workflows; do not reuse state or hard-coded environment assumptions. |
| [Instance root](../../../modules/source-control/gitlab/infra/main.tf#L28), [versions](../../../modules/source-control/gitlab/infra/versions.tf#L5), [EC2 bootstrap](../../../modules/source-control/gitlab/infra/user_data.sh#L49) | AWS-only provider/remote VPC state, package bootstrap; no current runners root. Instance changes must remain EKS-provider independent. |
| [Portal install](../../../modules/source-control/gitlab/scripts/configure-portal-sso.py#L1), [key provision](../../../modules/source-control/gitlab/scripts/provision-jwt-keys.py#L1), [admin seed](../../../modules/source-control/gitlab/scripts/seed-reconciler-token.py#L1), [product runbook](../../../modules/source-control/gitlab/docs/product-homes.md#L25) | Existing manual side effects and isolated administrator bootstrap; new registrar reuses the authority boundary, not arbitrary instance execution. |
| [Home reconciler manifest](../../../modules/source-control/gitlab/infra/home-reconciler.yaml#L1), [portal runtime](../../../modules/source-control/gitlab/infra/portal-runtime.yaml#L1) | Manually rendered/applied gateway-adjacent products; not GitLab CI runner manifests. Retain. |
| [GitLab tests](../../../.github/workflows/gitlab-integration-tests.yml#L1), [test config](../../../.github/actions/gitlab-e2e-config/action.yml#L1) | Existing webhook/agent roundtrip, not runner acceptance. Config helper now documents explicit runtime masking; open #4995 is risk history, not evidence that the exact historical code still exists. |
| [Platform apply](../../../.github/workflows/platform-infra-apply.yml#L1), [plan](../../../.github/workflows/platform-infra-plan.yml#L1) | Shared EKS/controller/IAM/build authority. Plan workflow's job is manual-main gated despite PR path triggers; do not assume a PR gets a real cloud plan. Runner PR adds offline checks. |
| [Agent factory root](../../../modules/agent-factory/infra/main.tf#L54), [ARC module](../../../modules/agent-factory/infra/modules/arc-runner/main.tf#L26), [deploy pool](../../../modules/agent-factory/infra/deploy-runner-pool.tf#L1) | Terraform Helm ARC and separate deployment execution pool; `deploy-runner-pool` is Terraform, not a workflow of that name. Read-only precedent. |
| [NodePool gVisor](../../../platform/infra/nodepool-gvisor.tf#L39), [packer](../../../platform/infra/nodepool-packer.tf#L1) | Two Auto Mode NodePool examples; do not reuse aggressive consolidation or mutate shared default NodeClass. |
| [Networking endpoints](../../../platform/infra/modules/networking/main.tf#L327), [node IAM](../../../platform/infra/modules/iam/main.tf#L72) | Existing S3/ECR/STS/SM endpoints and broad shared node role; endpoint presence in source is not reachability/authorization proof. Use new scoped role. |
| [ARC build workflow](../../../.github/workflows/arc-runner-build.yml#L10), [buildspec](../../../codebuild/bs-arc-runner.yml#L1), [image](../../../platform/arc-runner/Dockerfile#L1) | Push/manual image build precedent; no GitLab runner image recipe presently. Does not rebuild for this documentation path. |
| [CodeBuild action](../../../.github/actions/codebuild-run/action.yml#L1), [projects and per-project IAM](../../../platform/infra/modules/codebuild/main.tf#L58) | Reuse immutable source-per-attempt and scoped role pattern; S3 adds exact optional bootstrap build project/recipe, not buildspec overrides for arbitrary tenant jobs. |
| [Trusted deployment](../../../.github/actions/trusted-deployment/action.yml#L1), [load config](../../../.github/actions/load-deploy-config/action.yml#L1) | OIDC/environment trust and target selection. Recheck selected exact role/account after selection; no ambient credential fallback. |
| [Gateway deploy](../../../.github/workflows/gateway-deploy.yml#L5) | Relevant if S1 minimally extends the shared admin client; existing gateway path trigger remains, no new CI gateway trust. |
| [Root launcher](../../../deploy.sh#L1), [core orchestrator](../../../platform/scripts/deploy-all.sh#L1), [teardown](../../../platform/scripts/undeploy.sh#L1), [release workflow](../../../.github/workflows/adp-release.yml#L1) | Enumerated installation/release entry points, not a current GitLab-runner installer. Keep runners opt-in/manual; no silent launcher/release inclusion. |
| Other workflows, module infra/k8s/releases/images/buildspecs, scripts and package builds | Every tracked candidate appears in TSV with exclusion or context class; none is treated as an undocumented runner installer. |

Related issue review included the full parent #3717, three child bodies and
comments, the epic amendment, and security histories #8847/#4995/#2312. Native
sub-issue API readback confirmed exactly #9284, #9276 and #9285 under #9283.
The related broker PR #9198 was open, with its design inspected at
`aa02b1f25815d64ac26a577cccf749bf1770ee4c`; it is not assumed merged/deployed.

Recent workflow metadata checked: the latest returned GitLab apply run was
[this successful workflow](https://github.com/aws-e/adp/actions/runs/29166958300)
at `007dfa399f5402e2949262438a0fda805f724d9b`; the latest returned platform apply
was [this successful workflow](https://github.com/aws-e/adp/actions/runs/34714334062)
at `035790593b1578fe3f2ee8bae90293f97475e5b5`. These prove reported workflow
conclusions only. Inputs, current endpoint health, live permissions, actual node
image and acceptance were not verified, and are not inferred from them.

## Design coverage audit

| Required spec area | Covered / gap and accountable next action |
|---|---|
| Description | Architecture opening and §2: group-only CI, separate state, no privileged jobs/ADP trust; Q1 prevents unconditional S2 readiness |
| Impact | §3–5: secret exposure, lateral access, cross-tenant jobs/cache, resource caps and nonzero managers; no measured cost claim |
| Design | §§2–5: reference schemas, private TLS, rotation/recovery, manager/job identities, admission, node/network contracts; explicit Q1 vendor compatibility gap |
| Deployment | §6 and backlog: manual apply/destroy, optional shared image-build prerequisite, exact target identity, ordering, cleanup and retained CA/images |
| Validation | §7 and backlog: every EC/AC/SCN preserved, independent negative oracles, controls/deadlines, pre-review versus live checks |
| Storage | Existing SM/SSM and cache bucket fit; no new relational schema; inspect existing Product identity rather than create duplicate tenant storage |
| Cross-phase integration | S1 secret schema/private route → S2 runtime loader → S3 activation/readback; one module integration owner, delivery owner for live |
| Public disclosure | Only repository paths, public upstream artifacts, fictional/config placeholders; no raw issue bodies, target IDs, logs or credentials copied |

## Unresolved coverage, not hidden assumptions

- Q1 is a known **design feasibility** gap: supported policy enforcement for
  actual standalone runner pods, controller failure behavior and target
  ApplicationNetworkPolicy API. The requested tracked inventory is already done;
  it is not assigned to a future implementation story.
- Live AWS state, IAM union/resource policies, imported certificate provenance,
  private URL redirect behavior, actual GitLab/runner version compatibility,
  image digests and the test groups remain unverified. Named owners and blocking
  phases are in architecture §7.
- All tracked scripts/configuration are selected by the published exhaustive
  union; runtime-generated or remotely fetched artifacts cannot be enumerated
  from this checkout alone. S3 must bind deployed versions/receipts to the live
  manifest. The inventory does not promise discovery of opaque commands loaded
  from private configuration or of untracked infrastructure.
- Main was refreshed to `8d01a52bfc321d00ad6bbe45568c43a4bec6fb76` before
  publication. Its six changed paths relative to this branch are L5 Transform
  deployment tests and gateway internal-domain dispatch/store/tests, not the
  GitLab/EKS/runner/deployment contracts reviewed here. The inventory deliberately
  remains bound to its stated snapshot rather than silently changing provenance.
- Project/Module/Feature catalog lineage is still unmapped in the existing
  issues; delivery owner must resolve registered IDs, not invent them. Native
  parent relations and acceptance IDs are preserved.

## Checks for this design change

The final design PR records the tested revision and command results. Checks are
inventory regeneration/equality, Python syntax, Markdown local paths/anchors and
line references, upstream chart SHA-256, publication scan and Git whitespace.
Infrastructure tests/Helm deployment/live pipelines are **not run**: implementation
and cloud changes are outside this assignment. No positive claim in this audit
stands in for EC-01–EC-04 live evidence.
