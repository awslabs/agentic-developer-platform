# S21 — Consolidated evidence ledger (read-only snapshot)

Work package **S21** of the 2026-09-21 security scan. Issue [#5620](https://github.com/aws-e/adp/issues/5620);
daily epic [#5599](https://github.com/aws-e/adp/issues/5599); AWS epic [#5677](https://github.com/aws-e/adp/issues/5677).

## What this document is, and what it is not

This is the **read-only evidence preparation** that S21 explicitly permits while its
implementation predecessors are still open. It is a dated snapshot of what can be
substantiated today: issue states, pull-request states, committed disposition reports and
the scan inventories already published.

It is **not** the S21 integration step. No release pin, manifest, infrastructure reference,
scanner baseline or suppression was changed to produce it, no build or scan was run, and
nothing was merged or deployed. The scan day is **not** reconciled and this ledger must not
be read as a clean result.

- **Snapshot taken at:** 2026-09-24T11:44Z–12:40Z
- **Repository revision read:** `9afe1423b57318f1eda9d2ee0c44aeb25cac8417` (`main` at snapshot time)
- **Scan inventory read from:** `3193c78b167f583eed5026c582ab58794c88c34e`
  (`docs/security/runs/2026-09-21/findings.json`, generated `2026-09-21T00:34:56Z`)
- **AWS plan read from:** `d6091ba4d`
  (`doc/ai-dlc-engine/security-agent-35547077368/work-packages.json`)

### The four states this ledger keeps apart

Collapsing these into "done" is the specific failure this document exists to prevent.

| State | Means |
|---|---|
| **Code delivered** | A change exists and merged into `main`. |
| **Validated** | The owner ran specific checks and recorded their results. |
| **Deployed protection** | The control is running in a named environment. |
| **Acceptance resolved** | The scan day's acceptance for that finding is signed off. |

Across this whole snapshot, **exactly one package reaches "deployed protection"** on
retrievable evidence (AWS A01 #5653, where the auth rollout was applied to the dev gateway
and rejection responses were captured). **No package reaches "acceptance resolved."** The AWS
epic itself records "Code delivery does not imply live rollout", and its execution constraints
authorise "merge only; no deployment".

---

## 1. Counts verified against live GitHub

Re-checked directly rather than carried over from the issue text.

| Claim in the assignment | Verified state at snapshot | Verdict |
|---|---|---|
| AWS epic #5677: 17/24 packages closed | 24 sub-issues, 17 CLOSED, 7 OPEN | **holds** |
| Daily epic #5599: 12/21 direct packages closed | 22 sub-issues = 21 stories (S01–S21) + nested epic #5677; 12 stories CLOSED | **holds** |
| Combined 29/45, nested epic counted once | 17 + 12 = 29 closed of 24 + 21 = 45 | **holds** |
| 5 named predecessors still open | S12 #5611, S14 #5613, S15 #5614, S16 #5615, S18 #5617 all OPEN | **holds** |

Of S21's 17 named blockers, **12 are closed and 5 remain open**. The five open ones are the
live gate on S21's integration work.

The 12/21 figure was independently recounted issue-by-issue across #5600–#5620 (12 CLOSED,
9 OPEN). An intermediate check during this exercise suggested 15/21; that was wrong and is
recorded here so it is not propagated.

**Eleven packages were never dispatched at all.** **Five** daily packages (S10 #5609, S11 #5610,
S12 #5611, S13 #5612, S18 #5617) and six AWS packages (A03 #5655, A09 #5663, A11 #5666,
A12 #5668, A13 #5669, A14 #5670) have **zero comments** — 5 + 6 = 11, confirmed not-started rather than
merely unlinked. Three of these are hard S21 gates (S12, S18, plus S14/S15/S16 which are in
flight). On the AWS side the untouched set is dependency-critical: A03 is the deepest node
(depends on A02, A06, A12, A14) and the A10→A11→A12/A13→A14→A03 chain is stalled behind a
draft PR.

**A closed package is not a fixed vulnerability.** The AWS plan states this as a constraint
("A closed issue is not evidence that a vulnerability was fixed"), and two closures in this
snapshot are explicitly *validated-no-change* rather than code fixes (see §4).

### AWS epic structure — independently recomputed

Recomputed from the machine-readable plan rather than trusted from prose. All four
structural claims reproduce exactly:

| Claim | Recomputed | Verdict |
|---|---|---|
| 47 findings | 47 `primary_finding_ids`, **all 47 unique** — no double-count | **holds** |
| 24 execution packages | 24 entries | **holds** |
| 23 dependencies | 23 `depends_on` edges (plus 8 separate `contributes_to` edges) | **holds** |
| 9 independent roots | `dependency_levels[0]` = A01, A04, A07, A08, A16, A19, A21, A23, A24 | **holds** |

Six packages (A02, A04, A19, A21, A23, A24) carry **zero primary finding IDs** — they are
satellite tasks that feed a parent via `contributes_to`. Their composite contributions must
be preserved rather than reassigned; the plan forbids "invented per-ID subcomponent mapping".

Six source records were absorbed into shared owners, not separately dispatched:
#5654→#5653, #5659→#5658, #5661→#5658, #5665→#5664, #5657→#5666, #5667→#5666.

---

## 2. The inventories overlap — do not add them up

The four inventories in play are **different views of the same scan**, plus one older
backlog. Adding them would claim 33 + 860 + 16 + 47 = 956 distinct vulnerabilities, which is
wrong. Each derivation below is reproduced from its source record.

| Inventory | Composition (verified) | What it is |
|---|---|---|
| **33 source-rated occurrences** | 21 high + 12 critical = 33; by group 14 container + 10 code + 9 npm = 33 | Today's explicitly-rated occurrences |
| **860 S20 dispositions** | 234 Semgrep unrated + 626 Checkov unrated = 860; `unaccounted: 0`, 860 unique keys | Findings the tools returned **without** a rating |
| **16 older primary mappings** | 1 critical + 15 high, still OPEN, from a 29-ticket legacy list (13 of which are CLOSED) | Prior scan-day backlog |
| **47 AWS findings** | 47 unique IDs across 24 packages | Separate AWS Security Agent engine run |

Three further qualifications:

- The **860 are unrated, not unrated-therefore-safe**. Their assessed severities are 4 high,
  368 medium, 184 low, 303 none, 1 info.
- The **13 explicitly suppressed critical/high Semgrep records are excluded from the 860**
  by design, and were retained as historical dispositions rather than reclassified. They are
  *not* dispositioned anywhere in this snapshot (see §5).
- The 33 and the 47 partially describe the same code. The AWS plan links packages back to
  earlier nightly issues via `earlier_nightly_issues`, which is the join to use — not
  severity or file matching.

---

## 3. Release pins and image provenance — read, not changed

S21 is the **only** owner of the shared release lock and image references. This section
records what exists today so the integration step is mechanical; nothing here was edited.

### Current state of `modules/domain-apps/superplane/releases/superplane.lock.yaml`

| Entry | State |
|---|---|
| `images.skypilot-api` | **Resolved**: `sha256:de41a5c6…5019`, from `berkeleyskypilot/skypilot:0.12.0` |
| `pending_images.superplane-executor` | No digest — blocked on reviewed Python base + remote build |
| `pending_images.superplane-api` | No digest — no build has run yet |
| `pending_images.superplane-controller` | No digest — no build has run yet |
| `pending_images.superplane-platform-monitor` | No digest — no build has run yet |
| `schema.status` | `unverified`; `single_head: true`; live migration gate not discharged |

So the earlier observation of **four pending Superplane images and unverified schema status
still holds** at this snapshot. By construction `pending_images` entries carry no `digest`
key, and `tests/test_lock.py` asserts that — a placeholder digest would be worse than a
visible gap, because a fabricated `sha256:` looks authoritative.

### Two integration gaps this snapshot surfaces

These are the substantive findings of the exercise. Both are **S21's own work** and both are
currently unreconciled:

1. **S03's approved image is not in the lock — and a digest swap alone would be wrong.** S03
   (#5602, CLOSED) concluded on a derived linux/amd64 image
   `sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec`, built on the
   **SkyPilot 0.12.3** base with setuptools 81.0.0. The lock still pins the **0.12.0** digest
   (`sha256:de41a5c6…d75019`, `berkeleyskypilot/skypilot:0.12.0`).

   **Correction to an earlier revision of this ledger:** it implied the integration is a single
   lock edit that propagates. It is not. The derived artifact exists only as a **local OCI
   layout** — its recorded `image_reference` is the placeholder
   `"S21-owned-release-repository@sha256:f8d893…de55ec"`, every Syft/Grype command in its
   evidence targets `oci-dir:/tmp/S03-skypilot-oci`, and its provenance JSON contains no
   publish/push/registry step. **Substituting the digest while leaving the upstream Docker Hub
   provenance in place would record a false origin for the image.** S03 itself enumerates five
   prerequisites that must be done *together* (`S03-disposition.md:219-243`):

   1. **Publish** the OCI manifest and blobs to the S21-owned release repository without
      media-type conversion, and **verify the published manifest digest** is exactly
      `sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec`.
   2. Set `images.skypilot-api` to that digest **and** set `image_sources.skypilot-api.registry`
      and `.repository` to the final publication location — the control plane's
      `local.skypilot_image` combines all three, so no second image literal is needed in
      `infra/control-plane/config.tf`.
   3. Refresh the provenance comments in `k8s/40-skypilot-api.yaml` (its executable value stays
      the SSM-supplied `REPLACE_WITH_SKYPILOT_IMAGE`).
   4. Replace `tests/skypilot_image_facts.json` with the committed derived fixture
      `evidence/S03-skypilot_image_facts-derived.json`, changing only its placeholder repository
      name. On `main` that fixture still reads `berkeleyskypilot/skypilot:0.12.0`.
   5. Update the deliberate literal guard in
      `infra/control-plane/tests/lock_pin.tftest.hcl` to the same digest.

   Publishing under a different repository does not change the manifest digest, provided the
   manifest bytes and referenced blobs are preserved. **Not verified:** whether any out-of-band
   publication has occurred since 2026-09-22 — no repository artifact records one and no
   registry was queried here.
2. **S01's built controller image is not in the lock.** S01 (#5600, CLOSED) recorded
   `adp-superplane-controller@sha256:d87cf6355d…ce38ed` with a build run, a SUCCEEDED
   validation build and four runtime checks passing, and states the digest "is available to
   S21 without requiring a lock-file edit". `superplane-controller` remains in
   `pending_images` with "no build has run yet" — which is now stale as a description.

**Consequence:** the S01/S02/S03 remediation is real and evidenced at the image level, but
the **shared release still resolves to unfixed pins**. This is exactly the "code delivered,
not deployed" distinction, and it is unresolved.

### 3.1 S02's rebuilt-image acceptance — satisfied, and exactly how far it reaches

An earlier revision of this ledger recorded S02's rebuilt-image and SBOM criterion as **not
satisfied**. That was wrong: the acceptance was published on
[#5601 (2026-09-23)](https://github.com/aws-e/adp/issues/5601#issuecomment-5793303212), a day
**before** this snapshot, so it was available and simply missed. Recorded here with its exact
boundaries, because this is the one package in the container set with a registry-verified digest.

| Item | Evidence |
|---|---|
| Implementation | PR #5783, merged `27a5b943cb4081152069af76d9f5a90b18c918db`; corrected-head ARC tests passed in run 35846485819 |
| Real image build | Existing Superplane CodeBuild lane succeeded in **run 35847435952** |
| Verified ECR image | `adp-superplane-api@sha256:f1897a677fa7a5e21b7a1aa8c0ab6a2a03b37b66a98443a06b200d72f4a9b259`, tagged with that merge commit |
| SBOM | **Syft 1.52.0** catalogued the exact image **directly from the registry**: 179 packages, 63 of them Python. Contains PyJWT 2.14.0 and cryptography 50.0.1; **ecdsa, python-jose, rsa and pyasn1 absent** |
| Advisory re-scan | **Grype 0.119.0** against that image's SBOM (DB built `2026-09-23T06:31:39Z`): **zero matches** for `GHSA-wj6h-64fc-37mp`, ecdsa or python-jose |
| Disposition | **Fixed by removing the vulnerable dependency path** — not suppressed, not accepted-risk |

**What this acceptance does not establish**, stated by its own author and preserved here so the
row cannot be read as more than it is:

- **Not whole-image cleanliness.** The full Grype scan of that same image still reports
  **16 critical, 70 high, 68 medium, 10 low, 59 negligible** across *other* packages. Those are
  not suppressed or closed by this single-advisory disposition, and ECR's own completed scan is
  unchanged.
- **Not a deployment.** This was local validation against the registry image; no deployment and
  no additional paid scan. So S02 stays `deployed: no`.
- It resolves the **one** rated occurrence S02 owns (`ecdsa 0.19.2`, `GHSA-wj6h-64fc-37mp`,
  selector `grype | superplane/grype/modules-domain-apps-superplane-src-superplane-api.sarif |
  ri=94`) — the single container row in §8.B attributed to S02.

This is also the **only** Superplane image in the set with a *published, registry-verified*
digest. S03's derived image and S01's controller image are built but not published (see below),
which is why S21's integration cannot simply copy digests into the lock.

### 3.2 S03's runtime validation — it was run, and what it covered

An earlier revision of this ledger recorded S03's live startup and persistence as **unverified**.
That was wrong; `S03-disposition.md:152-183` documents the checks, corroborated by
`evidence/S03-derived-image-provenance.json` (`runtime_validation`, 8 passing results):

- The **exact OCI layout** was loaded directly by udocker 1.3.17 — no rebuild or flattened
  substitute. Runtime imports returned `setuptools=81.0.0`, `skypilot=0.12.3`.
- Ran as uid 1000 against disposable **PostgreSQL 16.15** via `SKYPILOT_DB_CONNECTION_URI`;
  required `clusters`, `storage`, `users` tables confirmed in schema `public`.
- Auth: `unauthenticated_health=401`, `authenticated_health=200`,
  `controller_health=healthy version=0.12.3`.
- The maintained **Go controller client** called its real `Health`/`Status` methods through the
  proxy: version 0.12.3, commit `9578bbb6…`, `external_proxy_auth_enabled=true`.
- **State survived restart:** a `s03-restart-proof` user created via `POST /users/create` was
  confirmed through the authenticated API and directly in PostgreSQL; stopping only SkyPilot gave
  `503`, and a new process from the same loaded image returned the same record (`200`).

**Scope limit that keeps `deployed: no` correct:** this was a local disposable udocker plus
disposable PostgreSQL run, not an EKS or cluster deployment. The Docker reproduction script
`evidence/S03-live-validation.sh` requires the not-yet-existing `S03_SKYPILOT_REPOSITORY`. So the
image is **functionally validated but not published and not deployed**.

### ECR tag mutability — 4 records routed to S21 as owner

S20 routed four Checkov `CKV_AWS_51` occurrences (`platform/infra/modules/ecr/main.tf:22-40`)
to **S21** because S21 owns shared release pins and tag semantics. Verified still true on
current `main`: `image_tag_mutability = var.image_tag_mutability`, and the variable's default
in `variables.tf:43` is `"MUTABLE"`. An authorised ECR writer can therefore retarget an
approved tag. **Open, owned by S21, not fixed.**

---

## 4. Daily packages S01–S21

Detailed PR/validation mapping is in §7. Highlights that bear on S21's duty:

- **npm remediation is verifiable in the current tree.** Of the 9 npm occurrences, the
  upgraded versions are present in the committed lockfiles at the snapshot revision:
  `modules/agent-factory/agent` has fast-uri 3.1.8, ip-address 10.7.2, js-yaml 3.15.2;
  `modules/gateway/frontend` has js-yaml 4.3.2, nanoid 3.3.19, undici 7.29.1,
  browserslist 4.29.0. `brace-expansion` resolves to 1.1.21 and 2.1.7 in both. This is
  **code delivered and inspectable**, not a deployment claim.
- **S19 was a coverage fix, not a vulnerability fix.** It closed 14 defects that made a
  broken scan look green — including only 8/17 Grype images producing results while the
  build passed at a ≥50% coverage threshold, Superplane images being invisible to the
  inventory, cfn-nag aborting on all three templates while reporting success, and a raw CVSS
  score outranking the scanner's own rating. **Its corrected tooling is a precondition for
  S21's final scan**; a pre-S19 scan cannot be used as the confirmation run.
- **S19 explicitly deferred baseline authority to S21:** "Global finding exceptions and
  baseline reconciliation remain S21's call."

---

## 5. Residual open items, missing evidence and unavailable artifacts

Recorded as unavailable or open — never as clean.

### Blocking gates (S21 integration cannot start)

Five predecessors remain OPEN: **S12 #5611, S14 #5613, S15 #5614, S16 #5615, S18 #5617**.

### Unresolved items that are S21's own

| Item | State |
|---|---|
| S03's 0.12.3-derived digest into the lock | **Not done** — lock still on 0.12.0 |
| S01's controller digest into the lock | **Not done** — still in `pending_images` |
| 4 × `CKV_AWS_51` ECR tag mutability | **Open**, S21-owned, default still `MUTABLE` |
| Scanner baseline / global exception reconciliation | **Not started**; deferred to S21 by S19 |
| Final one-off confirmation scan | **Not run** |

### Missing or unavailable evidence

| Gap | Status |
|---|---|
| Raw AWS ledger `s3://adp-dev-security-scans-879318057152/security-agent/runs/2026-09-21/` | **Unavailable** — the plan records credentials expired, refresh needs `mwinit`. Not retrieved; not verified clean. I did not attempt credentialed access. |
| 13 suppressed critical/high Semgrep records | **No disposition exists — but the locations are published.** All 13 records carry `locations[].file` and `.line` in the inventory (enumerated in §8.D below), so each is directly reselectable. What is absent is the **suppression justification**: every record's `suppression` array holds only `{"kind": "inSource"}`, with no `justification` field. So the gap is "why was this suppressed", not "where is it". No raw-SARIF recovery is needed to disposition these. |
| 8 Grype CVSS disagreements | **Fully identified.** All 8 carry advisory IDs in the inventory's `advisory` field — `CVE-2019-25210` (helm, CVSS 9.1), `GHSA-248v-346w-9cwc` (certifi 7.5), `GHSA-8r3f-844c-mc37` (protobuf 7.5), `CVE-2020-15778` ×3 (openssh-client/server/sftp 7.8), `GHSA-h395-gr6q-cpjc` ×2 (jsonwebtoken 7.5). Each is native low/medium against CVSS ≥7. Enumerated with fix versions in §8.E. **Correction:** an earlier revision of this ledger reported these IDs as null and required raw-SARIF recovery. That was wrong — it read a field named `vulnerability_id`, which does not exist in these records. The data was published all along. |
| 335 of 860 S20 records verdicted `needs-followon` | **No follow-on issues appear to have been filed.** A search for the 11 proposed follow-on packages (FOLLOWON-A…I) returned no matching issues. The largest, FOLLOWON-E, covers 244 k8s pod-hardening records. **These 335 records currently have no GitHub owner.** |
| Scanner tooling gaps flagged *to* S21 by owners | **Partly open, and two earlier entries here were wrong — see §5.1 below for the reconciliation against the actual workflow.** The supported limitation is cfn-nag's *parse aborts*, not its triggering. Bandit is pinned and configured in the snapshot workflow; cfn-nag does run in the on-demand scan. |
| `.grype.yaml` review date | **Lapsed.** The file's own header sets "Next review: 2026-08-22"; it is 2026-09-24 and 189 ignore entries are in force. Each entry removes findings from SARIF output entirely, so a lapsed review silently suppresses. S21 owns this. |
| 16 older primary tickets | All 16 re-checked live and **all still OPEN** (#4701–#4730). None closed by this scan day. |

### 5.1 Scanner configuration, reconciled against the actual workflow

An earlier revision of this ledger repeated owners' local-environment observations as if they
were repository-level coverage gaps. Checked directly against
`.github/workflows/security-scan.yml` at the snapshot revision `9afe1423`, two of the three
claimed gaps do not exist. What matters here is that **overstating a scanner gap is as harmful
as hiding one** — it would cast false doubt on the final confirmation scan.

| Earlier claim | Verified state at `9afe1423` | Verdict |
|---|---|---|
| Pinned `bandit[sarif]==1.7.9` and `.banditrc` absent from the checkout | `BANDIT_VERSION: "1.7.9"` (line 60); `pip install bandit[sarif]==${{ env.BANDIT_VERSION }}` (line 592); `bandit -r . --ini .github/security/.banditrc` (lines 596–597). The config file **exists** and sets `skips: [B101, B311]` plus `exclude_dirs`. | **Withdrawn as stated.** There is no root-level `.banditrc` — the committed config lives at `.github/security/.banditrc`, which is exactly what the workflow passes. A root-only check misreports it as absent. But see the separate verified defect below: the config is passed with the wrong flag. |
| cfn-nag is not PR-triggered, so CFN linting rests on local cfn-lint alone | The whole workflow is `on: workflow_dispatch` only — an authenticated, **dispatch-only one-off producer**. cfn-nag is Job 6 *inside it*, gated only by `if: inputs.scan_scope != 'superplane'`. | **Withdrawn as a coverage gap.** S21's final scan is an on-demand dispatch, so cfn-nag runs in it. Absence of a PR trigger is not absence of coverage for this scan. |
| Checkov allowlist inert because the workflow passes `--directory .` | The workflow passes `--directory .` **together with** `--config-file .github/security/checkov.yml` and `--baseline .github/security/checkov-baseline.json` (lines 213–216). The config itself documents this deliberately: "Keep local runs aligned with security-scan.yml, which explicitly passes `--directory .`". Skips come from `skip-check:` in the config, which `--directory` does not override. | **Not supported as stated.** `--directory .` widens the scanned path set; it does not disable the committed `skip-check` list or the baseline. A18's design item 6 may still be unimplemented on its own terms, but the shared allowlist is not rendered inert by this flag. |

**A distinct, newly verified scanner defect (not the one originally claimed).** While checking
the Bandit claim above, the config turned out to be passed with the wrong flag: the workflow
uses `bandit --ini .github/security/.banditrc`, but `--ini` is parsed with `configparser` and
requires an INI `[bandit]` section, whereas that file is YAML (its own header says
"Format: YAML (bandit >=1.6)"). Reproduced against `bandit[sarif]==1.7.9`: `--ini` emits
`WARNING Unable to parse config file ... or missing [bandit] section` and then **drops the
`skips` and `exclude_dirs`**, while `-c` applies them. The warning is non-fatal, so the job
stays green. Test-directory exclusion is partly recovered by the workflow's separate `-x` flag,
but `skips: [B101, B311]` is not applied at all.

This is a **real** finding of the same class S19 addressed — configuration that looks applied
and is not — and it is in S21's scanner-configuration lane. It is the opposite of the original
claim: the config exists and is referenced, but does not take effect. It is not fixed here;
this ledger changes no workflow or config file.

**The limitation that most affects the final scan** — and the reason S19's fix is a precondition
rather than a nicety — is recorded in the workflow's own source comment on the cfn-nag job:

> "No `continue-on-error`: a parse abort must surface. All three templates aborted unscanned on
> 2026-09-21 and the job still reported success."

So on the scan day, cfn-nag produced a green job while scanning nothing. That is a
trustworthiness defect in the *result*, not in the trigger. S19 closed it; the final scan must
run on the corrected tooling and must confirm the templates actually parsed.

A second narrower cfn-nag limitation, verified rather than inferred: `--input-path` covers only
`modules/agent-factory/agent-worker-image/aws/` (3 templates). Three further CloudFormation
templates — `modules/gateway/src/auth/cfn_templates/aws_role_v1.yaml`, `aws_role_v2.yaml`,
`aws_role_deploy_v1.yaml` — fall outside it. Checkov's `cloudformation` framework over
`--directory .` does reach them, so this is reduced coverage by one tool, not absent coverage.

`.grype.yaml` remains a real S21 item: its own header says "Re-evaluate quarterly. Next review:
2026-08-22", and 189 `vulnerability:` ignore entries are in force at a date past that review.
Each entry removes findings from SARIF output entirely, so a lapsed review suppresses silently.
**20 of the 189 carry no `package:` scope**, so they suppress their advisory globally across every
package and image — the broadest category, and the first that S21's baseline reconciliation
should re-justify per location.

### S20 disposition profile, for reference

Of 860: 464 `S20-reviewed`; 27 → S12; 27 → S14; 4 → S17; 4 → S21; 335 → unfiled follow-ons.
By verdict, 335 `needs-followon`, 61 `routed-existing-owner`, 175 accepted-risk across three
tiers, 60 `not-security`, and ~225 across fourteen distinct false-positive categories. The
false-positive verdicts are narrowly typed (`false-positive-bound-params`,
`false-positive-fixture`, `false-positive-callsite-controlled`, …) rather than blanket rule
suppressions, which is the disposition style S21's acceptance requires.

---

## 6. The final integration sequence, for when predecessors land

Order matters; each step's evidence feeds the next. This is a prepared sequence, not an
authorisation to run it.

1. **Wait for the five open gates** (S12, S14, S15, S16, S18) to merge with reviewed
   evidence. Do not infer closure from workflow success.
2. **Disposition the two enumerated sets** — no recovery step is needed, contrary to an earlier
   revision of this ledger. The 8 CVSS disagreements already carry advisory IDs and the 13
   suppressed records already carry file and line (§8.E, §8.D). What each of the 13 still needs is
   a **per-location justification**, since only `{"kind": "inSource"}` is recorded. Decide the 8 on
   their merits — native rating versus CVSS — noting 3 have no fix version available.
3. **Publish before pinning, then integrate in one reviewed commit.** For S03, the derived image
   must be **published first** and its registry digest confirmed to equal
   `sha256:f8d893cf…de55ec`; only then set `images.skypilot-api` **together with**
   `image_sources.skypilot-api.registry`/`.repository`, refresh the provenance comments in
   `k8s/40-skypilot-api.yaml`, swap in the derived `skypilot_image_facts` fixture and update the
   literal guard in `lock_pin.tftest.hcl` (the five coupled steps in §3). Changing only the digest
   while leaving the Docker Hub provenance would record a false origin. S01's controller image
   `sha256:d87cf635…ce38ed` needs the same publication check before `superplane-controller` leaves
   `pending_images`. Re-assert the shape tests (`tests/test_lock.py`, `tests/lock_pin.tftest.hcl`,
   `tests/test_skypilot_startup_contract.py`). **No placeholder digest** for the images with no
   build. S02's `adp-superplane-api@sha256:f1897a67…4a9b259` is already registry-verified (§3.1).
4. **Reconcile the shared scanner disposition**: refresh `.grype.yaml`'s lapsed review — starting
   with the **20 package-unscoped entries**, which suppress globally — and decide the 4 S21-owned
   `CKV_AWS_51` records on their merits. Per-location evidence only — no whole-rule suppression, no
   baselining away unresolved findings. Also fix the Bandit config invocation (`-c`, not `--ini`,
   for a YAML config) so its `skips` actually apply, and confirm cfn-nag's three templates parse
   instead of aborting green (§5.1). A final scan on the current invocation would under-report.
5. **File owners for the 335 orphaned records** (the 11 FOLLOWON packages) so no finding is
   closed by absence of an owner.
6. **Run the final one-off scan** with S19's corrected inventory and tooling, recording
   commit, image digests, coverage per target, artifacts and per-finding outcome.
7. **Leave #5620, #5599 and #5677 open** with named owners for every residual item and for
   pending rollout.

---

## 7. Per-package evidence detail

Every pull-request number below was resolved from `--head agent/issue-<n>` and confirmed with
`gh pr view`; none was inferred from prose. "Validated" reports what the owner recorded, not
an independent re-run by me.

### 7.1 Daily packages S01–S21

| Pkg | Issue | State | PR | Merge | Validated | Deployed | Notes |
|---|---|---|---|---|---|---|---|
| S01 | 5600 | CLOSED | 5725 | `76c91c24` | partial | no | Owner disclosed Docker/Syft/Grype unavailable — "the analysis predicts what a scan will report; it is not a scan". Controller image *was* later built (digest in `S01-image-evidence.json`) but the lock still says "no build has run yet". |
| S02 | 5601 | CLOSED | 5783 | `27a5b943` | yes | no | API suite 1203 passed / 25 skipped. **Rebuilt-image + SBOM criterion IS satisfied** — see §3.1; an earlier revision of this ledger wrongly recorded it as unsatisfied. Scoped to the original advisory only; the image is not globally clean and is not deployed. |
| S03 | 5602 | CLOSED | 5724 | `c742c087` | yes | no | **Not a doc-only PR** — 13 files incl. `images/skypilot/build_oci.py` (+400) and `tests/test_skypilot_vendored_packages.py` (+433) with its fixture (+471). It correctly touched **no lock/manifest/infra file** — those are S21's by design. Live startup, auth, controller-client and restart-persistence checks **were** run and documented (§3.2). Derived image is built but **not published**. |
| S04 | 5603 | CLOSED | 5774 | `ebde4b40` | yes | no | 2060/2067 jest pass, 7 failures pre-existing; 30 new tests. |
| S05 | 5604 | CLOSED | 5704 | `9614f881` | yes | n/a | Validated-as-by-design: the 3 Semgrep hits persist by rule design; per-location disposition, no rule suppressed. |
| S06 | 5605 | CLOSED | 5701 | `ccff2bea` | yes | no | `npm audit --include=dev` high 4→0. Reviewer approval hit a GitHub 422 self-review limit; published as COMMENTED. |
| S07 | 5606 | CLOSED | 5706 | `32c06c1a` | yes | no | 5 high → 0 critical/0 high across 582 deps; `tsc --noEmit` clean. |
| S08 | 5607 | CLOSED | 5753 | `4275564c` | yes | n/a | Reproduced with the pinned Bandit 1.7.9 + repo `.banditrc`; `nosec skipped: 0`. No suppression added. |
| S09 | 5608 | CLOSED | 5765 | `c2553b5f` | yes | no | `bandit -t B102` 1 → 0 results. Flagged to S21: pinned `bandit[sarif]==1.7.9` and `.banditrc` absent from checkout. |
| S10 | 5609 | **OPEN** | none | — | no | no | **Zero comments — never dispatched.** Identity gate cited by S15 and by AWS A01/A03/A05. |
| S11 | 5610 | **OPEN** | none | — | no | no | **Zero comments — never dispatched.** |
| S12 | 5611 | **OPEN** | none | — | no | no | **Zero comments — never dispatched. Hard S21 gate.** Also owns 27 S20 records. |
| S13 | 5612 | **OPEN** | none | — | no | no | **Zero comments — never dispatched.** |
| S14 | 5613 | **OPEN** | **5850 open** | — | yes | no | CI clean (3 success / 2 skipped), **no review submitted**. Owner: "none of the narrowed IAM has been applied to any account". Owns 27 S20 records. **S21 gate.** |
| S15 | 5614 | **OPEN** | **5851 open** | — | yes | no | CI green (3 success) but **CHANGES_REQUESTED** by the operator — the blocker is the review verdict, not CI. Owner: "code awaiting review — not merged, not deployed, not verified in a cluster". Egress containment currently unowned. **S21 gate.** |
| S16 | 5615 | **OPEN** | **5857 open** | — | yes | no | **CI red: 2 FAILURE (Lint, Test)**, 7 success, 5 skipped. Owner: "nothing closed … pending review and live acceptance". **S21 gate.** |
| S17 | 5616 | CLOSED | 5758 | `eed0fbe7` | yes | no | 143 passed / 7 pre-existing `libmagic` ImportErrors verified identical on clean main. gVisor, image build and CI wiring disclosed as **open prerequisites**. |
| S18 | 5617 | **OPEN** | none | — | no | no | **Zero comments — never dispatched. Hard S21 gate.** |
| S19 | 5618 | CLOSED | 5756 | `42fbe6cd` | yes | n/a | 1258 + 19 + 98 tests pass. Corrected tooling is the precondition for S21's final scan. |
| S20 | 5619 | CLOSED | 5702 | `956a60da` | partial | n/a | Doc-only; no module linters apply. 860 records dispositioned; 74 IAM-wildcard records **routed, not resolved**, to S14/S12/S17; accepted-risk and needs-followon verdicts left **for S21 to confirm or overturn**. |
| S21 | 5620 | **OPEN** | none | — | — | — | No implementation PR. This ledger is read-only preparation only. |

### 7.2 AWS packages A01–A24

**17 of 24 closed** (7 open), recounted from live issue state — an earlier revision of this
ledger said 18, which did not match the 17/24 verified in §1. Every closed package explicitly
disclaims live deployment.

| Pkg | Issue | State | PR | Merge | Validated | Notes |
|---|---|---|---|---|---|---|
| A01 | 5653 | CLOSED | 5737 | `6455f8b5` | yes | **The one package with real live evidence** — the auth rollout was applied to the dev gateway (acct 879318057152) and forged-identity 403 / direct 401 captured across replicas. |
| A02 | 5682 | CLOSED | 5786 | `3cb303b0` | yes | 1202 passed / 25 skipped. NetworkPolicy portion blocked on #4999. |
| A03 | 5655 | **OPEN** | none | — | no | **Zero comments — never dispatched.** Deepest node: depends on A02, A06, A12, A14. |
| A04 | 5683 | CLOSED | 5739 | `a5f3570c` | yes | 4018 passed / 11 skipped module-wide. No live secret rotated. |
| A05 | 5656 | CLOSED | 5787 | `058745bc` | yes | 1558 passed / 67 skipped. Owner states **live acceptance is PENDING**. |
| A06 | 5658 | CLOSED | 5790 | `e6e8f322` | yes | 2089 passed. IAM read-prefix narrowing is **code only, not applied**. |
| A07 | 5660 | CLOSED | 5742 | `a38cecbc` | yes | 350 chat + 106 Lambda ownership tests, TS build, CI green. |
| A08 | 5662 | CLOSED | 5721 | `1786f856` | yes | 143 tests pass. Finding `f-a7de2523-…` **should stay open** pending a deployed-agent E2E check. |
| A09 | 5663 | **OPEN** | none | — | no | **Zero comments — never dispatched.** |
| A10 | 5664 | **OPEN** | **5848 draft** | — | partial | **Draft PR**, cannot merge as-is; no submitted review. Blocks A11→A12/A13→A14→A03. |
| A11 | 5666 | **OPEN** | none | — | no | **Zero comments — never dispatched.** |
| A12 | 5668 | **OPEN** | none | — | no | **Zero comments — never dispatched.** Feeds A03. |
| A13 | 5669 | **OPEN** | none | — | no | **Zero comments — never dispatched.** |
| A14 | 5670 | **OPEN** | none | — | no | **Zero comments — never dispatched.** Feeds A03. |
| A15 | 5671 | CLOSED | 5826 | `20612976` | yes | 1342 passed; 6 failures reproduced identically on main. "No deployment contribution." |
| A16 | 5672 | CLOSED | 5762 | `69bd15f4` | yes | 969 tests; terraform fmt/validate clean. Captured logs not yet triaged. |
| A17 | 5673 | CLOSED | 5824 | `c4635f45` | partial | No headline pass count; local skips had masked 2 failures until a py3.11 CI re-run. Live/E2E unrun; `events` table has **no retention policy** now that denials are recorded too. |
| A18 | 5674 | CLOSED | 5767 | `d2646593` | yes | 69 guard tests, mutation-checked. Live acceptance open. Design item 6 (checkov allowlist) deliberately not implemented — **the allowlist is inert because the workflow passes `--directory .`**. |
| A19 | 5684 | CLOSED | 5741 | `aacfabfa` | yes | 28 RBAC manifest tests + 4038 module-wide; guards mutation-tested. |
| A20 | 5675 | CLOSED | 5768 | `70c8d6b4` | partial | **Weakest merged validation** — only 30 infra tests; image/cluster checks disclosed un-runnable. "PR open for review, not deployed." |
| A21 | 5685 | CLOSED | 5755 | `a6fcb67e` | yes | terraform fmt/validate pass; live deployment out of scope. |
| A22 | 5676 | CLOSED | 5785 | `8d9e767e` | yes | 4641 + 1207 + 41 tests pass. **Every environment needs its CA bundle provisioned *before* the TLS default flips**, or the service refuses to start. |
| A23 | 5686 | CLOSED | 5726 | `e9ef11f3` | yes | 15868 gateway tests pass. Guard stays **inert for users until the CLI artifacts are next published**. |
| A24 | 5687 | CLOSED | 5708 | `4457a3b4` | yes | 85 unit tests, cfn-lint and ruff clean. **cfn-nag never ran** (not PR-triggered). Found `budgets:DeleteBudget` is not a real IAM action, so a copied `Deny` silently failed open. |

### 7.3 Rollout blockers that must not be lost at closure

These are recorded by their owners and would silently disappear if closure were inferred from
merge status:

- **A23** — guard inert until `bg-gateway-proxy` CLI artifacts are republished.
- **A22** — CA bundle must be provisioned per environment *before* the TLS default flips.
- **A08** — finding `f-a7de2523-ae6a-4230-8ed7-cf4380d30ade` stays open pending a deployed-agent check.
- **A18** — checkov allowlist inert due to `--directory .`; design item 6 unimplemented.
- **A24** — cfn-nag is not PR-triggered, so CFN linting rests on local cfn-lint alone.
- **A17** — `events` table has no retention policy.
- **S17** — gVisor, image build and CI wiring remain open prerequisites on a *closed* issue.
- **S14** — no narrowed IAM has been applied to any account.
- **S02** — rebuilt-image scan and SBOM criterion unsatisfied on a *closed* issue.

---

## 8. Required per-finding joins

The assignment requires an auditable join for **every** one of the 47 AWS finding IDs, the 33
source-rated occurrences and the 16 older primary tickets. An earlier revision of this ledger
gave only totals and package summaries, which cannot substitute for the per-record mapping.

Each row carries a **stable selector** that reselects the exact record in its source artifact, so
any claim here can be checked independently. Coverage and uniqueness were verified
programmatically rather than by reading — 47 rows / 47 unique finding IDs, 33 rows / 33 unique
selectors, 16 rows / 16 tickets — and the generator asserts those counts, so a silently dropped
record would fail rather than pass quietly.

**"Owner" means the package accountable for the finding, not evidence that it is fixed.** Read
each row together with its owner's state and the per-package evidence in §7.

### A. All 47 AWS Security Agent finding IDs → owner

Source: `work-packages.json` at `7b0004ed`, `packages[].primary_finding_ids`. Every ID appears
exactly once across the 24 packages (checked programmatically: 47 rows, 47 unique IDs). The
`Feeds` column preserves composite contributions — a satellite package contributing to a parent
via `contributes_to` is **not** reassigned away from its own row.

| # | Finding ID | Owner pkg | Issue | State | Blocked by | Fed by | Earlier nightly |
|---|---|---|---|---|---|---|---|
| 1 | `f-042c16e1-fe62-4a70-8bd9-cc283c652004` | A01 | #5653 | CLOSED | - | - | 5609,5612 |
| 2 | `f-1473858b-602e-481f-bf8e-10a166560fd5` | A01 | #5653 | CLOSED | - | - | 5609,5612 |
| 3 | `f-0054d7ee-bbe7-4b3d-8f88-91695dbc247f` | A03 | #5655 | OPEN | A02,A06,A12,A14 | A02,A12,A14 | 5609,5612,5614,5617 |
| 4 | `f-16d7f58c-3049-4afc-8715-99b824be7108` | A03 | #5655 | OPEN | A02,A06,A12,A14 | A02,A12,A14 | 5609,5612,5614,5617 |
| 5 | `f-93db06c3-561a-49cf-8f42-16c66137301c` | A03 | #5655 | OPEN | A02,A06,A12,A14 | A02,A12,A14 | 5609,5612,5614,5617 |
| 6 | `f-4fcee527-02cd-46d8-8036-7672b9375688` | A05 | #5656 | CLOSED | A01,A04 | A04 | 5609,5610,5611 |
| 7 | `f-9289cf29-ffbf-4f94-820e-ad446874fa1a` | A05 | #5656 | CLOSED | A01,A04 | A04 | 5609,5610,5611 |
| 8 | `f-1416bf9f-8e69-4b54-a47f-a64e3ff8af95` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 9 | `f-2c92584b-b7f1-47d3-97ce-68008910aa21` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 10 | `f-3633a91e-c67a-4a45-b255-5701159813f1` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 11 | `f-5065d163-b429-4a57-9d3d-a8b9cec7e30c` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 12 | `f-83ad0918-2ceb-44b3-a153-096829422ed5` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 13 | `f-a9f8dda7-7bce-48fc-a001-a95faffa373a` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 14 | `f-afbff07b-0f5a-406c-8185-3a692aa29858` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 15 | `f-10be6bbc-1e9c-4e97-ac46-b5d56f5d3938` | A07 | #5660 | CLOSED | - | - | 5615 |
| 16 | `f-1c42a409-3bee-41bb-97c8-868d035090e2` | A07 | #5660 | CLOSED | - | - | 5615 |
| 17 | `f-ee68a4da-688e-42aa-b973-854ee12bea34` | A07 | #5660 | CLOSED | - | - | 5615 |
| 18 | `f-a7de2523-ae6a-4230-8ed7-cf4380d30ade` | A08 | #5662 | CLOSED | - | - | 5616 |
| 19 | `f-37867072-eabe-4eaf-9b67-946581e17141` | A09 | #5663 | OPEN | A01,A10 | - | 5610,5611 |
| 20 | `f-5716ea91-dddb-4cb1-9555-7dedb75e9f44` | A09 | #5663 | OPEN | A01,A10 | - | 5610,5611 |
| 21 | `f-5efb8f93-04db-442b-8879-d01dd7bf0bc8` | A09 | #5663 | OPEN | A01,A10 | - | 5610,5611 |
| 22 | `f-7c46ead6-06bf-4726-94a7-b09ea88efbb5` | A09 | #5663 | OPEN | A01,A10 | - | 5610,5611 |
| 23 | `f-32c4047a-643a-45cc-821a-e45ca5586239` | A10 | #5664 | OPEN | A05 | - | 5610,5611 |
| 24 | `f-5726d42e-1699-417a-8cd1-7ef62b29e0f5` | A10 | #5664 | OPEN | A05 | - | 5610,5611 |
| 25 | `f-a265c73e-f2e5-4a41-a7b8-5de3dcd63744` | A10 | #5664 | OPEN | A05 | - | 5610,5611 |
| 26 | `f-f72357f3-911b-4046-94fa-b20a30b3286b` | A10 | #5664 | OPEN | A05 | - | 5610,5611 |
| 27 | `f-169973a3-5eaa-4e21-aab8-2511d308cb38` | A11 | #5666 | OPEN | A10 | - | 5612,5617 |
| 28 | `f-47879254-cca8-4e12-baf8-2db1d18c78f7` | A11 | #5666 | OPEN | A10 | - | 5612,5617 |
| 29 | `f-7aa10bca-c6d1-44e2-9329-acd0e51411e4` | A11 | #5666 | OPEN | A10 | - | 5612,5617 |
| 30 | `f-f070371e-2653-48e9-bac3-32e671285e7f` | A11 | #5666 | OPEN | A10 | - | 5612,5617 |
| 31 | `f-4bb47eec-8e4f-4496-8bf7-59019021e065` | A12 | #5668 | OPEN | A11 | - | 5612,5617 |
| 32 | `f-a16723b6-5efa-465f-9ae9-7e0ce8029013` | A12 | #5668 | OPEN | A11 | - | 5612,5617 |
| 33 | `f-0c88fc4e-8abd-4b23-b913-27360a71a31c` | A13 | #5669 | OPEN | A11 | - | 5617 |
| 34 | `f-0ff37a38-1f7f-4892-8bdc-e1b182995d64` | A13 | #5669 | OPEN | A11 | - | 5617 |
| 35 | `f-37399a05-b05a-463c-8309-21fd16b284ec` | A14 | #5670 | OPEN | A13 | - | - |
| 36 | `f-f9e5efa9-7a0e-4568-b048-c2bf53eab3c3` | A14 | #5670 | OPEN | A13 | - | - |
| 37 | `f-96315bea-5e27-4d48-aacb-ca91301cf971` | A15 | #5671 | CLOSED | A02 | - | - |
| 38 | `f-f27c7448-7885-4cf7-abf5-4b89f96260a3` | A15 | #5671 | CLOSED | A02 | - | - |
| 39 | `f-5c6b7efb-9108-4bad-bc96-bef27072cb92` | A16 | #5672 | CLOSED | - | - | 5617,5618 |
| 40 | `f-80675759-b1d9-4f93-a7a0-4691685aeb12` | A16 | #5672 | CLOSED | - | - | 5617,5618 |
| 41 | `f-93177516-24d1-451e-bfd1-ddd2e3c60048` | A17 | #5673 | CLOSED | A02 | - | - |
| 42 | `f-51343273-7105-40c2-90e7-8011a508e996` | A18 | #5674 | CLOSED | A19,A24 | A19,A24 | 5613 |
| 43 | `f-c47fb5bc-a9e9-4927-972d-768be914c047` | A18 | #5674 | CLOSED | A19,A24 | A19,A24 | 5613 |
| 44 | `f-50fd079e-3650-4184-87ae-2bdd91b19bab` | A20 | #5675 | CLOSED | A21 | A21 | 5618 |
| 45 | `f-b6bc5a6d-433f-4be1-9a72-9b824172c155` | A20 | #5675 | CLOSED | A21 | A21 | 5618 |
| 46 | `f-ac1f6bcf-d79b-4411-a399-25d336eee878` | A22 | #5676 | CLOSED | A04,A23 | A23 | - |
| 47 | `f-fe6aaade-4cf3-4fb2-adbf-4c1942fef222` | A22 | #5676 | CLOSED | A04,A23 | A23 | - |

**Satellite packages carrying zero primary finding IDs:** A02, A04, A19, A21, A23, A24. These are not unowned findings — they are tasks that feed a parent package, and their contribution is recorded in the `Fed by` column of that parent.


### B. All 33 source-rated occurrences → owner

Selector is the stable tuple that reselects the exact record in `findings.json` at `3193c78b`:
`tool | artifact | result_index` for SARIF tools, `npm-audit | project | package` for npm.
Checked programmatically: 33 rows, 33 unique selectors, matching the published 14 container +
10 code + 9 npm split.

| # | Grp | Sev | Advisory / rule | Subject | Fix avail | Owner | Issue | State |
|---|---|---|---|---|---|---|---|---|
| 1 | container | critical | `CVE-2022-32511` | `py3-jmespath 1.0.1-r3` | 1.6.1 | S01 | #5600 | CLOSED |
| 2 | container | high | `CVE-2025-31498` | `c-ares 1.33.1-r0` | 1.34.5 | S01 | #5600 | CLOSED |
| 3 | container | critical | `CVE-2025-3277` | `sqlite-libs 3.45.3-r3` | 3.49.1 | S01 | #5600 | CLOSED |
| 4 | container | high | `CVE-2025-53547` | `helm 3.14.3-r4` | 3.18.4 | S01 | #5600 | CLOSED |
| 5 | container | high | `GHSA-hcg3-q754-cr77` | `golang.org/x/crypto v0.17.0` | 0.35.0 | S01 | #5600 | CLOSED |
| 6 | container | high | `GHSA-r6ph-v2qm-q3c2` | `cryptography 42.0.7` | 46.0.5 | S01 | #5600 | CLOSED |
| 7 | container | critical | `GHSA-v23v-6jw2-98fq` | `github.com/docker/docker v24.0.7+incompatible` | 25.0.6 | S01 | #5600 | CLOSED |
| 8 | container | critical | `GHSA-v778-237x-gjrc` | `golang.org/x/crypto v0.17.0` | 0.31.0 | S01 | #5600 | CLOSED |
| 9 | container | high | `GHSA-wj6h-64fc-37mp` | `ecdsa 0.19.2` | none | S02 | #5601 | CLOSED |
| 10 | container | high | `CVE-2023-36632` | `python 3.10.19` | none | S03 | #5602 | CLOSED |
| 11 | container | high | `GHSA-58pv-8j8x-9vj2` | `jaraco-context 5.3.0` | 6.1.0 | S03 | #5602 | CLOSED |
| 12 | container | high | `GHSA-72hv-8253-57qq` | `jackson-core 2.16.1` | 2.18.6 | S03 | #5602 | CLOSED |
| 13 | container | high | `GHSA-8rrh-rw8j-w5fx` | `wheel 0.45.1` | 0.46.2 | S03 | #5602 | CLOSED |
| 14 | container | high | `GHSA-r6ph-v2qm-q3c2` | `cryptography 43.0.3` | 46.0.5 | S03 | #5602 | CLOSED |
| 15 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/complex-task-chat/recall-at-task-start.ts:171` | - | S04 | #5603 | CLOSED |
| 16 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/invocability-probe/capture-proxy.ts:187` | - | S04 | #5603 | CLOSED |
| 17 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/invocability-probe/gateway-client.ts:86` | - | S04 | #5603 | CLOSED |
| 18 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/lib/artifactGateway.ts:27` | - | S04 | #5603 | CLOSED |
| 19 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/lib/knowledgeBridge.ts:60` | - | S04 | #5603 | CLOSED |
| 20 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/utils/installation.ts:61` | - | S05 | #5604 | CLOSED |
| 21 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/utils/installation.ts:74` | - | S05 | #5604 | CLOSED |
| 22 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/codex-reviewer/src/github.ts:114` | - | S05 | #5604 | CLOSED |
| 23 | code | high | `B324` | `modules/gateway/src/orchestration/deployment_workflow_provider.py:162` | - | S08 | #5607 | CLOSED |
| 24 | code | high | `tmp.gitlab.bandit.B102` | `docs/analysis/cli-uplift-rework/probe_historical_failures.py:34` | - | S09 | #5608 | CLOSED |
| 25 | npm | high | `brace-expansion` | `modules/agent-factory/agent range <=1.1.17 || 2.0.0 - 2.1.3` | yes | S06 | #5605 | CLOSED |
| 26 | npm | high | `fast-uri` | `modules/agent-factory/agent range 3.0.0 - 3.1.5` | yes | S06 | #5605 | CLOSED |
| 27 | npm | high | `ip-address` | `modules/agent-factory/agent range <=10.3.0` | yes | S06 | #5605 | CLOSED |
| 28 | npm | high | `js-yaml` | `modules/agent-factory/agent range 3.0.0 - 3.15.1` | yes | S06 | #5605 | CLOSED |
| 29 | npm | high | `brace-expansion` | `modules/gateway/frontend range <=1.1.17 || 2.0.0 - 2.1.3` | yes | S07 | #5606 | CLOSED |
| 30 | npm | high | `browserslist` | `modules/gateway/frontend range <=4.28.6` | yes | S07 | #5606 | CLOSED |
| 31 | npm | high | `js-yaml` | `modules/gateway/frontend range 4.0.0 - 4.3.1` | yes | S07 | #5606 | CLOSED |
| 32 | npm | high | `nanoid` | `modules/gateway/frontend range <3.3.18` | yes | S07 | #5606 | CLOSED |
| 33 | npm | high | `undici` | `modules/gateway/frontend range 7.0.0 - 7.28.0` | yes | S07 | #5606 | CLOSED |

Selectors in full (same order) so each row is machine-reselectable:

| # | Selector |
|---|---|
| 1 | `grype|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif|ri=1` |
| 2 | `grype|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif|ri=22` |
| 3 | `grype|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif|ri=25` |
| 4 | `grype|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif|ri=26` |
| 5 | `grype|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif|ri=47` |
| 6 | `grype|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif|ri=53` |
| 7 | `grype|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif|ri=54` |
| 8 | `grype|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif|ri=55` |
| 9 | `grype|superplane/grype/modules-domain-apps-superplane-src-superplane-api.sarif|ri=94` |
| 10 | `grype|superplane/grype/superplane-skypilot-api.sarif|ri=104` |
| 11 | `grype|superplane/grype/superplane-skypilot-api.sarif|ri=440` |
| 12 | `grype|superplane/grype/superplane-skypilot-api.sarif|ri=442` |
| 13 | `grype|superplane/grype/superplane-skypilot-api.sarif|ri=444` |
| 14 | `grype|superplane/grype/superplane-skypilot-api.sarif|ri=448` |
| 15 | `semgrep|original/semgrep/semgrep-results.sarif|ri=2066` |
| 16 | `semgrep|original/semgrep/semgrep-results.sarif|ri=2178` |
| 17 | `semgrep|original/semgrep/semgrep-results.sarif|ri=2180` |
| 18 | `semgrep|original/semgrep/semgrep-results.sarif|ri=2243` |
| 19 | `semgrep|original/semgrep/semgrep-results.sarif|ri=2245` |
| 20 | `semgrep|original/semgrep/semgrep-results.sarif|ri=2347` |
| 21 | `semgrep|original/semgrep/semgrep-results.sarif|ri=2348` |
| 22 | `semgrep|original/semgrep/semgrep-results.sarif|ri=2418` |
| 23 | `bandit|original/bandit/bandit-results.sarif|ri=8712` |
| 24 | `semgrep|original/semgrep/semgrep-results.sarif|ri=758` |
| 25 | `npm-audit|modules/agent-factory/agent|brace-expansion` |
| 26 | `npm-audit|modules/agent-factory/agent|fast-uri` |
| 27 | `npm-audit|modules/agent-factory/agent|ip-address` |
| 28 | `npm-audit|modules/agent-factory/agent|js-yaml` |
| 29 | `npm-audit|modules/gateway/frontend|brace-expansion` |
| 30 | `npm-audit|modules/gateway/frontend|browserslist` |
| 31 | `npm-audit|modules/gateway/frontend|js-yaml` |
| 32 | `npm-audit|modules/gateway/frontend|nanoid` |
| 33 | `npm-audit|modules/gateway/frontend|undici` |

### C. All 16 older primary tickets → current owner

The 16 still-OPEN tickets of the 29-ticket legacy list. Each was re-checked live on 2026-09-24
and all 16 remain OPEN. Owners derived from the current issue bodies' explicit references, not
from title or severity matching.

| # | Ticket | Sev | Current owner(s) | Title |
|---|---|---|---|---|
| 1 | [#4701](https://github.com/aws-e/adp/issues/4701) | high | S10/5609 (OPEN) | [Security 2026-08-30] Internal and gateway planes trust unverified caller-id |
| 2 | [#4702](https://github.com/aws-e/adp/issues/4702) | high | S12/5611 (OPEN) | [Security 2026-08-30] Vault credential delivery routes rely on client-declar |
| 3 | [#4703](https://github.com/aws-e/adp/issues/4703) | high | S11/5610 (OPEN) | [Security 2026-08-30] Internal identity and installation resolution endpoint |
| 4 | [#4704](https://github.com/aws-e/adp/issues/4704) | high | S11/5610 (OPEN) | [Security 2026-08-30] External identity linking never proves control of the  |
| 5 | [#4706](https://github.com/aws-e/adp/issues/4706) | high | S13/5612 (OPEN) | [Security 2026-08-30] Admin routes miss permission and role-ceiling checks;  |
| 6 | [#4707](https://github.com/aws-e/adp/issues/4707) | high | S13/5612 (OPEN) | [Security 2026-08-30] Agent Cognito clients let a user rewrite their own rol |
| 7 | [#4709](https://github.com/aws-e/adp/issues/4709) | high | S18/5617 (OPEN) | [Security 2026-08-30] Client-supplied request identifier drives budget reser |
| 8 | [#4710](https://github.com/aws-e/adp/issues/4710) | high | S18/5617 (OPEN) | [Security 2026-08-30] Spend on the main model-invocation routes never reache |
| 9 | [#4718](https://github.com/aws-e/adp/issues/4718) | high | S15/5614 (OPEN) | [Security 2026-08-30] Ingestion writes content with no real access labels an |
| 10 | [#4719](https://github.com/aws-e/adp/issues/4719) | high | S15/5614 (OPEN) | [Security 2026-08-30] Ingestion fetches arbitrary caller-named web and objec |
| 11 | [#4720](https://github.com/aws-e/adp/issues/4720) | critical | S15/5614 (OPEN) | [Security 2026-08-30] Knowledge ingestion runs build scripts from untrusted  |
| 12 | [#4722](https://github.com/aws-e/adp/issues/4722) | high | S16/5615 (OPEN) | [Security 2026-08-30] Chat session and artifact identifiers accepted from th |
| 13 | [#4724](https://github.com/aws-e/adp/issues/4724) | high | S12/5611 (OPEN) | [Security 2026-08-30] Agent worker cloud roles can reach other tenants' secr |
| 14 | [#4725](https://github.com/aws-e/adp/issues/4725) | high | S13/5612 (OPEN), S14/5613 (OPEN) | [Security 2026-08-30] CI runner role can escalate to full account administra |
| 15 | [#4729](https://github.com/aws-e/adp/issues/4729) | high | S17/5616 (CLOSED) | [Security 2026-08-30] Cyber worker runs caller-named scripts with no real va |
| 16 | [#4730](https://github.com/aws-e/adp/issues/4730) | high | S17/5616 (CLOSED) | [Security 2026-08-30] Cyber workers read arbitrary and cross-org stored obje |

**#4725 is a genuine composite** — referenced by both S13 (#5612, Cognito/admin ceilings) and
S14 (#5613, CI runner IAM). Both contributions are retained; it is not collapsed to one owner.
**14 of the 16 have an OPEN owner**, so they cannot yet be resolved. The exception matters:
**#4729 and #4730 are still OPEN while their owner S17 (#5616) is CLOSED.** S17's own disposition
records gVisor, image build and CI wiring as open prerequisites, so this is consistent with a
closed package that did not discharge its older tickets — and it is precisely the pattern that
inferring closure from merge status would hide. No older ticket is resolved by this scan day.


### D. All 13 suppressed critical/high records — locations, as published

Every record carries `locations[].file` and `.line`; none requires raw-SARIF recovery.
What is missing is the **justification** — each `suppression` entry is exactly
`{"kind": "inSource"}` with no justification field. These 13 are excluded from the 860 by
design and are **not dispositioned anywhere in this snapshot**. Selector: `semgrep | artifact |
result_index`, all from `original/semgrep/semgrep-results.sarif` (run 35545387749).

| # | Sev | Rule | Location | result_index | Justification |
|---|---|---|---|---|---|
| 1 | high | `bandit.B606` | `modules/agent-factory/agent-worker-image/adp_cred/assume.py:127` | 1736 | **absent** |
| 2 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/clients/gitlab_client.ts:36` | 2022 | **absent** |
| 3 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/clients/gitlab_client.ts:56` | 2023 | **absent** |
| 4 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/clients/gitlab_client.ts:79` | 2024 | **absent** |
| 5 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/clients/gitlab_client.ts:107` | 2025 | **absent** |
| 6 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/complex-task-chat/vault/gateway-client.ts:139` | 2078 | **absent** |
| 7 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/complex-task-chat/vault/gateway-client.ts:156` | 2079 | **absent** |
| 8 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/components/checkRunStreamer.ts:593` | 2123 | **absent** |
| 9 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/github-comments.ts:414` | 2157 | **absent** |
| 10 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/gateway/frontend/src/services/activity.ts:207` | 8465 | **absent** |
| 11 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/gateway/frontend/src/services/api.ts:79` | 8466 | **absent** |
| 12 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/gateway/frontend/src/services/auth.ts:285` | 8469 | **absent** |
| 13 | critical | `nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/gateway/frontend/src/services/auth.ts:355` | 8470 | **absent** |

By rule: 1 × `bandit.B606` (high, dynamic process spawn) and 12 × 
`nodejs_scan.javascript-ssrf-rule-node_ssrf` (critical). By area: 9 in
`modules/agent-factory/`, 4 in `modules/gateway/frontend/src/services/`. S21's acceptance
requires per-location evidence for any false positive, so each of these 13 needs an
individual justification — recorded, not baselined away.


### E. All 8 Grype CVSS disagreements — advisory IDs, as published

Native scanner severity is low/medium while the CVSS score is ≥7. All eight advisory IDs are
in the inventory's `advisory` field. Selector: `grype | artifact | result_index`.

| # | Advisory | Package | Version | Native | CVSS | Fix version | Image | result_index |
|---|---|---|---|---|---|---|---|---|
| 1 | `CVE-2019-25210` | helm | 3.14.3-r4 | medium | **9.1** | **none** | superplane-controller | 0 |
| 2 | `GHSA-248v-346w-9cwc` | certifi | 2024.2.2 | low | **7.5** | 2024.7.4 | superplane-controller | 38 |
| 3 | `GHSA-8r3f-844c-mc37` | google.golang.org/protobuf | v1.31.0 | medium | **7.5** | 1.33.0 | superplane-controller | 44 |
| 4 | `CVE-2020-15778` | openssh-client | 1:10.0p1-7+deb13u1 | low | **7.8** | **none** | skypilot-api | 70 |
| 5 | `CVE-2020-15778` | openssh-server | 1:10.0p1-7+deb13u1 | low | **7.8** | **none** | skypilot-api | 71 |
| 6 | `CVE-2020-15778` | openssh-sftp-server | 1:10.0p1-7+deb13u1 | low | **7.8** | **none** | skypilot-api | 72 |
| 7 | `GHSA-h395-gr6q-cpjc` | jsonwebtoken | 9.3.1 | medium | **7.5** | 10.3.0 | skypilot-api | 445 |
| 8 | `GHSA-h395-gr6q-cpjc` | jsonwebtoken | 9.3.1 | medium | **7.5** | 10.3.0 | skypilot-api | 446 |

Three have **no fix version** (`CVE-2019-25210` helm, `CVE-2020-15778` openssh ×3 — the
openssh entries share one advisory across three packages). `GHSA-h395-gr6q-cpjc` appears twice
for the same jsonwebtoken 9.3.1 in one image, so the 8 rows are **7 distinct advisories**.
S19 recorded that a raw CVSS score outranking the scanner's own rating was one of the defects
it fixed, so these need a rating decision by S21 rather than automatic escalation.


---

## Provenance of this ledger

Produced under the read-only parallel assignment on #5620. Sources: live `gh` queries against
issues and pull requests; `findings.json` at `3193c78b`; `work-packages.json` at `d6091ba4d`;
committed dispositions under `docs/security/runs/2026-09-21/`; and the working tree at
`9afe1423`. No file outside this document was modified; no scan, build, merge, deployment,
IAM or configuration change, secret access or agent dispatch was performed.
