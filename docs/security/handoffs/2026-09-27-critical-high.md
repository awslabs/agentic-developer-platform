# Critical and High security remediation handover

> **Continuation update:** See [the two-hour remediation outcome](2026-09-27-critical-continuation.md) for the 14:44 UTC inventory, remaining work and logging incident. The contents below describe the earlier handoff.

Date: 2026-09-27 UTC
Repository: `aws-e/adp`
Epic: https://github.com/aws-e/adp/issues/6492

## Outcome and unfinished commitment

**The remediation is incomplete. Fixes have not been applied for all 59 Critical CVEs.** Some Critical vulnerabilities have fixes in tested candidate images, but there is no verified reduction in the number of unique Critical CVEs remaining in the live scope. Do not describe candidate fixes, merged source changes, or successful builds as completed live remediation.

The user requested completion within three hours, 08:06:55–11:06:55 UTC. That objective was not met. Deployment and investigation holds were explicitly lifted. Existing authorization covers fixes, publication, merges and controlled deployments; it does not replace verification and acceptance checks.

## Authoritative totals and their limits

The latest workload inventory is from **10:50:54 UTC**, reconciled at 11:01:06 UTC. No newer inventory was collected for this handover.

| Measure | Critical | High | Open occurrences |
| --- | ---: | ---: | ---: |
| Frozen active baseline | 59 | 389 | 3,231 |
| Conservative open total | **59** | **377** | **2,861** |
| Observed digests only; incomplete closure scope | 56 | 359 | 2,613 |
| Historical union of scanned scopes | 64 | 425 | 4,123 |

The verified reduction is **12 unique High CVEs and 370 occurrences**. Unique advisory totals are deduplicated; occurrences track affected image/package findings and must not be confused with unique CVEs or GitHub issue counts.

DeepWiki remains desired but has no verified current runtime imageID. Its **248 prior findings are carried forward**, explaining why the observed-only total cannot be used as the open total. All 32 observed active digests have scan mappings, but coverage still has 9 missing runtime imageID rows, 34 incomplete owner chains and 7 desired-template rows without active observations. Old images attached to active pods, including init images, still contribute findings.

These totals cover point-in-time EKS image/package inventory. They are not a complete count of source, static-analysis, secret, external-compute or Dependabot findings. Zero unresolved advisory bundles remain in the reviewed baseline.

## Dependabot is not yet reconciled

Dependabot alerts were **not separately reconciled into the totals above**. The latest observed GitHub push notice reported **2 Critical and 3 High Dependabot alerts**; this is a historical notice, not a fresh alert API inventory. Some identifiers may overlap container findings. Do not add these counts to 59/377 or assume they are already included.

Next owner must export current open Dependabot alerts, retain alert number, CVE/GHSA aliases, package/ecosystem, manifest, installed range and patched version, and match canonical advisory IDs against the container register. Report both unique advisory union and source-specific occurrences. Keep unmatched Dependabot alerts as additional open defects. If access is unavailable, document the coverage gap rather than inventing a consolidated total.

## Verified live progress

| Component | Latest observed result | Important limitation |
| --- | --- | --- |
| Gateway | New digest scanned 0 Critical /50 High raw; 0/49 after exact zlib unaffected review | Original rollout, migrations and real-token smoke passed. Later snapshot had 4 desired/updated and 3 ready; latest readiness needs refresh. Old authority-probe gateway remains. |
| MCP | Fixed candidate digest observed; 0/50 raw, 0/49 reviewed; one updated ready replica | Some old image exposure remains attached to active pods. |
| LiteLLM | Fixed candidate digest observed; 0/50 raw, 0/49 reviewed; one updated ready replica | Some old image exposure remains attached to active pods. |
| Chat | New digest observed and scanned 0/0 | Does not establish closure for other components. |
| ARC runner | New digest observed and scanned 0/105 | ARC controller upgrade acceptance is separate and unverified. |

Exact image/config digests and scan bindings are in the final inventory evidence; do not deploy from mutable tags or copy a config digest as an image manifest digest.

## Changes delivered and remaining promotion work

Merged PRs: #6529, #6530, #6531, #6532, #6533, #6534, #6535, #6536, #6537, #6538, #6540, #6542, #6547, #6551, #6556.

