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

**Eleven packages were never dispatched at all.** Six daily packages (S10 #5609, S11 #5610,
S12 #5611, S13 #5612, S18 #5617) and six AWS packages (A03 #5655, A09 #5663, A11 #5666,
A12 #5668, A13 #5669, A14 #5670) have **zero comments** — confirmed not-started rather than
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

1. **S03's approved image is not in the lock.** S03 (#5602, CLOSED) concluded on a derived
   linux/amd64 image `sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec`,
   built on the **SkyPilot 0.12.3** base with setuptools 81.0.0. The lock still pins the
   **0.12.0** digest. Downstream references derive from the lock rather than restating it
   (`infra/control-plane/config.tf` reads `local.lock.images["skypilot-api"]`; the manifest
   carries `REPLACE_WITH_SKYPILOT_IMAGE`), so a single reviewed lock edit propagates — but
   until it happens, the fixed image is **not** what the release resolves to.
2. **S01's built controller image is not in the lock.** S01 (#5600, CLOSED) recorded
   `adp-superplane-controller@sha256:d87cf6355d…ce38ed` with a build run, a SUCCEEDED
   validation build and four runtime checks passing, and states the digest "is available to
   S21 without requiring a lock-file edit". `superplane-controller` remains in
   `pending_images` with "no build has run yet" — which is now stale as a description.

**Consequence:** the S01/S02/S03 remediation is real and evidenced at the image level, but
the **shared release still resolves to unfixed pins**. This is exactly the "code delivered,
not deployed" distinction, and it is unresolved.

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
| 13 suppressed critical/high Semgrep records | **No disposition exists.** The inventory carries `suppression.kind: inSource` but **null file, line and justification** — so no location-specific evidence is available from it. S21's acceptance requires per-location evidence for a false positive; that evidence must be recovered from source, not from this inventory. |
| 8 Grype CVSS disagreements | Enumerated (native low/medium vs CVSS 7.5–9.1: helm 9.1, certifi 7.5, protobuf 7.5, openssh-client/server/sftp 7.8, jsonwebtoken 7.5 ×2) but **`vulnerability_id` is null in every record** — the advisory IDs must be recovered from the source SARIF before they can be dispositioned. |
| 335 of 860 S20 records verdicted `needs-followon` | **No follow-on issues appear to have been filed.** A search for the 11 proposed follow-on packages (FOLLOWON-A…I) returned no matching issues. The largest, FOLLOWON-E, covers 244 k8s pod-hardening records. **These 335 records currently have no GitHub owner.** |
| Scanner tooling gaps flagged *to* S21 by owners | **Open.** S09 flagged that the pinned `bandit[sarif]==1.7.9` and `.banditrc` are absent from the checkout. A18 found the checkov allowlist is **inert** because the workflow passes `--directory .`. A24 found cfn-nag is not PR-triggered. All three affect whether S21's final scan is trustworthy, and all three are scanner-configuration matters in S21's lane. |
| `.grype.yaml` review date | **Lapsed.** The file's own header sets "Next review: 2026-08-22"; it is 2026-09-24 and 189 ignore entries are in force. Each entry removes findings from SARIF output entirely, so a lapsed review silently suppresses. S21 owns this. |
| 16 older primary tickets | All 16 re-checked live and **all still OPEN** (#4701–#4730). None closed by this scan day. |

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
2. **Recover the two evidence gaps first**, because dispositions depend on them: the
   advisory IDs behind the 8 CVSS disagreements, and the file/line/justification for the 13
   suppressed records, both from the source SARIF.
3. **Integrate the image digests in one reviewed commit** — S03's
   `sha256:f8d893cf…de55ec` and S01's `sha256:d87cf635…ce38ed` — promoting
   `superplane-controller` out of `pending_images`. Keep the lock, `k8s/40-skypilot-api.yaml`
   and `infra/control-plane/config.tf` consistent; the downstream refs derive from the lock,
   so re-assert the shape tests (`tests/test_lock.py`, `tests/lock_pin.tftest.hcl`,
   `tests/test_skypilot_startup_contract.py`). **No placeholder digest** for the three images
   that still have no build.
4. **Reconcile the shared scanner disposition**: refresh `.grype.yaml`'s lapsed review, and
   decide the 4 S21-owned `CKV_AWS_51` records on their merits. Per-location evidence only —
   no whole-rule suppression, no baselining away unresolved findings.
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
| S02 | 5601 | CLOSED | 5783 | `27a5b943` | yes | no | API suite 1203 passed / 25 skipped. Rebuilt-image scan + SBOM criterion **not satisfied**. |
| S03 | 5602 | CLOSED | 5724 | `c742c087` | yes | no | **Doc-only PR** — touched no lock/manifest/infra file. Re-pin "handed over as data" to S21. Live startup/persistence unverified. |
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

18 of 24 merged. Every merged package explicitly disclaims live deployment.

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

## Provenance of this ledger

Produced under the read-only parallel assignment on #5620. Sources: live `gh` queries against
issues and pull requests; `findings.json` at `3193c78b`; `work-packages.json` at `d6091ba4d`;
committed dispositions under `docs/security/runs/2026-09-21/`; and the working tree at
`9afe1423`. No file outside this document was modified; no scan, build, merge, deployment,
IAM or configuration change, secret access or agent dispatch was performed.