| Workstream | Completed evidence | Remaining work |
| --- | --- | --- |
| Curl, #6540 | Authenticated curl 8.22.0 source; OpenSSL and GnuTLS compatibility; 1,835 and 1,831 tests; QUIC suites; Git/TLS refusal fixtures; 54 exact candidate occurrence dispositions | Integrate/promote applicable images and verify live digests. TLS-SRP/RTMP compatibility changes are documented. |
| Rsync, #6547 | Authenticated 3.5.1 release; Debian integration; 272 nonroot, 341 root/TCP and bidirectional mixed-version tests; 24 exact dispositions | Included in final SkyPilot candidate; live acceptance outstanding. |
| SkyPilot SSH, #6551 | Authenticated Debian backport, coordinated client/server/SFTP packages; real UID1000 passwd entry; native SSH/Git/SCP/SFTP and API fixtures | Promote final image through #6552 and verify live rollout. Other SSH High findings remain. |
| KEDA, #6530 | 2.21 chart/CRD/IRSA migration; three candidate images scanned 0/0 | Complete authentication inventory and cluster acceptance before claiming live closure. |
| ARC controller, #6534 | Controller 0.14.2 rebuilt with updated Go/modules, candidate 0/0 | CRD server-side upgrade and live controller acceptance remain. |
| Metrics-server, #6535 | Candidate 0/0 pinned | Live promotion/acceptance remains. CoreDNS candidate 0/0 was tested but minor migration not promoted. |
| Worker, #6538 and #6540 | Terraform 1.15.7 rebuilt with Go 1.26.8; AWS CLI 2.37.4/Python 3.14.7; Node 24.21/npm 11.20; curl overlay raw 24/106, reviewed 0/76 | Ensure normal image build retains repairs, promote and verify live digest. |
| DeepWiki, #6558 | Dependency candidate built and runtime-tested; details below | Draft remains; finish validation, publication and controlled deployment. |

The table is not proof that every Critical CVE has a fix. Produce an explicit per-CVE coverage matrix from the conservative register: affected workloads/packages, remediation owner/story, fixed source/image evidence, tests, deployment status and remaining blockers. Any Critical without matching evidence must remain marked as needing a fix or investigation.

## Immediate next action: SkyPilot promotion

PR: https://github.com/aws-e/adp/pull/6552
Worktree: `/workspaces/projects/security27-skypilot-promotion`
Head: `8b4670a39d02b70f0cd4d8d3fc1de66f16af7163`

**Status refreshed while writing this handover:** PR remains OPEN; full Superplane domain tests passed at **11:20:06 UTC**. Other returned executable checks passed; Terraform Plan was skipped. This supersedes the earlier handoff's pending-CI status. There were also 97 passing focused tests. Recheck current head/checks and mergeability before merging; merge was not performed as part of this documentation task.

Final published candidate manifest digest:

`sha256:f45198dd5cf103f72f797a1aeb13bab50d6223f950c59d63e6d671915e7cda5a`

Config digest: `sha256:9789da812b3829ff601b837ea0b1ade224aa750a5cde263f127fc377fdc534dd`.

Raw scan **32 Critical /184 High**, reviewed candidate **0 Critical /135 High** after authenticated curl, rsync and SSH evidence. Raw scanner results are retained. No SkyPilot live rollout has been verified. Use the release-lock reference in the PR as the full repository/digest deployment reference.

## DeepWiki candidate: #6558

PR: https://github.com/aws-e/adp/pull/6558 (OPEN, draft)
Worktree: `/workspaces/projects/security27-deepwiki-deps`
Branch: `security27/deepwiki-dependencies`
Head: `1be5774ea446e0cfa7b67cd41be1d5ed93485c81`

Pins PostCSS 8.5.18, nanoid 3.3.18, sharp 0.35.4 and setuptools 81.0.0. Removes the residual setuptools package tree before reinstalling. Normal Dockerfile now imports tested curl packages and licenses from an immutable build-input artifact; this artifact is not a deployable application image.

Use **`security27/deepwiki:dependencies-clean`**, not the superseded `dependencies-final` image.

- Root descriptor: `sha256:78a44243c46039dfdbda40c7e74259b7b067d1d5d20f8aac261cd97c447ba1ab`.
- Config digest: `sha256:4196fbcf3d4c82401e37e7e22beb6fb5ea06587fb918e83b7d2ddd82e7eb4ec1`.
- Raw scan: **25 Critical /118 High**, five fewer High than the earlier curl overlay.
- Exact curl review passed with 54 candidate dispositions, leaving **1 Critical /88 High before separately binding the existing SSH disposition**. Do not label this clean candidate reviewed 0 Critical until that binding is recorded.
- Packaged tar/API/UI/cache, stdlib, Node hostname and full SSH fixtures passed. SSH binary hash: `040d6afdb0e3194a3afca73981e4be57c8acb5898ed4108ea2b051232d0001c1`.
- Final native frontend recheck remains; prior sharp/PostCSS tests preceded the final setuptools-only cleanup.
- Jaraco-context 5.3.0 (`GHSA-58pv-8j8x-9vj2`) and wheel 0.45.1 (`GHSA-8rrh-rw8j-w5fx`) still match. Investigate actual installed locations/provenance. Do not remove scanner metadata to hide findings.
- At handover, the returned GitHub check was Gateway Hardening Tests: success. This is not evidence of full component acceptance.

Candidate is **not published or deployed**. Finish binary-bound SSH review, native frontend tests, no-new-C/H comparison and applicable CI before publication/promotion. Retain raw findings and review evidence separately.

## Evidence and continuation tools

Merged inventory evidence: https://github.com/aws-e/adp/pull/6556, under `docs/security/runs/2026-09-27/final-live/` on the merged branch. The current main working tree may not contain the latest merged files; avoid overwriting unrelated changes to update it.

Local evidence, which must be preserved or securely copied if changing machines:

- `/workspaces/projects/security27/final-live-reconciled/`: `summary.json`, `active-register.json`, **`conservative-open-register.json`**, `scan-receipts.json`.
- `/workspaces/projects/security27/live-refresh-20260927T105050Z/`: inventory script, collection receipt, scan targets, workload image rows and coverage gaps; raw resource files marked private.
- `/workspaces/projects/security27/reconcile_final_live.py`: final reconciliation logic; inspect path assumptions before reusing with a new run.
- `/workspaces/projects/security27/scan_local.py`: archive/config-bound Syft/Grype scan helper.
- `/workspaces/projects/security27/deepwiki-dependencies-clean-scan/`: clean archive, SBOM, raw scan and receipt.
- `/workspaces/projects/security27/deepwiki-deps/`: runtime results, build logs and `curl-clean-review.json`.
- `/workspaces/projects/security27-curl-upstream`, `security27-rsync`, `security27-skypilot-openssh`: source/evidence worktrees beneath `/workspaces/projects/`.
- `/workspaces/projects/security25/story-closure-20260926.md` and `critical-high-priority-20260926.md`: earlier local trackers.

Example existing scan/review commands, run from the DeepWiki worktree and use a new output directory for new scans:

```bash
python3 /workspaces/projects/security27/scan_local.py IMAGE NEW_OUTPUT_DIRECTORY
python3 platform/security/curl-8.22.0/review-candidate.py SCAN_DIRECTORY --output NEW_REVIEW_JSON
python3 modules/agent-context/images/deepwiki/security/openssh/check.py security27/deepwiki:dependencies-clean security27/openssh:backport
```

The scanner uses Syft 1.52, Grype 0.119 and the frozen database at `/workspaces/projects/security25/live-audit-20260927/db`. Preserve that comparable baseline; identify database versions explicitly if adding a fresh advisory scan.

## Ordered continuation and closure criteria

1. Build the per-CVE remediation coverage matrix for all 59 Critical CVEs; expose those with no implemented fix. Reconcile current Dependabot alerts as a separate source.
2. Recheck and merge SkyPilot #6552 when current checks and mergeability permit, then perform controlled deployment and functional acceptance.
3. Complete DeepWiki #6558 validation, publication and deployment. Recover a verified desired-workload/runtime digest mapping.
4. Promote remaining tested candidates with component-specific migration and rollback checks; remediate residual Critical and High findings for which no candidate fix exists.
5. Refresh live workload-to-digest collectors into a new timestamped directory. Include active/init containers, desired templates and owner chains. Retain unresolved desired workloads instead of treating missing runtime IDs as removal.
6. Scan every new observed digest; bind image manifest/platform/config identities to SBOM and raw scan receipts. Rebind exact vendor/backport dispositions to actual binaries.
7. Reconcile against the frozen baseline and update Epic/stories. Close a unique CVE only when every in-scope affected occurrence is fixed, verified unaffected or demonstrably retired, with coverage gaps accounted for. Report candidate and live results separately.

## Environment and operational continuity

Target AWS account **879318057152**, region **us-east-1**, EKS cluster **adp-dev-eks-cluster**. Confirm active credentials with `aws sts get-caller-identity` before deployment. Follow `docs/adp-platform-deployment/deploy-with-agent.md` and its canonical quickstart, using the existing authorization and deployment state.

Do not overwrite unrelated `/home/ubuntu/adp` changes: `.adp-deploy-state.json`, `docs/adp-cli/getting-started.md`, `data/investigations/`. Concurrent work is repairing CI/deployment identities; do not duplicate or overwrite it. Use isolated worktrees. No subagents are authorized for this continuation.

Isolate HOME, BG_CONFIG_DIR and synthetic test credentials; never print credentials. The task Docker auth directory `/workspaces/projects/security27/docker-auth` was removed at cleanup; reauthenticate if publishing. Preserve restrictive permissions on private inventory artifacts. Never touch the unrelated `adp-5433-validation-control-plane` container.
